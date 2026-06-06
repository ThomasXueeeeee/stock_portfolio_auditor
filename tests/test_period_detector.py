from __future__ import annotations

from datetime import date

import pytest

from stock_portfolio_auditor.domain.errors import PeriodDetectionError
from stock_portfolio_auditor.ingestion.period_detector import (
    StatementPeriod,
    detect_period_from_filename,
    detect_period_from_text,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Statement Period March 1 - March 31, 2024",
            StatementPeriod(date(2024, 3, 1), date(2024, 3, 31), "M"),
        ),
        (
            'Activity Statement\nPeriod,"January 1, 2023 - December 31, 2023"',
            StatementPeriod(date(2023, 1, 1), date(2023, 12, 31), "A"),
        ),
        (
            "Investment Report May 1, 2015-May 31, 2015",
            StatementPeriod(date(2015, 5, 1), date(2015, 5, 31), "M"),
        ),
        (
            "Statement Period January 1, 2024 to March 31, 2024",
            StatementPeriod(date(2024, 1, 1), date(2024, 3, 31), "Q"),
        ),
    ],
)
def test_detect_period_from_text(text: str, expected: StatementPeriod) -> None:
    assert detect_period_from_text(text) == expected


def test_detect_period_from_filename_monthly() -> None:
    assert detect_period_from_filename("Brokerage Statement_2024-03-31_755.PDF") == StatementPeriod(
        date(2024, 3, 1), date(2024, 3, 31), "M"
    )


def test_detect_period_from_filename_annual_ibkr() -> None:
    assert detect_period_from_filename("U1234567_2023_2023.csv") == StatementPeriod(
        date(2023, 1, 1), date(2023, 12, 31), "A"
    )


def test_detect_period_from_text_raises_for_unknown_text() -> None:
    with pytest.raises(PeriodDetectionError):
        detect_period_from_text("No period here")
