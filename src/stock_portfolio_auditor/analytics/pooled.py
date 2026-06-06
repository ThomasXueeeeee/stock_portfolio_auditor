# SPDX-License-Identifier: MIT
"""Pooled portfolio calculations across multiple accounts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import pandas as pd

from stock_portfolio_auditor.analytics.returns import modified_dietz_return
from stock_portfolio_auditor.domain.models import Statement


@dataclass(frozen=True, slots=True)
class PooledPeriod:
    """One pooled portfolio month."""

    period_end: date
    beginning_value: float
    ending_value: float
    external_cash_flow: float
    period_return: float


def _months_between(start: date, end: date) -> int:
    return max((end.year - start.year) * 12 + (end.month - start.month) + 1, 1)


def _month_end_index(start: date, end: date) -> pd.DatetimeIndex:
    return pd.date_range(
        start=pd.Timestamp(start.year, start.month, 1),
        end=pd.Timestamp(end.year, end.month, 1) + pd.offsets.MonthEnd(0),
        freq="ME",
    )


def audit_end_date(statements: list[Statement]) -> date:
    """Return the latest month-end where every account has statement coverage.

    Multi-month statements (e.g. IBKR annual) are considered to cover every
    calendar month from period_start to period_end. The result is the latest
    month-end that is in every account's coverage set.
    """
    by_account: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        by_account[stmt.account_label].append(stmt)

    per_account_last: list[date] = []
    for stmts in by_account.values():
        last = max(stmt.period_end for stmt in stmts)
        end_month = pd.Timestamp(last.year, last.month, 1) + pd.offsets.MonthEnd(0)
        per_account_last.append(end_month.date())
    return min(per_account_last) if per_account_last else max(s.period_end for s in statements)


def per_account_monthly_returns(
    statements: list[Statement], *, end: date | None = None
) -> dict[str, pd.Series]:
    """Build a monthly return series per account, spreading multi-month periods.

    Each statement's Modified Dietz return is converted to a per-month equivalent
    and stamped on every calendar month it covers. Monthly statements map 1:1.
    """
    by_account: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        by_account[stmt.account_label].append(stmt)
    for label in by_account:
        by_account[label].sort(key=lambda stmt: stmt.period_start)

    cap = end

    result: dict[str, pd.Series] = {}
    for account, account_stmts in by_account.items():
        first_start = account_stmts[0].period_start
        last_end = account_stmts[-1].period_end
        if cap is not None and last_end > cap:
            last_end = cap
        index = _month_end_index(first_start, last_end)
        monthly: dict[pd.Timestamp, float] = {}
        for stmt in account_stmts:
            flows = [
                (txn.trade_date, float(txn.amount_base))
                for txn in stmt.transactions
                if txn.is_external_cash_flow
            ]
            period_return = modified_dietz_return(
                float(stmt.beginning_value_base),
                float(stmt.ending_value_base),
                flows,
                stmt.period_start,
                stmt.period_end,
            )
            n_months = _months_between(stmt.period_start, stmt.period_end)
            monthly_return = (1.0 + period_return) ** (1.0 / n_months) - 1.0
            cursor = pd.Timestamp(stmt.period_start.year, stmt.period_start.month, 1)
            target_end = pd.Timestamp(stmt.period_end.year, stmt.period_end.month, 1)
            while cursor <= target_end:
                month_end = cursor + pd.offsets.MonthEnd(0)
                monthly[month_end] = monthly_return
                cursor = (cursor + pd.offsets.MonthBegin(1)).normalize()
        series = pd.Series({key: monthly.get(key, 0.0) for key in index}, dtype="float64")
        series.index = index
        result[account] = series
    return result


def per_account_monthly_values(
    statements: list[Statement], *, end: date | None = None
) -> dict[str, pd.Series]:
    """Per-account month-end NAV series, with linear interpolation across periods.

    For each statement we record its beginning value at the start of its period
    and its ending value at the end. Months in between are filled by reinvesting
    the per-month equivalent return on the beginning value, which keeps the line
    visually smooth without introducing fictitious cash flows.
    """
    by_account: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        by_account[stmt.account_label].append(stmt)
    for label in by_account:
        by_account[label].sort(key=lambda stmt: stmt.period_start)

    out: dict[str, pd.Series] = {}
    for account, account_stmts in by_account.items():
        last_end = account_stmts[-1].period_end
        if end is not None and last_end > end:
            last_end = end
        index = _month_end_index(account_stmts[0].period_start, last_end)
        values = pd.Series(0.0, index=index, dtype="float64")
        for stmt in account_stmts:
            stmt_end = stmt.period_end if end is None else min(stmt.period_end, end)
            if stmt_end < stmt.period_start:
                continue
            stmt_index = _month_end_index(stmt.period_start, stmt_end)
            if len(stmt_index) == 0:
                continue
            full_index = _month_end_index(stmt.period_start, stmt.period_end)
            n_full = max(len(full_index), 1)
            ending = float(stmt.ending_value_base)
            beginning = float(stmt.beginning_value_base)
            for i, month_end in enumerate(stmt_index, start=1):
                fraction = i / n_full
                values.loc[month_end] = beginning + (ending - beginning) * fraction
        out[account] = values
    return out


def pooled_monthly_periods(
    statements: list[Statement], *, end: date | None = None
) -> list[PooledPeriod]:
    """Build pooled monthly portfolio periods using asset-weighted returns."""
    if not statements:
        return []

    audit_end = end or audit_end_date(statements)
    account_returns = per_account_monthly_returns(statements, end=audit_end)
    account_values = per_account_monthly_values(statements, end=audit_end)
    if not account_returns:
        return []

    start = min(stmt.period_start for stmt in statements)
    index = _month_end_index(start, audit_end)

    aligned_values = pd.DataFrame(
        {
            acct: series.reindex(index).ffill().fillna(0.0)
            for acct, series in account_values.items()
        },
        index=index,
    )
    aligned_returns = pd.DataFrame(
        {acct: series.reindex(index).fillna(0.0) for acct, series in account_returns.items()},
        index=index,
    )

    pooled_value = aligned_values.sum(axis=1)
    beginning_values = pooled_value.shift(1).fillna(0.0)
    if not beginning_values.empty:
        first_beg = 0.0
        for stmts in _by_account(statements).values():
            first_stmt = stmts[0]
            first_month = pd.Timestamp(
                first_stmt.period_start.year, first_stmt.period_start.month, 1
            ) + pd.offsets.MonthEnd(0)
            if first_month == index[0]:
                first_beg += float(first_stmt.beginning_value_base)
        beginning_values.iloc[0] = first_beg

    flows_by_month = _external_flows_by_month(statements, index)

    weights = aligned_values.shift(1).fillna(0.0)
    weight_totals = weights.sum(axis=1).replace(0.0, 1.0)
    weighted_returns = (aligned_returns * weights).sum(axis=1) / weight_totals

    periods: list[PooledPeriod] = []
    for month_end, ret in weighted_returns.items():
        cf = float(flows_by_month.get(month_end, 0.0))
        beg = float(beginning_values.loc[month_end])
        denom = beg + 0.5 * cf
        if denom == 0:
            period_return = float(ret)
        else:
            implied_end = beg * (1.0 + float(ret)) + cf
            period_return = float(ret) if not pd.isna(ret) else 0.0
            del implied_end
        periods.append(
            PooledPeriod(
                period_end=month_end.date(),
                beginning_value=beg,
                ending_value=float(pooled_value.loc[month_end]),
                external_cash_flow=cf,
                period_return=period_return,
            )
        )
    return periods


def cumulative_pooled_twr(periods: list[PooledPeriod]) -> pd.Series:
    """Chain-link pooled period returns into a cumulative TWR series."""
    if not periods:
        return pd.Series(dtype="float64")
    index = pd.to_datetime([period.period_end for period in periods])
    returns = pd.Series([period.period_return for period in periods], index=index)
    return (1.0 + returns).cumprod() - 1.0


def pooled_total_twr(periods: list[PooledPeriod]) -> float:
    """Total pooled portfolio TWR over the full audit window."""
    if not periods:
        return 0.0
    total = 1.0
    for period in periods:
        total *= 1.0 + period.period_return
    return float(total - 1.0)


def pooled_value_series(statements: list[Statement], *, end: date | None = None) -> pd.Series:
    """Pooled portfolio value at each month-end across all accounts."""
    if not statements:
        return pd.Series(dtype="float64")
    audit_end = end or audit_end_date(statements)
    values = per_account_monthly_values(statements, end=audit_end)
    if not values:
        return pd.Series(dtype="float64")
    start = min(stmt.period_start for stmt in statements)
    index = _month_end_index(start, audit_end)
    aligned = pd.DataFrame(
        {acct: series.reindex(index).ffill().fillna(0.0) for acct, series in values.items()},
        index=index,
    )
    return aligned.sum(axis=1)


def pooled_yearly_breakdown(periods: list[PooledPeriod]) -> list[dict[str, float]]:
    """Group pooled monthly periods into per-year stats."""
    if not periods:
        return []

    by_year: dict[int, list[PooledPeriod]] = defaultdict(list)
    for period in periods:
        by_year[period.period_end.year].append(period)

    rows: list[dict[str, float]] = []
    for year, year_periods in sorted(by_year.items()):
        twr = 1.0
        for period in year_periods:
            twr *= 1.0 + period.period_return
        twr -= 1.0
        first = year_periods[0]
        last = year_periods[-1]
        net_cf = sum(period.external_cash_flow for period in year_periods)
        rows.append(
            {
                "year": float(year),
                "twr": twr,
                "beginning_value": first.beginning_value,
                "ending_value": last.ending_value,
                "external_cash_flow": net_cf,
                "pnl": last.ending_value - first.beginning_value - net_cf,
            }
        )
    return rows


def _by_account(statements: list[Statement]) -> dict[str, list[Statement]]:
    out: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        out[stmt.account_label].append(stmt)
    for label in out:
        out[label].sort(key=lambda stmt: stmt.period_start)
    return out


def _external_flows_by_month(
    statements: list[Statement], index: pd.DatetimeIndex
) -> dict[pd.Timestamp, float]:
    flows_by_month: dict[pd.Timestamp, float] = defaultdict(float)
    for stmt in statements:
        for txn in stmt.transactions:
            if not txn.is_external_cash_flow:
                continue
            month_end = pd.Timestamp(
                txn.trade_date.year, txn.trade_date.month, 1
            ) + pd.offsets.MonthEnd(0)
            flows_by_month[month_end] += float(txn.amount_base)
    return {month: amount for month, amount in flows_by_month.items() if month in index}


# Backwards-compatible alias (legacy module API).
def pooled_period_returns(statements: list[Statement]) -> list[PooledPeriod]:
    """Alias for ``pooled_monthly_periods`` for backwards compatibility."""
    return pooled_monthly_periods(statements)
