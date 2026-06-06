# SPDX-License-Identifier: MIT
"""Unit tests for the per-underlying option-income aggregator."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.analytics.option_income import (
    OptionIncomeRow,
    option_income_by_underlying,
    total_option_income,
)
from stock_portfolio_auditor.domain.models import AssetKind
from tests.factories import make_statement, make_transaction


def _option(symbol: str, amount: Decimal):
    return make_transaction(date(2025, 3, 10), amount).model_copy(
        update={
            "symbol": symbol,
            "kind": AssetKind.OPTION,
        }
    )


def test_premium_received_and_paid_aggregate_per_underlying() -> None:
    """Schwab option txns use ``_OPT_<UNDERLYING>`` -- the prefix is stripped
    to give a clean per-underlying ticker. Premium received aggregates into
    ``premium_received``; close costs aggregate into ``premium_paid``.
    """
    txns = (
        _option("_OPT_JXN", Decimal("525")),     # open short, premium received
        _option("_OPT_JXN", Decimal("-120")),    # buy to close
        _option("_OPT_DAL", Decimal("596.67")),  # open short, premium received
    )
    stmt = make_statement(
        start=date(2025, 3, 1), end=date(2025, 3, 31), frequency="M", transactions=txns
    )
    rows = option_income_by_underlying([stmt])
    by_underlying = {row.underlying: row for row in rows}
    assert by_underlying["JXN"].premium_received == 525
    assert by_underlying["JXN"].premium_paid == -120
    assert by_underlying["JXN"].net == 405
    assert by_underlying["DAL"].premium_received == 596.67
    assert by_underlying["DAL"].premium_paid == 0
    assert by_underlying["DAL"].net == 596.67


def test_ibkr_option_contract_symbol_is_collapsed_to_underlying() -> None:
    """IBKR option Trades use the full contract identifier (``JXN 21MAR25 80 P``);
    the first whitespace-separated token is the underlying.
    """
    txns = (
        _option("JXN 21MAR25 80 P", Decimal("525")),
        _option("JXN 16MAY25 80 P", Decimal("1540")),
        _option("TLN 17APR25 200 P", Decimal("-1972.90")),
    )
    stmt = make_statement(
        start=date(2025, 3, 1), end=date(2025, 3, 31), frequency="M", transactions=txns
    )
    rows = option_income_by_underlying([stmt])
    by_underlying = {row.underlying: row.net for row in rows}
    assert by_underlying["JXN"] == 525 + 1540
    assert by_underlying["TLN"] == -1972.90


def test_assignment_does_not_emit_option_transaction_so_net_stays_at_premium() -> None:
    """An option assignment is modelled as the option transaction simply
    being absent (no buy-to-close row, no closing transaction); the
    premium-received row from the open is the only cash event on the
    option leg. The net stays at the gross premium received.
    """
    txns = (
        # Open short put: premium received.
        _option("_OPT_DAL", Decimal("596.67")),
        # Assignment: no option-leg transaction emitted by the parser.
        # (The stock side gets a separate row at the strike price.)
    )
    stmt = make_statement(
        start=date(2025, 3, 1), end=date(2025, 3, 31), frequency="M", transactions=txns
    )
    rows = option_income_by_underlying([stmt])
    assert rows == [
        OptionIncomeRow(underlying="DAL", premium_received=596.67, premium_paid=0, net=596.67)
    ]


def test_sort_order_descending_by_net() -> None:
    txns = (
        _option("_OPT_AAA", Decimal("100")),
        _option("_OPT_BBB", Decimal("500")),
        _option("_OPT_CCC", Decimal("-50")),
    )
    stmt = make_statement(
        start=date(2025, 1, 1), end=date(2025, 1, 31), frequency="M", transactions=txns
    )
    rows = option_income_by_underlying([stmt])
    assert [r.underlying for r in rows] == ["BBB", "AAA", "CCC"]
    assert total_option_income(rows) == 100 + 500 - 50


def test_non_option_transactions_ignored() -> None:
    txns = (
        # Equity buy / sell / dividend / interest -- none of these should
        # contribute to the option income table.
        make_transaction(date(2025, 1, 5), Decimal("1000")).model_copy(
            update={"symbol": "AAPL"}
        ),
        # Even one with a leading underscore that's not an option pseudo-symbol.
        make_transaction(date(2025, 1, 5), Decimal("100")).model_copy(
            update={"symbol": "_FX_EUR", "kind": AssetKind.FX}
        ),
    )
    stmt = make_statement(
        start=date(2025, 1, 1), end=date(2025, 1, 31), frequency="M", transactions=txns
    )
    assert option_income_by_underlying([stmt]) == []
