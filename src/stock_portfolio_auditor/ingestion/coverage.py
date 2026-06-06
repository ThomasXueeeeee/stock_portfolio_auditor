# SPDX-License-Identifier: MIT
"""Strict statement coverage checks."""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from stock_portfolio_auditor.domain.errors import CoverageGapError
from stock_portfolio_auditor.domain.models import Statement

MonthKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class CoverageCell:
    """Coverage status for one account-month."""

    account_label: str
    year: int
    month: int
    statement_count: int
    best_frequency: str | None

    @property
    def covered(self) -> bool:
        """True if one or more statements cover the full month."""
        return self.statement_count > 0


def covered_months(statement: Statement) -> set[MonthKey]:
    """Return calendar months fully covered by a statement period."""
    months: set[MonthKey] = set()
    current = date(statement.period_start.year, statement.period_start.month, 1)
    end_month = date(statement.period_end.year, statement.period_end.month, 1)
    while current <= end_month:
        last_day = calendar.monthrange(current.year, current.month)[1]
        month_start = current
        month_end = date(current.year, current.month, last_day)
        if statement.period_start <= month_start and statement.period_end >= month_end:
            months.add((current.year, current.month))
        current = _next_month(current)
    return months


def coverage_matrix(statements: Iterable[Statement]) -> list[CoverageCell]:
    """Build a per-account contiguous coverage matrix."""
    by_account: dict[str, list[Statement]] = {}
    for statement in statements:
        by_account.setdefault(statement.account_label, []).append(statement)

    cells: list[CoverageCell] = []
    for account, account_statements in by_account.items():
        all_months = sorted(
            {month for stmt in account_statements for month in covered_months(stmt)}
        )
        if not all_months:
            continue
        for year, month in _month_range(all_months[0], all_months[-1]):
            covering = [
                stmt for stmt in account_statements if (year, month) in covered_months(stmt)
            ]
            best_frequency = min(
                (stmt.frequency for stmt in covering), key=_frequency_rank, default=None
            )
            cells.append(
                CoverageCell(
                    account_label=account,
                    year=year,
                    month=month,
                    statement_count=len(covering),
                    best_frequency=best_frequency,
                )
            )
    return cells


def assert_strict_coverage(statements: Iterable[Statement]) -> None:
    """Raise CoverageGapError when any month in the account range is uncovered."""
    gaps = [cell for cell in coverage_matrix(statements) if not cell.covered]
    if gaps:
        raise CoverageGapError(
            "Missing statement coverage",
            {
                "gaps": [
                    {"account": cell.account_label, "year": cell.year, "month": cell.month}
                    for cell in gaps
                ]
            },
        )


def _month_range(start: MonthKey, end: MonthKey) -> list[MonthKey]:
    current = date(start[0], start[1], 1)
    final = date(end[0], end[1], 1)
    out: list[MonthKey] = []
    while current <= final:
        out.append((current.year, current.month))
        current = _next_month(current)
    return out


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _frequency_rank(freq: str) -> int:
    return {"M": 0, "Q": 1, "A": 2}.get(freq, 99)
