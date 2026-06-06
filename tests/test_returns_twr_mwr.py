from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.analytics.returns import (
    annualize_return,
    modified_dietz_return,
    money_weighted_return_dietz,
    money_weighted_return_irr,
    monthly_twr,
    time_weighted_return,
)
from tests.factories import make_statement, make_transaction


def test_modified_dietz_no_cash_flow() -> None:
    assert modified_dietz_return(100, 110, [], date(2024, 1, 1), date(2024, 1, 31)) == 0.10


def test_time_weighted_return_chain_links() -> None:
    assert time_weighted_return([0.10, -0.10]) == pytest.approx(-0.01)


def test_monthly_twr_excludes_external_cash_flow() -> None:
    contribution = make_transaction(date(2024, 1, 15), Decimal("50"))
    contribution = contribution.model_copy(
        update={"is_external_cash_flow": True, "action": "contrib"}
    )
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
        transactions=(contribution,),
    ).model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("160")}
    )

    twr, periods = monthly_twr([statement])

    assert len(periods) == 1
    assert periods[0].external_cash_flow == 50
    assert twr < 0.10


def test_mwr_irr_and_dietz_return_float() -> None:
    contribution = make_transaction(date(2024, 1, 15), Decimal("50"))
    contribution = contribution.model_copy(
        update={"is_external_cash_flow": True, "action": "contrib"}
    )
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
        transactions=(contribution,),
    ).model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("170")}
    )

    assert isinstance(money_weighted_return_irr([statement]), float)
    assert isinstance(money_weighted_return_dietz([statement]), float)


def test_partial_year_annualization_is_noop() -> None:
    assert annualize_return(0.25, date(2024, 1, 1), date(2024, 3, 31), partial_year=True) == 0.25
