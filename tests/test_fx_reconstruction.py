# SPDX-License-Identifier: MIT
"""Unit tests for FX reconstruction across statement snapshots.

Cost basis on a non-base-currency holding must be translated using the
acquisition-date FX rate (approximated as the earliest snapshot date the
(account, symbol) pair appears on), not the period-end rate. Translating
both market value and cost basis at the period-end rate hides
(rate_today − rate_acquired) × cost_local of FX-driven P&L from
per-position attribution.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from stock_portfolio_auditor.domain.errors import MissingFxRateError
from stock_portfolio_auditor.domain.models import (
    AssetKind,
    Holding,
    Statement,
    Transaction,
)
from stock_portfolio_auditor.ingestion.fx_reconstruction import fx_convert_statements
from stock_portfolio_auditor.pricing.providers.base import PriceProvider


class _StubProvider(PriceProvider):
    """Tiny offline FX provider for deterministic FX-rate test fixtures."""

    name = "stub"

    def __init__(self, series: pd.Series) -> None:
        self._series = series.sort_index()

    def get_close(self, ticker, start, end):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def get_splits(self, ticker, start, end):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def get_fx(self, base, quote, start, end):  # type: ignore[no-untyped-def]
        ts_start = pd.Timestamp(start)
        ts_end = pd.Timestamp(end)
        return self._series[(self._series.index >= ts_start) & (self._series.index <= ts_end)]


def _holding(
    *,
    symbol: str,
    currency: str,
    mv_local: Decimal,
    cb_local: Decimal,
    as_of: date,
) -> Holding:
    return Holding(
        symbol=symbol,
        kind=AssetKind.EQUITY,
        currency=currency,
        quantity=Decimal("100"),
        market_value_local=mv_local,
        market_value_base=mv_local,
        cost_basis_local=cb_local,
        cost_basis_base=cb_local,
        as_of=as_of,
    )


def _statement(*, end: date, holdings: tuple[Holding, ...]) -> Statement:
    return Statement(
        account_label="brokerage_intl",
        broker="ibkr",
        base_currency="USD",
        period_start=date(end.year, end.month, 1),
        period_end=end,
        frequency="M",
        beginning_value_base=Decimal("1000"),
        ending_value_base=Decimal("1100"),
        holdings=holdings,
        parser_version=1,
        raw_text_hash=f"hash-{end.isoformat()}",
    )


def test_cost_basis_translated_at_acquisition_rate_market_value_at_period_end_rate() -> None:
    """Long-held HKD position with FX drift exposes the FX gain on cost."""
    fx = pd.Series(
        {pd.Timestamp("2024-01-31"): 0.13, pd.Timestamp("2024-06-30"): 0.12},
        name="HKDUSD",
    )
    provider = _StubProvider(fx)

    jan = _statement(
        end=date(2024, 1, 31),
        holdings=(
            _holding(
                symbol="1681.HK",
                currency="HKD",
                mv_local=Decimal("10000"),
                cb_local=Decimal("5000"),
                as_of=date(2024, 1, 31),
            ),
        ),
    )
    jun = _statement(
        end=date(2024, 6, 30),
        holdings=(
            _holding(
                symbol="1681.HK",
                currency="HKD",
                mv_local=Decimal("12000"),
                cb_local=Decimal("5000"),  # unchanged cost basis: same lot held
                as_of=date(2024, 6, 30),
            ),
        ),
    )

    translated = fx_convert_statements([jan, jun], provider=provider)
    jun_holding = translated[1].holdings[0]
    # MV translates at period-end rate (0.12): 12000 * 0.12 = 1440.
    assert jun_holding.market_value_base == Decimal("1440.0000")
    # CB translates at acquisition rate (0.13, from the earliest snapshot):
    # 5000 * 0.13 = 650 (NOT 5000 * 0.12 = 600).
    assert jun_holding.cost_basis_base == Decimal("650.0000")
    # Unrealized P&L in USD = MV - CB = 1440 - 650 = 790, which embeds both
    # the local-currency price gain and the FX drift on the cost basis.
    unrealized = float(jun_holding.market_value_base) - float(jun_holding.cost_basis_base)
    assert unrealized == 790.0


def test_transactions_translated_at_trade_date_rate() -> None:
    """A HKD trade row must have ``amount_base`` multiplied by the FX rate
    at the trade's date, not left equal to ``amount_local``.

    Without this translation, turnover (which sums |amount_base| over
    in-window buys / sells) treats a HK$50,000 purchase as US$50,000,
    inflating IBKR HKD trading volume by ~8x. The regression here pins
    that the FX path covers transactions, not just holdings.
    """
    fx = pd.Series(
        {pd.Timestamp("2024-03-15"): 0.128, pd.Timestamp("2024-06-30"): 0.12},
        name="HKDUSD",
    )
    provider = _StubProvider(fx)
    mar_buy = Transaction(
        trade_date=date(2024, 3, 15),
        action="buy",
        symbol="3347.HK",
        kind=AssetKind.EQUITY,
        currency="HKD",
        quantity=Decimal("1000"),
        amount_local=Decimal("-50000"),
        amount_base=Decimal("-50000"),
        commission=Decimal("12.5"),
    )
    stmt = Statement(
        account_label="ibkr_intl",
        broker="ibkr",
        base_currency="USD",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 6, 30),
        frequency="M",
        beginning_value_base=Decimal("1000"),
        ending_value_base=Decimal("1100"),
        holdings=(),
        transactions=(mar_buy,),
        parser_version=1,
        raw_text_hash="hash",
    )
    translated = fx_convert_statements([stmt], provider=provider)
    txn = translated[0].transactions[0]
    # 50000 * 0.128 = 6400 USD.
    assert txn.amount_base == Decimal("-6400.0000")
    # Commission also FX-translated.
    assert txn.commission == Decimal("1.6000")
    # Local amount is unchanged so subsequent passes can detect "already
    # translated" via the local/base equality guard.
    assert txn.amount_local == Decimal("-50000.0000")


def test_transactions_in_base_currency_pass_through_unchanged() -> None:
    """USD transactions on a USD-base statement get no FX work done."""
    fx = pd.Series({pd.Timestamp("2024-03-15"): 1.0}, name="USDUSD")
    provider = _StubProvider(fx)
    txn_usd = Transaction(
        trade_date=date(2024, 3, 15),
        action="buy",
        symbol="AAPL",
        kind=AssetKind.EQUITY,
        currency="USD",
        quantity=Decimal("10"),
        amount_local=Decimal("-1900"),
        amount_base=Decimal("-1900"),
    )
    stmt = Statement(
        account_label="us",
        broker="ibkr",
        base_currency="USD",
        period_start=date(2024, 3, 1),
        period_end=date(2024, 3, 31),
        frequency="M",
        beginning_value_base=Decimal("1000"),
        ending_value_base=Decimal("1100"),
        transactions=(txn_usd,),
        parser_version=1,
        raw_text_hash="h",
    )
    translated = fx_convert_statements([stmt], provider=provider)
    assert translated[0].transactions[0].amount_base == Decimal("-1900")


def test_strict_mode_raises_when_required_fx_rate_is_missing() -> None:
    """When the FX provider can't supply a rate within
    ``_MAX_FX_RATE_GAP_DAYS`` of a required date, the pipeline aborts
    with :class:`MissingFxRateError` instead of silently leaving
    HKD-nominal values in ``amount_base`` / ``market_value_base``.

    Cold-cache yfinance rate-limits are the typical reason this would
    fire on a real run; the error message tells the operator to wait
    a few minutes and re-run (cached rates from earlier runs persist).
    """
    # FX series stops at 2024-01-15, but the holding's period_end and
    # the trade_date both fall in March -- well past the
    # _MAX_FX_RATE_GAP_DAYS=7 tolerance.
    fx = pd.Series({pd.Timestamp("2024-01-15"): 0.13}, name="HKDUSD")
    provider = _StubProvider(fx)
    holding = _holding(
        symbol="3347.HK",
        currency="HKD",
        mv_local=Decimal("1000"),
        cb_local=Decimal("800"),
        as_of=date(2024, 3, 31),
    )
    stmt = Statement(
        account_label="ibkr",
        broker="ibkr",
        base_currency="USD",
        period_start=date(2024, 3, 1),
        period_end=date(2024, 3, 31),
        frequency="M",
        beginning_value_base=Decimal("1000"),
        ending_value_base=Decimal("1100"),
        holdings=(holding,),
        parser_version=1,
        raw_text_hash="h",
    )
    with pytest.raises(MissingFxRateError) as exc:
        fx_convert_statements([stmt], provider=provider)
    # Error message includes the missing pair and the date.
    assert "HKD->USD" in str(exc.value)
    assert "2024-03-31" in str(exc.value)
    # And the structured context too (CLI / logs can render either).
    assert exc.value.context["missing_pairs"] == [
        ("HKD", "USD", "2024-03-31"),
    ]


def test_strict_mode_passes_when_cache_covers_within_tolerance() -> None:
    """A small (~weekend / holiday) gap is tolerable; the cached series
    a few days before the trade is allowed."""
    fx = pd.Series(
        {
            pd.Timestamp("2024-03-25"): 0.128,  # Mon
            pd.Timestamp("2024-03-26"): 0.128,
        },
        name="HKDUSD",
    )
    provider = _StubProvider(fx)
    txn = Transaction(
        trade_date=date(2024, 3, 29),  # Fri after the cached dates
        action="buy",
        symbol="3347.HK",
        kind=AssetKind.EQUITY,
        currency="HKD",
        amount_local=Decimal("-10000"),
        amount_base=Decimal("-10000"),
    )
    stmt = Statement(
        account_label="ibkr",
        broker="ibkr",
        base_currency="USD",
        period_start=date(2024, 3, 1),
        period_end=date(2024, 3, 31),
        frequency="M",
        beginning_value_base=Decimal("1000"),
        ending_value_base=Decimal("1100"),
        transactions=(txn,),
        parser_version=1,
        raw_text_hash="h",
    )
    # Should not raise.
    out = fx_convert_statements([stmt], provider=provider)
    # 10000 * 0.128 = 1280.
    assert out[0].transactions[0].amount_base == Decimal("-1280.0000")


def test_non_strict_mode_silently_skips_missing_rates() -> None:
    """``strict=False`` is the courtesy mode for inspection contexts
    (e.g. parsed-CSV writes). Missing rates leave the row at
    ``amount_local == amount_base``."""
    fx = pd.Series({pd.Timestamp("2024-01-15"): 0.13}, name="HKDUSD")
    provider = _StubProvider(fx)
    holding = _holding(
        symbol="3347.HK",
        currency="HKD",
        mv_local=Decimal("1000"),
        cb_local=Decimal("800"),
        as_of=date(2024, 3, 31),
    )
    stmt = Statement(
        account_label="ibkr",
        broker="ibkr",
        base_currency="USD",
        period_start=date(2024, 3, 1),
        period_end=date(2024, 3, 31),
        frequency="M",
        beginning_value_base=Decimal("1000"),
        ending_value_base=Decimal("1100"),
        holdings=(holding,),
        parser_version=1,
        raw_text_hash="h",
    )
    out = fx_convert_statements([stmt], provider=provider, strict=False)
    # Untouched (no rate available within tolerance).
    assert out[0].holdings[0].market_value_base == Decimal("1000")


def test_idempotent_when_already_translated() -> None:
    """A second call must not re-translate already-converted holdings."""
    fx = pd.Series({pd.Timestamp("2024-01-31"): 0.13}, name="HKDUSD")
    provider = _StubProvider(fx)
    jan = _statement(
        end=date(2024, 1, 31),
        holdings=(
            _holding(
                symbol="1681.HK",
                currency="HKD",
                mv_local=Decimal("10000"),
                cb_local=Decimal("5000"),
                as_of=date(2024, 1, 31),
            ),
        ),
    )
    once = fx_convert_statements([jan], provider=provider)
    twice = fx_convert_statements(once, provider=provider)
    assert once[0].holdings[0].market_value_base == twice[0].holdings[0].market_value_base
    assert once[0].holdings[0].cost_basis_base == twice[0].holdings[0].cost_basis_base
