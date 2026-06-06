# SPDX-License-Identifier: MIT
"""Unit tests for per-position contribution analytics."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.analytics.contribution import (
    position_contributions,
    top_contributors,
    top_detractors,
    total_attributed_pnl,
)
from stock_portfolio_auditor.domain.models import AssetKind, Holding, Statement


def _holding(
    symbol: str,
    *,
    quantity: str = "100",
    mv: str = "10000",
    cb: str = "8000",
    as_of: date,
    description: str | None = None,
) -> Holding:
    return Holding(
        symbol=symbol,
        description=description or symbol,
        kind=AssetKind.EQUITY,
        currency="USD",
        quantity=Decimal(quantity),
        market_value_local=Decimal(mv),
        market_value_base=Decimal(mv),
        cost_basis_local=Decimal(cb),
        cost_basis_base=Decimal(cb),
        as_of=as_of,
    )


def _stmt(
    account: str,
    *,
    period_start: date,
    period_end: date,
    holdings: tuple[Holding, ...],
) -> Statement:
    return Statement(
        account_label=account,
        broker="schwab",
        base_currency="USD",
        period_start=period_start,
        period_end=period_end,
        frequency="M",
        beginning_value_base=Decimal("0"),
        ending_value_base=Decimal("0"),
        holdings=holdings,
        parser_version=2,
        raw_text_hash="x",
    )


def test_position_contribution_two_month_window() -> None:
    """A position whose market value grows faster than cost basis contributes positively."""
    stmts = [
        _stmt(
            "schwab",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 31),
            holdings=(_holding("AAPL", mv="10000", cb="8000", as_of=date(2024, 1, 31)),),
        ),
        _stmt(
            "schwab",
            period_start=date(2024, 2, 1),
            period_end=date(2024, 2, 29),
            holdings=(_holding("AAPL", mv="12000", cb="8000", as_of=date(2024, 2, 29)),),
        ),
    ]
    rows = position_contributions(stmts, total_dollar_pnl=4000.0)
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "AAPL"
    # Start (Jan): unrealized = 10000 - 8000 = 2000.
    # End (Feb): unrealized = 12000 - 8000 = 4000.
    # Dollar PnL across the two-month window = 4000 - 0 (first month seed) = 4000.
    # The Jan snapshot's 2000 is counted as the period-0 contribution (no prior).
    assert row.dollar_pnl == 4000.0
    assert row.contribution_pct == 1.0  # 4000 / 4000 (total PnL) = 100% of attributed PnL


def test_top_contributors_and_detractors_split() -> None:
    """Positions are partitioned into positive and negative contributors."""
    stmts = [
        _stmt(
            "schwab",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 31),
            holdings=(
                _holding("AAPL", mv="10000", cb="8000", as_of=date(2024, 1, 31)),
                _holding("NVDA", mv="5000", cb="5000", as_of=date(2024, 1, 31)),
                _holding("TSLA", mv="5000", cb="6000", as_of=date(2024, 1, 31)),
            ),
        ),
        _stmt(
            "schwab",
            period_start=date(2024, 2, 1),
            period_end=date(2024, 2, 29),
            holdings=(
                _holding("AAPL", mv="15000", cb="8000", as_of=date(2024, 2, 29)),
                _holding("NVDA", mv="8000", cb="5000", as_of=date(2024, 2, 29)),
                _holding("TSLA", mv="3000", cb="6000", as_of=date(2024, 2, 29)),
            ),
        ),
    ]
    rows = position_contributions(stmts)
    contributors = top_contributors(rows, n=2)
    detractors = top_detractors(rows, n=2)
    assert [c.symbol for c in contributors] == ["AAPL", "NVDA"]
    assert [d.symbol for d in detractors] == ["TSLA"]


def test_position_contribution_aggregates_across_accounts() -> None:
    """Same symbol in two accounts aggregates dollar_pnl and lists both labels."""
    stmts = [
        _stmt(
            "schwab",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 31),
            holdings=(_holding("AAPL", mv="10000", cb="8000", as_of=date(2024, 1, 31)),),
        ),
        _stmt(
            "schwab",
            period_start=date(2024, 2, 1),
            period_end=date(2024, 2, 29),
            holdings=(_holding("AAPL", mv="11000", cb="8000", as_of=date(2024, 2, 29)),),
        ),
        _stmt(
            "ibkr",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 31),
            holdings=(_holding("AAPL", mv="5000", cb="4000", as_of=date(2024, 1, 31)),),
        ),
        _stmt(
            "ibkr",
            period_start=date(2024, 2, 1),
            period_end=date(2024, 2, 29),
            holdings=(_holding("AAPL", mv="6000", cb="4000", as_of=date(2024, 2, 29)),),
        ),
    ]
    rows = position_contributions(stmts, total_dollar_pnl=5000.0)
    assert len(rows) == 1
    assert rows[0].accounts == ("ibkr", "schwab")
    # Schwab: unrealized goes 2000 -> 3000.
    # IBKR: unrealized goes 1000 -> 2000.
    # Total dollar PnL across both = 5000 (first-month baselines included).
    assert rows[0].dollar_pnl == 5000.0
    # Ending MV is total_quantity * latest unit price. Schwab Feb mv=11000 at
    # qty=100 -> $110/share. IBKR Feb mv=6000 at qty=100 -> $60/share. Both are
    # observed at the same period_end so the "latest" is $60 (last to be
    # written wins when dates tie -> account order in dict iteration). Either
    # way, total qty = 200 shares.
    assert rows[0].dollar_pnl == 5000.0


def test_total_attributed_pnl_sums_rows() -> None:
    stmts = [
        _stmt(
            "schwab",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 31),
            holdings=(
                _holding("AAPL", mv="10000", cb="8000", as_of=date(2024, 1, 31)),
                _holding("TSLA", mv="5000", cb="6000", as_of=date(2024, 1, 31)),
            ),
        ),
    ]
    rows = position_contributions(stmts)
    total = total_attributed_pnl(rows)
    # AAPL contributes +2000, TSLA contributes -1000, total = +1000.
    assert total == 1000.0


def test_position_contributions_empty_input_returns_empty_list() -> None:
    assert position_contributions([]) == []
