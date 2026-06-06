# SPDX-License-Identifier: MIT
"""Return calculations: TWR, MWR-IRR, and MWR-Dietz."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pyxirr

from stock_portfolio_auditor.domain.models import Statement, Transaction


@dataclass(frozen=True, slots=True)
class PeriodReturn:
    """One sub-period return observation."""

    start: date
    end: date
    beginning_value: float
    ending_value: float
    external_cash_flow: float
    return_value: float


def modified_dietz_return(
    beginning_value: float,
    ending_value: float,
    cash_flows: list[tuple[date, float]],
    start: date,
    end: date,
) -> float:
    """Compute Modified Dietz return for one period."""
    days = max((end - start).days, 1)
    net_cf = sum(amount for _, amount in cash_flows)
    weighted_cf = 0.0
    for flow_date, amount in cash_flows:
        weight = max((end - flow_date).days, 0) / days
        weighted_cf += amount * weight
    denominator = beginning_value + weighted_cf
    if np.isclose(denominator, 0.0):
        return 0.0
    return (ending_value - beginning_value - net_cf) / denominator


def time_weighted_return(period_returns: list[float]) -> float:
    """Chain-link sub-period returns."""
    if not period_returns:
        return 0.0
    return float(np.prod([1.0 + value for value in period_returns]) - 1.0)


def monthly_twr(statements: list[Statement]) -> tuple[float, tuple[PeriodReturn, ...]]:
    """Compute statement-period TWR from beginning/ending NAV and external flows."""
    ordered = sorted(statements, key=lambda stmt: (stmt.period_start, stmt.period_end))
    period_returns: list[PeriodReturn] = []
    for statement in ordered:
        flows = _external_flows(
            statement.transactions, statement.period_start, statement.period_end
        )
        return_value = modified_dietz_return(
            float(statement.beginning_value_base),
            float(statement.ending_value_base),
            flows,
            statement.period_start,
            statement.period_end,
        )
        period_returns.append(
            PeriodReturn(
                start=statement.period_start,
                end=statement.period_end,
                beginning_value=float(statement.beginning_value_base),
                ending_value=float(statement.ending_value_base),
                external_cash_flow=sum(amount for _, amount in flows),
                return_value=return_value,
            )
        )
    return time_weighted_return([period.return_value for period in period_returns]), tuple(
        period_returns
    )


def money_weighted_return_irr(statements: list[Statement]) -> float:
    """Compute true XIRR over external cash flows plus ending value.

    Multi-account portfolios are handled by treating each account's earliest
    beginning value as an outflow at its first statement start, each account's
    final ending value as an inflow at its last statement end, and all external
    transactions in between as the actual cash flows.
    """
    if not statements:
        return 0.0
    by_account: dict[str, list[Statement]] = {}
    for stmt in statements:
        by_account.setdefault(stmt.account_label, []).append(stmt)

    flows: dict[date, float] = {}
    for stmts in by_account.values():
        stmts_sorted = sorted(stmts, key=lambda s: (s.period_start, s.period_end))
        first = stmts_sorted[0]
        last = stmts_sorted[-1]
        flows[first.period_start] = flows.get(first.period_start, 0.0) - float(
            first.beginning_value_base
        )
        for stmt in stmts_sorted:
            for txn in stmt.transactions:
                if txn.is_external_cash_flow:
                    flows[txn.trade_date] = flows.get(txn.trade_date, 0.0) - float(txn.amount_base)
        flows[last.period_end] = flows.get(last.period_end, 0.0) + float(last.ending_value_base)
    try:
        result = pyxirr.xirr(list(flows.items()))
        return 0.0 if result is None else float(result)
    except Exception:
        return 0.0


def money_weighted_return_dietz(statements: list[Statement]) -> float:
    """Compute whole-period Modified Dietz, matching IBKR's MWR convention."""
    if not statements:
        return 0.0
    by_account: dict[str, list[Statement]] = {}
    for stmt in statements:
        by_account.setdefault(stmt.account_label, []).append(stmt)

    start = min(stmt.period_start for stmt in statements)
    end = max(stmt.period_end for stmt in statements)
    beginning = sum(
        float(sorted(stmts, key=lambda s: s.period_start)[0].beginning_value_base)
        for stmts in by_account.values()
    )
    ending = sum(
        float(sorted(stmts, key=lambda s: s.period_end)[-1].ending_value_base)
        for stmts in by_account.values()
    )
    flows = [
        (txn.trade_date, float(txn.amount_base))
        for stmts in by_account.values()
        for stmt in stmts
        for txn in stmt.transactions
        if txn.is_external_cash_flow
    ]
    return modified_dietz_return(beginning, ending, flows, start, end)


def annualize_return(
    total_return: float, start: date, end: date, *, partial_year: bool = False
) -> float:
    """Annualize return unless this is a partial-year/YTD row."""
    if partial_year:
        return total_return
    days = max((end - start).days, 1)
    return float((1.0 + total_return) ** (365.0 / days) - 1.0)


def _external_flows(
    transactions: tuple[Transaction, ...], start: date, end: date
) -> list[tuple[date, float]]:
    return [
        (txn.trade_date, float(txn.amount_base))
        for txn in transactions
        if txn.is_external_cash_flow and start <= txn.trade_date <= end
    ]
