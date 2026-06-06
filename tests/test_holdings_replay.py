# SPDX-License-Identifier: MIT
"""Unit tests for the holdings-replay engine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.analytics.holdings_replay import (
    replay_account_quantities,
    replay_all_accounts,
)
from stock_portfolio_auditor.domain.models import AssetKind, Holding, Transaction
from tests.factories import make_statement


def _holding(symbol: str, quantity: Decimal, currency: str = "USD") -> Holding:
    return Holding(
        symbol=symbol,
        kind=AssetKind.EQUITY,
        currency=currency,
        quantity=quantity,
        market_value_local=quantity * Decimal("100"),
        market_value_base=quantity * Decimal("100"),
        as_of=date(2023, 12, 31),
    )


def _trade(
    symbol: str,
    quantity: Decimal,
    trade_date: date,
    *,
    currency: str = "USD",
) -> Transaction:
    return Transaction(
        trade_date=trade_date,
        action="buy" if quantity > 0 else "sell",
        symbol=symbol,
        kind=AssetKind.EQUITY,
        currency=currency,
        quantity=quantity,
        amount_local=Decimal("0"),
        amount_base=Decimal("0"),
    )


def test_replay_returns_anchor_snapshot_directly_when_dates_match() -> None:
    """A snapshot at exactly the target date is the answer; no replay needed."""
    snap = make_statement(
        account_label="ibkr",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "holdings": (
                _holding("AAPL", Decimal("100")),
                _holding("MSFT", Decimal("50")),
            ),
        }
    )
    result = replay_account_quantities([snap], date(2023, 12, 31))
    assert result.quantities == {"AAPL": Decimal("100"), "MSFT": Decimal("50")}
    assert result.anchor_date == date(2023, 12, 31)


def test_replay_walks_backward_from_year_end_snapshot_for_mid_year_target() -> None:
    """IBKR-style: a Dec 31 snapshot plus trades during the year lets us
    reconstruct quantity at any mid-year month-end by reversing the
    trades that happened after the target date.

    Scenario: Dec 31 snapshot says AAPL=100, MSFT=50. The Trades section
    contains a Nov 15 buy of 30 AAPL and an Oct 5 sell of 20 MSFT (so
    they were carried into the year as AAPL=70, MSFT=70). For a Sep 30
    target date, replay should reverse the Oct and Nov trades and
    report AAPL=70, MSFT=70.
    """
    snap = make_statement(
        account_label="ibkr",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "holdings": (
                _holding("AAPL", Decimal("100")),
                _holding("MSFT", Decimal("50")),
            ),
            "transactions": (
                _trade("AAPL", Decimal("30"), date(2023, 11, 15)),
                _trade("MSFT", Decimal("-20"), date(2023, 10, 5)),
            ),
        }
    )
    result = replay_account_quantities([snap], date(2023, 9, 30))
    assert result.quantities == {"AAPL": Decimal("70"), "MSFT": Decimal("70")}
    assert result.anchor_date == date(2023, 12, 31)


def test_replay_walks_forward_when_only_past_snapshots_available() -> None:
    """When ``as_of`` is past the latest snapshot, apply post-snapshot trades forward."""
    snap = make_statement(
        account_label="ibkr",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "holdings": (_holding("AAPL", Decimal("100")),),
        }
    )
    later_trades_stmt = make_statement(
        account_label="ibkr",
        start=date(2024, 1, 1),
        end=date(2024, 6, 30),
        frequency="M",
    ).model_copy(
        update={
            "transactions": (
                _trade("AAPL", Decimal("25"), date(2024, 2, 15)),
                _trade("AAPL", Decimal("-10"), date(2024, 4, 20)),
            ),
        }
    )
    result = replay_account_quantities(
        [snap, later_trades_stmt], date(2024, 3, 31)
    )
    # By Mar 31, 2024: +25 applied (Feb 15), -10 not yet (Apr 20).
    assert result.quantities == {"AAPL": Decimal("125")}


def test_replay_drops_zero_quantity_positions() -> None:
    """A position fully closed before as_of should not appear in the output."""
    snap = make_statement(
        account_label="ibkr",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "holdings": (_holding("AAPL", Decimal("100")),),
            "transactions": (
                # All 100 sold on Dec 1, so Dec 31 holdings of 100 implies
                # 200 were held on Nov 30 (counterfactual but useful to
                # pin the algorithm).
                _trade("AAPL", Decimal("-100"), date(2023, 12, 1)),
            ),
        }
    )
    nov_30 = replay_account_quantities([snap], date(2023, 11, 30))
    assert nov_30.quantities == {"AAPL": Decimal("200")}

    snap_closed = make_statement(
        account_label="ibkr",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "holdings": (),  # nothing left at Dec 31
            "transactions": (
                _trade("XYZ", Decimal("50"), date(2023, 3, 1)),
                _trade("XYZ", Decimal("-50"), date(2023, 8, 1)),
            ),
        }
    )
    result_mid = replay_account_quantities([snap_closed], date(2023, 6, 30))
    # Closed positions: at Dec 31 no holdings, so empty anchor. Walking
    # backward from empty applies nothing -- we never see XYZ. This is
    # a known limitation when the anchor snapshot is the only source of
    # symbols. Tests pin the current behaviour so a future improvement
    # (e.g. seeding from trades when the anchor is empty) can rewrite
    # it deliberately.
    assert result_mid.quantities == {}


def test_replay_filters_options_and_fx_pseudo_symbols() -> None:
    """``_OPT_*`` and ``_FX_*`` rows are not stock positions."""
    snap = make_statement(
        account_label="ibkr",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "holdings": (
                _holding("AAPL", Decimal("100")),
                Holding(
                    symbol="_OPT_AAPL",
                    kind=AssetKind.OPTION,
                    currency="USD",
                    quantity=Decimal("5"),
                    market_value_local=Decimal("500"),
                    market_value_base=Decimal("500"),
                    as_of=date(2023, 12, 31),
                ),
            ),
        }
    )
    result = replay_account_quantities([snap], date(2023, 12, 31))
    assert result.quantities == {"AAPL": Decimal("100")}


def test_replay_all_accounts_partitions_by_account_label() -> None:
    """Trades on one account never affect quantities on another."""
    schwab = make_statement(
        account_label="schwab",
        start=date(2023, 6, 1),
        end=date(2023, 6, 30),
        frequency="M",
    ).model_copy(update={"holdings": (_holding("BRKB", Decimal("10")),)})
    ibkr = make_statement(
        account_label="ibkr",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "holdings": (_holding("3347.HK", Decimal("1000"), currency="HKD"),),
            "transactions": (
                _trade("3347.HK", Decimal("500"), date(2023, 9, 1), currency="HKD"),
            ),
        }
    )
    results = replay_all_accounts([schwab, ibkr], date(2023, 6, 30))
    assert results["schwab"].quantities == {"BRKB": Decimal("10")}
    # At Jun 30, 2023 IBKR held 3347.HK pre-Sep-purchase: 1000 - 500 = 500.
    assert results["ibkr"].quantities == {"3347.HK": Decimal("500")}
    assert results["ibkr"].currency_by_symbol == {"3347.HK": "HKD"}


def test_replay_returns_empty_when_account_has_no_snapshots() -> None:
    """An account with only transaction-history CSVs (no holdings snapshot)
    has no anchor; replay returns empty rather than guessing."""
    stmt = make_statement(
        account_label="orphan",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "transactions": (
                _trade("AAPL", Decimal("50"), date(2023, 5, 1)),
            ),
        }
    )
    result = replay_account_quantities([stmt], date(2023, 6, 30))
    assert result.quantities == {}
    assert result.anchor_date is None
