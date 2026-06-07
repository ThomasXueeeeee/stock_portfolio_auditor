# SPDX-License-Identifier: MIT
"""Unit tests for monthly portfolio-concentration analytics."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.analytics.concentration import (
    ConcentrationSnapshot,
    concentration_metrics,
    monthly_concentration_series,
    stock_holdings_at,
    yearly_concentration_aggregates,
)
from stock_portfolio_auditor.domain.models import AssetKind, Holding
from tests.factories import make_statement


def _holding(
    symbol: str,
    *,
    market_value_base: Decimal,
    kind: AssetKind = AssetKind.EQUITY,
    as_of: date = date(2025, 3, 31),
) -> Holding:
    return Holding(
        symbol=symbol,
        kind=kind,
        currency="USD",
        quantity=Decimal("1"),
        market_value_local=market_value_base,
        market_value_base=market_value_base,
        as_of=as_of,
    )


def test_equal_weighted_book_has_effective_n_equal_to_position_count() -> None:
    """An equally weighted book of N positions has HHI = 1/N and effective_N = N."""
    holdings = [_holding(f"S{i}", market_value_base=Decimal("100")) for i in range(5)]
    snap = concentration_metrics(holdings, as_of=date(2025, 3, 31))
    assert snap is not None
    assert snap.n_positions == 5
    assert snap.hhi == pytest.approx(0.20)
    assert snap.effective_n == pytest.approx(5.0)
    assert snap.top1_share == pytest.approx(0.20)
    assert snap.top5_share == pytest.approx(1.0)
    assert snap.top10_share == pytest.approx(1.0)


def test_single_name_book_has_max_concentration() -> None:
    """One-position book has HHI = 1 and effective_N = 1."""
    snap = concentration_metrics(
        [_holding("ONLY", market_value_base=Decimal("500"))], as_of=date(2025, 3, 31)
    )
    assert snap is not None
    assert snap.hhi == pytest.approx(1.0)
    assert snap.effective_n == pytest.approx(1.0)
    assert snap.top1_share == pytest.approx(1.0)


def test_options_and_fx_pseudo_symbols_excluded_from_stock_concentration() -> None:
    """``_OPT_*`` and ``_FX_*`` rows are not stock positions."""
    holdings = [
        _holding("AAPL", market_value_base=Decimal("100")),
        _holding("_OPT_AAPL", market_value_base=Decimal("100"), kind=AssetKind.OPTION),
        _holding("_FX_HKD", market_value_base=Decimal("100"), kind=AssetKind.FX),
    ]
    snap = concentration_metrics(holdings, as_of=date(2025, 3, 31))
    assert snap is not None
    # Pseudo-symbol option/FX rows skipped via kind filter; only AAPL counts.
    assert snap.n_positions == 1
    assert snap.top1_share == pytest.approx(1.0)


def test_concentration_returns_none_when_no_long_equity_value() -> None:
    """A book with only zero / negative market values produces no metric."""
    holdings = [_holding("BAD", market_value_base=Decimal("0"))]
    snap = concentration_metrics(holdings, as_of=date(2025, 3, 31))
    assert snap is None


def test_stock_holdings_at_picks_latest_snapshot_per_account() -> None:
    """When two snapshots from the same account precede ``as_of`` we use the later one."""
    early = make_statement(
        account_label="A", start=date(2025, 1, 1), end=date(2025, 1, 31), frequency="M"
    ).model_copy(
        update={
            "holdings": (_holding("AAPL", market_value_base=Decimal("100")),),
        }
    )
    late = make_statement(
        account_label="A", start=date(2025, 2, 1), end=date(2025, 2, 28), frequency="M"
    ).model_copy(
        update={
            "holdings": (_holding("AAPL", market_value_base=Decimal("200")),),
        }
    )
    holdings = stock_holdings_at([early, late], as_of=date(2025, 3, 15))
    assert sum(float(h.market_value_base) for h in holdings) == 200.0


def test_stock_holdings_at_pools_across_accounts() -> None:
    """Holdings from each account's latest <= as_of snapshot sum together."""
    schwab = make_statement(
        account_label="schwab",
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
        frequency="M",
    ).model_copy(update={"holdings": (_holding("AAPL", market_value_base=Decimal("400")),)})
    ibkr = make_statement(
        account_label="ibkr",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(update={"holdings": (_holding("MSFT", market_value_base=Decimal("600")),)})
    holdings = stock_holdings_at([schwab, ibkr], as_of=date(2025, 3, 31))
    by_symbol = {h.symbol: float(h.market_value_base) for h in holdings}
    assert by_symbol == {"AAPL": 400.0, "MSFT": 600.0}


def test_monthly_concentration_series_emits_one_snapshot_per_in_window_month() -> None:
    """Series snaps to calendar month-ends inside ``[start, end]``."""
    jan = make_statement(
        account_label="A", start=date(2025, 1, 1), end=date(2025, 1, 31), frequency="M"
    ).model_copy(
        update={
            "holdings": (
                _holding("AAPL", market_value_base=Decimal("100")),
                _holding("MSFT", market_value_base=Decimal("100")),
            )
        }
    )
    feb = make_statement(
        account_label="A", start=date(2025, 2, 1), end=date(2025, 2, 28), frequency="M"
    ).model_copy(
        update={
            "holdings": (
                _holding("AAPL", market_value_base=Decimal("500")),
                _holding("MSFT", market_value_base=Decimal("100")),
            )
        }
    )
    snaps = monthly_concentration_series([jan, feb], start=date(2025, 1, 1), end=date(2025, 2, 28))
    assert [s.as_of for s in snaps] == [date(2025, 1, 31), date(2025, 2, 28)]
    # Feb skewed more heavily to AAPL -> higher HHI.
    assert snaps[1].hhi > snaps[0].hhi


def test_yearly_concentration_aggregates_includes_total_row() -> None:
    """Per-year average plus a Total across all snapshots is emitted."""
    snaps = [
        ConcentrationSnapshot(
            as_of=date(2024, 12, 31),
            portfolio_value_base=1000.0,
            n_positions=5,
            hhi=0.20,
            effective_n=5.0,
            top1_share=0.20,
            top5_share=1.0,
            top10_share=1.0,
        ),
        ConcentrationSnapshot(
            as_of=date(2025, 6, 30),
            portfolio_value_base=2000.0,
            n_positions=4,
            hhi=0.25,
            effective_n=4.0,
            top1_share=0.30,
            top5_share=1.0,
            top10_share=1.0,
        ),
        ConcentrationSnapshot(
            as_of=date(2025, 12, 31),
            portfolio_value_base=3000.0,
            n_positions=10,
            hhi=0.10,
            effective_n=10.0,
            top1_share=0.15,
            top5_share=0.60,
            top10_share=1.0,
        ),
    ]
    rows = yearly_concentration_aggregates(
        snaps, audit_start=date(2024, 1, 1), audit_end=date(2025, 12, 31)
    )
    labels = [r.label for r in rows]
    assert labels == ["2024", "2025", "Total"]
    assert rows[0].months_observed == 1
    assert rows[1].months_observed == 2
    assert rows[2].months_observed == 3
    # 2025 average HHI is mean(0.25, 0.10) = 0.175.
    assert rows[1].avg_hhi == pytest.approx(0.175)
    # Total is across all 3 snapshots; avg of (0.20, 0.25, 0.10) ≈ 0.1833.
    assert rows[2].avg_hhi == pytest.approx((0.20 + 0.25 + 0.10) / 3)
