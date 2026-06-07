# SPDX-License-Identifier: MIT
"""Unit tests for the audit-window statement filter.

The filter clips partial-overlap statements rather than dropping them so
that an annual 1099 Composite or a YTD IBKR CSV can still contribute the
data inside the requested ``[start_date, end_date]`` interval. The tests
exercise the three documented behaviours:

* fully in-window statements pass through unchanged;
* fully out-of-window statements are excluded with a reason;
* partial-overlap statements are returned with transactions, holdings
  and (when per-disposition data is available) ``per_symbol_pnl_base``
  trimmed to the in-window slice, and a note describing the clipping is
  surfaced for the run log.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.domain.models import (
    AssetKind,
    Holding,
    Statement,
    Transaction,
)
from stock_portfolio_auditor.ingestion.audit_window import filter_statements_to_window
from tests.factories import make_statement


def _stmt(start: date, end: date, *, label: str = "acct") -> Statement:
    return make_statement(account_label=label, start=start, end=end, frequency="M").model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("110")}
    )


def _disposition(symbol: str, sold: date, realized: Decimal) -> Transaction:
    return Transaction(
        trade_date=sold,
        action="sell",
        symbol=symbol,
        kind=AssetKind.EQUITY,
        currency="USD",
        amount_local=Decimal("0"),
        amount_base=Decimal("0"),
        realized_pnl_base=realized,
        source_section="1099-B",
    )


def _holding(symbol: str, as_of: date) -> Holding:
    return Holding(
        symbol=symbol,
        kind=AssetKind.EQUITY,
        currency="USD",
        quantity=Decimal("1"),
        market_value_local=Decimal("100"),
        market_value_base=Decimal("100"),
        as_of=as_of,
    )


def test_statement_fully_inside_window_included_unchanged() -> None:
    stmt = _stmt(date(2025, 3, 1), date(2025, 3, 31))
    included, excluded = filter_statements_to_window(
        [stmt], start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    assert included == [stmt]
    assert excluded == []


def test_statement_ending_before_start_fully_excluded() -> None:
    stmt = _stmt(date(2022, 1, 1), date(2022, 9, 30))
    included, excluded = filter_statements_to_window(
        [stmt], start_date=date(2022, 10, 1), end_date=date(2026, 4, 30)
    )
    assert included == []
    assert len(excluded) == 1
    assert "fully outside audit window" in excluded[0][1]


def test_statement_starting_after_end_fully_excluded() -> None:
    stmt = _stmt(date(2026, 5, 1), date(2026, 5, 31))
    included, excluded = filter_statements_to_window(
        [stmt], start_date=date(2022, 10, 1), end_date=date(2026, 4, 30)
    )
    assert included == []
    assert len(excluded) == 1
    assert "fully outside audit window" in excluded[0][1]


def test_partial_overlap_1099_clips_dispositions_to_window() -> None:
    """A full-year 1099 with the audit starting mid-year keeps only the
    dispositions sold inside the window and rebuilds ``per_symbol_pnl_base``
    from them."""
    early = _disposition("GOOGL", date(2022, 3, 15), Decimal("-6214.00"))
    in_q4_a = _disposition("AMZN", date(2022, 11, 10), Decimal("500.00"))
    in_q4_b = _disposition("AMZN", date(2022, 12, 1), Decimal("300.00"))
    stmt = make_statement(
        account_label="schwab_individual",
        start=date(2022, 1, 1),
        end=date(2022, 12, 31),
        frequency="A",
        transactions=(early, in_q4_a, in_q4_b),
    ).model_copy(
        update={
            "per_symbol_pnl_base": {
                "GOOGL": Decimal("-6214.00"),
                "AMZN": Decimal("800.00"),
            }
        }
    )
    included, excluded = filter_statements_to_window(
        [stmt], start_date=date(2022, 10, 1), end_date=date(2026, 4, 30)
    )
    assert len(included) == 1
    clipped = included[0]
    assert clipped.transactions == (in_q4_a, in_q4_b)
    assert clipped.per_symbol_pnl_base == {"AMZN": Decimal("800.00")}
    assert len(excluded) == 1
    _, note = excluded[0]
    assert "clipped to 2022-10-01..2026-04-30" in note
    assert "before audit start 2022-10-01" in note
    assert "rebuilt from in-window dispositions" in note


def test_partial_overlap_ibkr_ytd_keeps_full_period_aggregate() -> None:
    """An IBKR YTD whose Mark-to-Market Performance Summary mixes realized
    and unrealized cannot be split per-trade, so the aggregate is kept
    as-is (small overshoot is preferred over dropping the statement)."""
    in_window_trade = Transaction(
        trade_date=date(2026, 2, 15),
        action="buy",
        symbol="NBIS",
        kind=AssetKind.EQUITY,
        currency="USD",
        amount_local=Decimal("-5000"),
        amount_base=Decimal("-5000"),
        realized_pnl_base=None,
    )
    out_of_window_trade = Transaction(
        trade_date=date(2026, 5, 10),
        action="sell",
        symbol="NBIS",
        kind=AssetKind.EQUITY,
        currency="USD",
        amount_local=Decimal("6000"),
        amount_base=Decimal("6000"),
        realized_pnl_base=Decimal("1000"),
    )
    stmt = make_statement(
        account_label="ibkr_individual",
        start=date(2026, 1, 1),
        end=date(2026, 5, 22),
        frequency="A",
        transactions=(in_window_trade, out_of_window_trade),
    ).model_copy(
        update={
            "per_symbol_pnl_base": {"NBIS": Decimal("5000")},
            "per_symbol_pnl_includes_unrealized": True,
        }
    )
    included, excluded = filter_statements_to_window(
        [stmt], start_date=date(2022, 10, 1), end_date=date(2026, 4, 30)
    )
    assert len(included) == 1
    clipped = included[0]
    assert clipped.transactions == (in_window_trade,)
    # Unrealized portion can't be split off, so the full-period aggregate
    # is kept on the clipped statement.
    assert clipped.per_symbol_pnl_base == {"NBIS": Decimal("5000")}
    assert len(excluded) == 1
    _, note = excluded[0]
    assert "kept as full-period aggregate" in note
    assert "after audit end 2026-04-30" in note


def test_partial_overlap_drops_out_of_window_holdings_snapshot() -> None:
    """A holdings snapshot whose ``period_end`` is past the audit end is
    dropped because it can't be the audit's final snapshot."""
    holding = _holding("AAPL", date(2026, 5, 22))
    stmt = make_statement(
        account_label="ibkr_individual",
        start=date(2026, 1, 1),
        end=date(2026, 5, 22),
        frequency="A",
    ).model_copy(update={"holdings": (holding,)})
    included, _excluded = filter_statements_to_window(
        [stmt], start_date=date(2022, 10, 1), end_date=date(2026, 4, 30)
    )
    assert len(included) == 1
    clipped = included[0]
    assert clipped.holdings == ()


def test_invalid_window_raises() -> None:
    with pytest.raises(ValueError, match="after end"):
        filter_statements_to_window([], start_date=date(2026, 4, 30), end_date=date(2022, 10, 1))


def test_empty_input_returns_empty() -> None:
    included, excluded = filter_statements_to_window(
        [], start_date=date(2022, 1, 1), end_date=date(2026, 12, 31)
    )
    assert included == []
    assert excluded == []
