# SPDX-License-Identifier: MIT
"""NAV time series helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from stock_portfolio_auditor.domain.models import Statement


@dataclass(frozen=True, slots=True)
class NavPoint:
    """A statement-level NAV observation."""

    date: date
    value: float
    account_label: str


def monthly_nav(statements: list[Statement]) -> pd.Series:
    """Build month-end NAV from statement ending values."""
    points = [
        NavPoint(stmt.period_end, float(stmt.ending_value_base), stmt.account_label)
        for stmt in statements
    ]
    if not points:
        return pd.Series(dtype="float64")
    frame = pd.DataFrame(
        {
            "date": [point.date for point in points],
            "value": [point.value for point in points],
        }
    )
    series = frame.groupby("date", sort=True)["value"].sum()
    series.index = pd.to_datetime(series.index)
    return series.astype("float64")


def beginning_nav(statements: list[Statement]) -> float:
    """Return summed beginning NAV of the earliest statement per account."""
    earliest: dict[str, Statement] = {}
    for statement in statements:
        existing = earliest.get(statement.account_label)
        if existing is None or statement.period_start < existing.period_start:
            earliest[statement.account_label] = statement
    return sum(float(statement.beginning_value_base) for statement in earliest.values())
