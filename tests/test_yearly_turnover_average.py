# SPDX-License-Identifier: MIT
"""Unit tests for the multi-year average-annual-turnover helper.

The Total row in the yearly summary table reports a "average annual
turnover" computed as ``sum(per_year_turnovers) / years_in_window``
rather than the raw SEC formula applied to the cumulative window. The
docstring on :func:`_average_annual_turnover` explains why; this test
file pins the math.
"""

from __future__ import annotations

from datetime import date

import pytest

from stock_portfolio_auditor.reporting.build_report import _average_annual_turnover
from stock_portfolio_auditor.reporting.report_schema import YearlySummaryRow


def _row(label: str, turnover: float) -> YearlySummaryRow:
    return YearlySummaryRow(label=label, twr=0.0, mwr_irr=0.0, turnover=turnover)


def test_average_annual_turnover_sums_per_year_ratios_then_divides_by_years() -> None:
    """0% + 44% + 261% + 152% + 38% over 1308 days / 365 = 3.58 years -> ~138%."""
    rows = [
        _row("2022 (Oct-Dec)", 0.00),
        _row("2023", 0.44),
        _row("2024", 2.61),
        _row("2025", 1.52),
        _row("2026 (Jan-Apr)", 0.38),
        _row("Total", 0.0),  # placeholder; should be excluded
    ]
    avg = _average_annual_turnover(rows, date(2022, 10, 1), date(2026, 4, 30))
    total_days = (date(2026, 4, 30) - date(2022, 10, 1)).days + 1
    years = total_days / 365.0
    expected = (0.00 + 0.44 + 2.61 + 1.52 + 0.38) / years
    assert avg == pytest.approx(expected, rel=1e-9)


def test_average_annual_turnover_excludes_existing_total_row() -> None:
    """If the Total row already has a non-zero turnover it must not be summed in."""
    rows = [
        _row("2023", 0.50),
        _row("Total", 9.99),  # stale value must not contribute
    ]
    avg = _average_annual_turnover(rows, date(2023, 1, 1), date(2023, 12, 31))
    assert avg == pytest.approx(0.50, rel=1e-3)


def test_average_annual_turnover_handles_single_year_window() -> None:
    """A clean non-leap-year window returns the per-year ratio itself."""
    rows = [_row("2023", 0.70)]
    avg = _average_annual_turnover(rows, date(2023, 1, 1), date(2023, 12, 31))
    assert avg == pytest.approx(0.70, rel=1e-6)


def test_average_annual_turnover_returns_zero_for_empty_input() -> None:
    assert _average_annual_turnover([], date(2024, 1, 1), date(2024, 12, 31)) == 0.0
