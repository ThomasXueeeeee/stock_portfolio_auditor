from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.analytics.tcost import cost_ledger, total_cost_drag_bps
from stock_portfolio_auditor.domain.models import CostBucket
from tests.factories import make_statement, make_transaction


def test_cost_ledger_groups_by_bucket() -> None:
    fee = make_transaction(date(2024, 1, 2), Decimal("-5")).model_copy(
        update={"action": "fee", "fees": Decimal("5"), "cost_bucket": CostBucket.REGULATORY_FEE}
    )
    statement = make_statement(
        start=date(2024, 1, 1), end=date(2024, 1, 31), frequency="M", transactions=(fee,)
    )

    rows = cost_ledger([statement], average_nav=1000)

    assert rows[0].bucket is CostBucket.REGULATORY_FEE
    assert rows[0].amount == 5
    assert rows[0].bps == pytest.approx(50)
    assert total_cost_drag_bps(rows) == pytest.approx(50)
