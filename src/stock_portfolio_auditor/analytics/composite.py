# SPDX-License-Identifier: MIT
"""Composite account aggregation methods."""

from __future__ import annotations

from collections import defaultdict

from stock_portfolio_auditor.domain.models import Statement


def pooled_nav_statements(statements: list[Statement]) -> list[Statement]:
    """Return statements unchanged for pooled calculations.

    Analytics functions that sum by date can consume all account statements
    directly. This function exists as a named policy hook for the GUI/report.
    """
    return statements


def beginning_nav_weights(statements: list[Statement]) -> dict[str, float]:
    """Beginning NAV weights by account."""
    beginning_by_account: dict[str, float] = defaultdict(float)
    for statement in statements:
        beginning_by_account[statement.account_label] += float(statement.beginning_value_base)
    total = sum(beginning_by_account.values())
    if total == 0:
        return {account: 0.0 for account in beginning_by_account}
    return {account: value / total for account, value in beginning_by_account.items()}
