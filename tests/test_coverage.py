from __future__ import annotations

from datetime import date

import pytest

from stock_portfolio_auditor.domain.errors import CoverageGapError
from stock_portfolio_auditor.ingestion.coverage import assert_strict_coverage, covered_months
from tests.factories import make_statement


def test_quarterly_statement_covers_three_months() -> None:
    statement = make_statement(start=date(2024, 1, 1), end=date(2024, 3, 31), frequency="Q")
    assert covered_months(statement) == {(2024, 1), (2024, 2), (2024, 3)}


def test_partial_month_is_not_covered() -> None:
    statement = make_statement(start=date(2024, 1, 15), end=date(2024, 3, 31), frequency="Q")
    assert covered_months(statement) == {(2024, 2), (2024, 3)}


def test_strict_coverage_raises_gap() -> None:
    statements = [
        make_statement(start=date(2024, 1, 1), end=date(2024, 1, 31), frequency="M"),
        make_statement(start=date(2024, 3, 1), end=date(2024, 3, 31), frequency="M"),
    ]
    with pytest.raises(CoverageGapError):
        assert_strict_coverage(statements)


def test_strict_coverage_accepts_quarterly_gap_fill() -> None:
    statements = [
        make_statement(start=date(2024, 1, 1), end=date(2024, 3, 31), frequency="Q"),
        make_statement(start=date(2024, 4, 1), end=date(2024, 4, 30), frequency="M"),
    ]
    assert_strict_coverage(statements)
