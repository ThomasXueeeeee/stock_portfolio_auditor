from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.analytics.attribution import contribution_bps, pnl_breakdown
from stock_portfolio_auditor.domain.models import IncomeBucket
from tests.factories import make_statement, make_transaction


def test_pnl_breakdown_extracts_dividend_and_lending_income() -> None:
    div = make_transaction(date(2024, 1, 5), Decimal("10")).model_copy(
        update={"action": "div", "income_bucket": IncomeBucket.CASH_DIVIDEND}
    )
    lend = make_transaction(date(2024, 1, 6), Decimal("2")).model_copy(
        update={"action": "lend_int", "income_bucket": IncomeBucket.LENDING_INCOME}
    )
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
        transactions=(div, lend),
    ).model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("120")}
    )

    breakdown = pnl_breakdown([statement])

    assert breakdown.dividends == 10
    assert breakdown.lending == 2
    assert breakdown.price == 8
    assert contribution_bps(breakdown, 100)["dividends"] == pytest.approx(1000)


def test_pnl_breakdown_does_not_treat_principal_as_cost_drag_when_charges_zero() -> None:
    """A cost_bucket-tagged row with zero commission/fees must contribute zero
    cost drag, not the full transaction principal. Locks in the removal of the
    ``or abs(amount)`` fallback that would otherwise eat the principal.
    """
    from stock_portfolio_auditor.domain.models import CostBucket

    txn = make_transaction(date(2024, 1, 5), Decimal("12345")).model_copy(
        update={
            "action": "fee",
            "cost_bucket": CostBucket.OTHER_COST,
            "commission": Decimal("0"),
            "fees": Decimal("0"),
        }
    )
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
        transactions=(txn,),
    ).model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("100")}
    )

    breakdown = pnl_breakdown([statement])
    assert breakdown.tcost == 0


def test_pnl_breakdown_excludes_external_cash_flows_from_price_residual() -> None:
    """A deposit must not inflate the Price bucket — it's not investment P&L."""
    deposit = make_transaction(date(2024, 1, 10), Decimal("50")).model_copy(
        update={"action": "contrib", "is_external_cash_flow": True}
    )
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
        transactions=(deposit,),
    ).model_copy(
        # NAV moved +60: 50 of it is the deposit, only 10 is real PnL.
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("160")}
    )

    breakdown = pnl_breakdown([statement])

    assert breakdown.price == pytest.approx(10.0)
    assert breakdown.total == pytest.approx(10.0)
