# SPDX-License-Identifier: MIT
"""Portfolio turnover for fundamental equity strategies.

Turnover answers "how much of the book was traded in the period?" -- a
key reality check for a fundamental strategy that claims to compound
quality businesses. A 10%/year turnover means the average position is
held ~10 years; 200%/year turnover means the strategy is effectively a
trading book regardless of what its marketing material says.

This module reports **monthly two-way turnover** as the building block
that the yearly summary and KPI strip read from:

    monthly_turnover = (buys_month + sells_month) / 2 / avg_NAV_month

where ``avg_NAV_month = (beginning + ending) / 2`` for the month and
``buys_month`` / ``sells_month`` sum the absolute notional of every
buy / sell ``Transaction`` whose ``trade_date`` lands in the month.
Yearly turnover is the sum of the twelve monthly ratios; multi-year
totals divide the sum of yearly ratios by the number of years in the
audit window.

Why two-way (``(buys + sells) / 2``) instead of the SEC's
``min(buys, sells)`` convention used on Form N-1A:

  * The SEC formula is designed to strip out *fund flow* trading:
    when a mutual fund takes a $10M deposit and deploys it into new
    positions, ``min(buys, sells)`` correctly reports zero
    "rotation" even though the manager has done a lot of buying. For
    a personal-portfolio audit there is no fund-flow noise to filter
    out -- the user *is* the investor -- so a quarter that only
    bought stock should show non-zero activity. The two-way formula
    captures one-way deployment as half-turnover, which is the
    correct conceptual weight (buying $X without selling anything is
    "less rotational" than selling $X and buying $Y but it is not
    zero activity).
  * For a pure rotation (sell $X of A, buy $X of B), both formulas
    agree: ``min(X, X) = X`` and ``(X + X) / 2 = X``, so a fund
    actually rotating its book reports the same rate either way.

Computing the ratio per-month and summing -- rather than aggregating
buys, sells and NAV across the year and dividing once -- weights each
month's trading by the *that-month* NAV. A $20K trade on a $100K
early-year portfolio counts as more "rotation" than the same $20K on a
$500K late-year portfolio, which is conceptually right: rotation is
relative to the size of the portfolio at the time the trade happens.

The legacy SEC-min ``portfolio_turnover`` function is kept below for
callers that want the Form N-1A number explicitly; the report
pipeline uses :func:`monthly_two_way_turnover_series` and the helpers
that aggregate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.analytics.pooled import PooledPeriod
from stock_portfolio_auditor.domain.models import AssetKind, Statement

# Asset kinds counted as portfolio trading. Excluding OPTION keeps
# covered-call premium flow out of the turnover figure (option premium
# is reported separately on the per-underlying option-income table).
# Excluding CASH/FX keeps routine currency sweeps from masquerading as
# portfolio activity.
_TRADING_KINDS: frozenset[AssetKind] = frozenset(
    {AssetKind.EQUITY, AssetKind.ETF, AssetKind.MUTUAL_FUND}
)


@dataclass(frozen=True)
class MonthlyTurnover:
    """One month's two-way turnover.

    ``ratio = (buys + sells) / 2 / avg_nav``. ``buys`` and ``sells``
    are the absolute notional of trading-kind ``Transaction`` rows
    whose ``trade_date`` lands in the month; ``avg_nav`` is the simple
    average of the pooled portfolio beginning and ending values for
    the month.
    """

    period_end: date
    buys_base: float
    sells_base: float
    average_nav: float
    ratio: float


@dataclass(frozen=True)
class TurnoverStats:
    """Per-period turnover output (legacy SEC formula, see
    :func:`portfolio_turnover`)."""

    label: str
    purchases_base: float
    sales_base: float
    average_nav: float
    ratio: float
    annualised: bool


def monthly_two_way_turnover_series(
    statements: list[Statement],
    pooled_periods: list[PooledPeriod],
) -> list[MonthlyTurnover]:
    """Per-month two-way turnover over the pooled audit window.

    Iterates the pooled monthly periods and, for each month, sums
    stock buy / sell notional from two sources:

      * **Per-trade transactions** -- ``Transaction(kind in
        TRADING_KINDS, action in {'buy', 'sell'})`` whose
        ``trade_date`` lands in the month. This catches IBKR Trades
        rows and any other broker that enumerates individual stock
        fills.

      * **Statement-level aggregates** -- ``stmt.stock_purchases_base``
        / ``stmt.stock_sales_base`` on statements whose period equals
        this month. This catches Schwab monthly statements, which
        publish ``Investments Purchased`` / ``Investments Sold``
        totals in the Cash Transactions Summary but do not enumerate
        per-trade stock rows (the Schwab parser intentionally skips
        those because realized PnL flows through
        ``per_symbol_pnl_base`` instead).

    The two sources don't overlap by construction: brokers that emit
    per-trade rows leave the statement aggregates at zero, and vice
    versa. The monthly ratio is ``(buys + sells) / 2 / avg_NAV_month``
    where ``avg_NAV_month`` is the simple average of the pooled
    portfolio beginning and ending values.
    """
    if not pooled_periods:
        return []
    out: list[MonthlyTurnover] = []
    for period in pooled_periods:
        month_start = period.period_end.replace(day=1)
        month_end = period.period_end
        buys = Decimal("0")
        sells = Decimal("0")
        for stmt in statements:
            # Skip statements whose period doesn't overlap this month
            # at all. A monthly Schwab statement overlaps exactly one
            # pooled period; a yearly IBKR statement overlaps twelve.
            if stmt.period_end < month_start or stmt.period_start > month_end:
                continue
            for txn in stmt.transactions:
                if txn.kind not in _TRADING_KINDS:
                    continue
                if not (month_start <= txn.trade_date <= month_end):
                    continue
                amount = abs(txn.amount_base)
                if txn.action == "buy":
                    buys += amount
                elif txn.action == "sell":
                    sells += amount
            # Statement-level aggregate contributes only when the
            # statement covers this exact month (Schwab monthly).
            # Multi-month statements that carry per-trade transactions
            # already had their trading counted above; multi-month
            # statements with an aggregate would over-attribute their
            # entire window to a single month, so we require
            # ``period_start == month_start`` AND
            # ``period_end == month_end``.
            if stmt.period_start == month_start and stmt.period_end == month_end:
                buys += abs(stmt.stock_purchases_base)
                sells += abs(stmt.stock_sales_base)
        avg_nav = (period.beginning_value + period.ending_value) / 2.0
        ratio = 0.0 if avg_nav <= 0 else (float(buys) + float(sells)) / 2.0 / avg_nav
        out.append(
            MonthlyTurnover(
                period_end=month_end,
                buys_base=float(buys),
                sells_base=float(sells),
                average_nav=avg_nav,
                ratio=ratio,
            )
        )
    return out


def yearly_turnover_from_monthly(
    monthly: list[MonthlyTurnover],
    *,
    start: date,
    end: date,
) -> float:
    """Sum monthly two-way turnover ratios across ``[start, end]``.

    The per-year row in the yearly summary reads this directly:
    summing twelve monthly rotation rates gives the year's total
    rotation rate. Partial-year rows report only the in-window months
    -- a 3-month or 4-month partial year sums 3 or 4 monthly ratios,
    so the headline number reflects *that period's* activity without
    being inflated to an annual rate.
    """
    return sum(m.ratio for m in monthly if start <= m.period_end <= end)


def portfolio_turnover(
    statements: list[Statement],
    *,
    start: date,
    end: date,
    average_nav: float,
    label: str = "",
    annualise: bool = False,
) -> TurnoverStats:
    """SEC-convention turnover -- ``min(buys, sells) / avg_NAV``.

    Kept for callers that explicitly want the Form N-1A number (e.g.
    direct comparisons against mutual-fund prospectus turnover). The
    report pipeline does *not* use this function -- it uses
    :func:`monthly_two_way_turnover_series` and
    :func:`yearly_turnover_from_monthly` instead, which capture
    one-way deployment activity that ``min(buys, sells)`` zeroes out.

    ``annualise=True`` scales the raw ratio by ``365 / window_days``;
    by default the function returns the within-period ratio.
    """
    if start > end:
        raise ValueError("turnover window start must be <= end")
    purchases = Decimal("0")
    sales = Decimal("0")
    for stmt in statements:
        for txn in stmt.transactions:
            if txn.kind not in _TRADING_KINDS:
                continue
            if not (start <= txn.trade_date <= end):
                continue
            amount = abs(txn.amount_base)
            if txn.action == "buy":
                purchases += amount
            elif txn.action == "sell":
                sales += amount

    lesser = min(float(purchases), float(sales))
    ratio = 0.0 if average_nav <= 0 else lesser / average_nav
    if annualise and ratio > 0:
        window_days = (end - start).days + 1
        if window_days > 0:
            ratio *= 365.0 / window_days
    return TurnoverStats(
        label=label,
        purchases_base=float(purchases),
        sales_base=float(sales),
        average_nav=float(average_nav),
        ratio=ratio,
        annualised=annualise,
    )
