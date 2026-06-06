from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.analytics.fx_attribution import (
    allocate_cross_term_to_fx,
    cash_fx_attribution,
)
from stock_portfolio_auditor.domain.models import CashBalance
from tests.factories import make_statement


def test_allocate_cross_term_to_fx() -> None:
    assert allocate_cross_term_to_fx(0.10, 0.05) == pytest.approx(0.055)


def test_cash_fx_attribution_uses_statement_cash_balances() -> None:
    statement = make_statement(
        start=date(2024, 1, 1), end=date(2024, 1, 31), frequency="M"
    ).model_copy(
        update={
            "cash_balances": (
                CashBalance(
                    currency="HKD",
                    starting=Decimal("100"),
                    ending=Decimal("200"),
                    fx_translation_pnl=Decimal("5"),
                ),
            )
        }
    )
    rows = cash_fx_attribution([statement], average_nav=1000)
    assert rows[0].currency == "HKD"
    assert rows[0].contribution_bps == pytest.approx(50)
