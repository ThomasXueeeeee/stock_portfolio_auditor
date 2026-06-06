# SPDX-License-Identifier: MIT
"""Build a report schema from parsed statements."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import cast

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import pyxirr

from stock_portfolio_auditor.analytics import (
    annualize_return,
    calmar_ratio,
    cost_ledger,
    cumulative_index,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    total_cost_drag_bps,
)
from stock_portfolio_auditor.analytics.concentration import (
    monthly_concentration_series,
    yearly_concentration_aggregates,
)
from stock_portfolio_auditor.analytics.contribution import (
    position_contributions,
    top_contributors,
    top_detractors,
    total_attributed_pnl,
)
from stock_portfolio_auditor.analytics.option_income import (
    option_income_by_underlying,
    total_option_income,
)
from stock_portfolio_auditor.analytics.pooled import (
    PooledPeriod,
    audit_end_date,
    cumulative_pooled_twr,
    pooled_monthly_periods,
    pooled_total_twr,
    pooled_value_series,
)
from stock_portfolio_auditor.analytics.returns import modified_dietz_return
from stock_portfolio_auditor.analytics.turnover import (
    monthly_two_way_turnover_series,
    yearly_turnover_from_monthly,
)
from stock_portfolio_auditor.domain.models import AssetKind, IncomeBucket, Statement
from stock_portfolio_auditor.ingestion.audit_window import filter_statements_to_window
from stock_portfolio_auditor.ingestion.fx_reconstruction import fx_convert_statements
from stock_portfolio_auditor.reporting.benchmark_data import (
    BenchmarkSeries,
    fetch_benchmark_series,
)
from stock_portfolio_auditor.reporting.narrative import build_narrative
from stock_portfolio_auditor.reporting.report_schema import (
    ChartBlock,
    ConcentrationPoint,
    ContributionRow,
    KpiStrip,
    OptionIncomeRow,
    ReportSchema,
    YearlySummaryRow,
)

_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
# Default primary benchmark for the regression statistics in the KPI
# strip. URTH (iShares MSCI World ETF) covers developed-market equities
# in the same global breadth this audit pipeline supports (US + HK +
# whatever other listings the user holds), so it is a more honest
# baseline than SPY for a portfolio with material non-US exposure. The
# user can override by passing ``primary_benchmark="SPY"`` (or any
# other ticker present in ``DEFAULT_BENCHMARK_TICKERS``) to
# :func:`build_report_schema`. The cumulative-return chart still
# displays the full ticker set regardless of which one is primary.
_DEFAULT_PRIMARY_BENCHMARK = "URTH"


def build_report_schema(
    statements: list[Statement],
    *,
    title: str = "Portfolio Performance Audit",
    audit_start: date | None = None,
    audit_end: date | None = None,
    primary_benchmark: str = _DEFAULT_PRIMARY_BENCHMARK,
    include_dollar_amounts: bool = True,
) -> ReportSchema:
    """Create a ReportSchema from parsed statements.

    ``primary_benchmark`` is the ticker used for the KPI strip's beta,
    alpha and information-ratio. Defaults to ``URTH`` (iShares MSCI
    World) so a global portfolio is regressed against a global
    benchmark; pass ``primary_benchmark="SPY"`` for a US-only audit.
    The cumulative-return chart still renders every benchmark in
    :data:`DEFAULT_BENCHMARK_TICKERS` regardless of which one is
    primary -- only the regression statistics on the KPI strip change.

    ``include_dollar_amounts=False`` instructs the renderer to hide
    the absolute-scale surfaces of the report (total $ P&L, $-column
    on the contribution tables, value-vs-contribution chart, dollar
    PnL waterfall / decomposition, dollar option-income figures).
    Returns, bps decomposition, concentration, turnover and the risk
    ratios stay visible so the report can be shared without revealing
    portfolio size.

    When ``audit_start`` and / or ``audit_end`` are supplied, statements
    are *clipped* to the requested window before any FX / dedup /
    attribution work runs:

    * statements fully inside the window pass through unchanged;
    * statements fully outside the window are dropped;
    * statements that partially overlap (e.g. a full-year 1099 Composite
      when the audit starts mid-year, or an IBKR YTD CSV that runs past
      the audit end) are *trimmed*: transactions are filtered to
      ``trade_date`` inside the window, holdings / cash snapshots are
      kept only when ``period_end`` itself is in window, and
      ``per_symbol_pnl_base`` is rebuilt from in-window dispositions
      whenever the source publishes per-trade realized PnL (Schwab
      1099-B does; IBKR's Mark-to-Market Performance Summary does not,
      so its full-period aggregate is kept with a logged note).

    This makes the audit window an *explicit contract* without throwing
    away useful data on broader-period documents.
    """
    if not statements:
        raise ValueError("Cannot build report without statements")

    ordered = sorted(statements, key=lambda stmt: (stmt.period_start, stmt.period_end))
    if audit_start is not None or audit_end is not None:
        effective_start = audit_start or min(s.period_start for s in ordered)
        effective_end = audit_end or max(s.period_end for s in ordered)
        ordered, audit_window_notes = filter_statements_to_window(
            ordered, start_date=effective_start, end_date=effective_end
        )
        for stmt, reason in audit_window_notes:
            print(
                f"[audit-window] {stmt.account_label} "
                f"{stmt.period_start.isoformat()}..{stmt.period_end.isoformat()}: {reason}"
            )
        if not ordered:
            raise ValueError(
                f"No statements remain inside audit window "
                f"{effective_start.isoformat()}..{effective_end.isoformat()}; "
                "supply broker statements that cover the requested period."
            )
    # Translate any non-base-currency holding values to the account's base
    # currency. The IBKR CSV parser leaves market_value_base equal to the
    # local-currency value, which makes HKD / JPY positions appear as huge
    # dollar amounts in the contribution panel without this step. The pass
    # is idempotent for already-translated inputs because
    # ``fx_convert_statements`` only mutates rows where the currency differs
    # from the statement's base_currency *and* the FX rate fetch succeeds.
    ordered = fx_convert_statements(ordered)
    # When a Schwab Form 1099 Composite is present for an account/year, it
    # supersedes whatever realized PnL the monthly statements managed to
    # extract -- the 1099-B is the authoritative IRS-reportable record and
    # covers periods (pre-2024 brokerage) where the monthly statements
    # publish only an aggregate "Investments Sold" total with no per-symbol
    # detail. Drop the monthly per_symbol_pnl_base in overlapping years to
    # avoid double-counting.
    ordered = _dedupe_schwab_1099_realized(ordered)
    # The 1099 Composite is zero-NAV / no-holdings / no-transactions and
    # contributes only per_symbol_pnl_base. It must therefore NOT shape the
    # audit window's start/end or pooled monthly NAV series -- if its
    # ``period_start = year start`` were treated like a regular statement it
    # would extend the audit back to Jan 1 of the earliest tax-year filed
    # for, well before any monthly statement actually covers, and the
    # pooled NAV series would invent zero-data months. Compute the audit
    # window from the NAV-bearing statements only.
    nav_statements = [stmt for stmt in ordered if _is_nav_bearing(stmt)]
    if not nav_statements:
        raise ValueError("Cannot build report without any NAV-bearing statements")
    # Operator-supplied dates are *authoritative* when present. The
    # default ``audit_end_date`` heuristic collapses the window to the
    # earliest "latest month-end" across accounts (so a stale IBKR annual
    # statement would force every other account's data to be truncated
    # to that month), which is the opposite of what an explicit end date
    # is asking for.
    start = audit_start if audit_start is not None else min(s.period_start for s in nav_statements)
    end = audit_end if audit_end is not None else audit_end_date(nav_statements)
    base_currency = nav_statements[0].base_currency
    accounts = tuple(sorted({stmt.account_label for stmt in nav_statements}))

    pooled_periods = pooled_monthly_periods(nav_statements, end=end)
    total_twr = pooled_total_twr(pooled_periods)
    mwr_irr = _money_weighted_return_irr_until(nav_statements, end)
    mwr_dietz = _money_weighted_return_dietz_until(nav_statements, end)

    returns_series = pd.Series(
        [period.period_return for period in pooled_periods],
        index=pd.to_datetime([period.period_end for period in pooled_periods]),
    )
    wealth = cumulative_index(returns_series)
    dd = max_drawdown(wealth)
    annual_return = annualize_return(total_twr, start, end, partial_year=False)
    avg_nav = _average_nav(nav_statements)
    costs = cost_ledger(nav_statements, average_nav=avg_nav)

    final_value = pooled_periods[-1].ending_value if pooled_periods else 0.0
    initial_value = pooled_periods[0].beginning_value if pooled_periods else 0.0
    net_cf = sum(period.external_cash_flow for period in pooled_periods)
    total_pnl = final_value - initial_value - net_cf

    benchmark_series = fetch_benchmark_series(start, end)
    primary_benchmark_series = _find_benchmark(benchmark_series, primary_benchmark)

    # Monthly stock-only concentration time series. Each month-end uses
    # each account's latest snapshot <= the month-end, so the series
    # smoothly spans the audit window even for accounts (annual IBKR
    # statements) that don't publish a snapshot every month.
    concentration_snapshots = monthly_concentration_series(
        ordered, start=start, end=end
    )
    concentration_by_year = {
        row.label: row
        for row in yearly_concentration_aggregates(
            concentration_snapshots,
            audit_start=start,
            audit_end=end,
            period_labeler=period_label,
        )
    }

    # Monthly two-way turnover ratios are the building block the
    # yearly summary and KPI strip read from. Each entry is
    # ``(buys_month + sells_month) / 2 / avg_NAV_month`` so a quarter
    # of one-way buying contributes real activity rather than zeroing
    # out the way the SEC's ``min(buys, sells)`` formula would. The
    # yearly summary's Turnover column sums these ratios for each
    # calendar year inside the audit window; the Total row /
    # KPI strip divides the sum of yearly ratios by years-in-window.
    monthly_turnover_series = monthly_two_way_turnover_series(
        ordered, pooled_periods
    )

    yearly_rows = _yearly_summary_rows(
        nav_statements,
        pooled_periods,
        start,
        end,
        concentration_by_label=concentration_by_year,
        monthly_turnover_series=monthly_turnover_series,
    )
    full_window_turnover = _average_annual_turnover(yearly_rows, start, end)
    yearly_rows = _set_total_turnover(yearly_rows, full_window_turnover)

    # Full-window beta / alpha / IR against the primary benchmark.
    portfolio_monthly = pd.Series(
        [p.period_return for p in pooled_periods],
        index=pd.to_datetime([p.period_end for p in pooled_periods]),
    )
    benchmark_monthly = (
        _benchmark_to_monthly_returns(primary_benchmark_series)
        if primary_benchmark_series is not None
        else pd.Series(dtype=float)
    )
    beta, alpha, info_ratio = _benchmark_regression(
        portfolio_monthly, benchmark_monthly
    )

    kpis = KpiStrip(
        twr=annual_return,
        mwr_irr=mwr_irr,
        mwr_dietz=mwr_dietz,
        total_pnl=total_pnl,
        max_drawdown=dd.max_drawdown,
        sharpe=sharpe_ratio(returns_series),
        sortino=sortino_ratio(returns_series),
        calmar=calmar_ratio(annual_return, dd.max_drawdown),
        tcost_bps=total_cost_drag_bps(costs),
        turnover=full_window_turnover,
        lending_bps=_lending_bps(nav_statements, avg_nav),
        beta=beta,
        alpha=alpha,
        info_ratio=info_ratio,
        primary_benchmark_ticker=(
            primary_benchmark_series.ticker if primary_benchmark_series else primary_benchmark
        ),
    )

    charts = _build_charts(
        nav_statements,
        pooled_periods,
        yearly_rows,
        benchmark_series,
        concentration_snapshots=concentration_snapshots,
    )
    narrative = build_narrative(
        kpis=kpis,
        yearly_rows=yearly_rows,
        period_start=start,
        period_end=end,
        benchmark_series=benchmark_series,
    )

    # Per-position attribution is **price-only** (dividends, lending, fees
    # are reported separately in the dollar-PnL decomposition chart). Default
    # the share denominator to the sum of per-position PnL so the percentages
    # sum to 100% of the attributed price total, matching fund-letter
    # convention; the report's reconciliation footnote compares this total
    # against the chart's Price bucket.
    contributions = position_contributions(ordered, audit_end=end)
    contributors = tuple(_to_contribution_row(row) for row in top_contributors(contributions, n=5))
    detractors = tuple(_to_contribution_row(row) for row in top_detractors(contributions, n=5))

    # Option income is reported as transaction cash flow per underlying --
    # premium received minus premium paid -- using only ``Transaction(kind=
    # OPTION)`` rows so assignments don't show as artificial losses on the
    # option leg (the cash flow at an assignment lands on the stock side,
    # not the option side). 1099 "realized" for options conflates the two
    # and is intentionally not used here.
    option_rows = option_income_by_underlying(nav_statements)
    option_income_schema = tuple(
        OptionIncomeRow(
            underlying=row.underlying,
            premium_received=row.premium_received,
            premium_paid=row.premium_paid,
            net=row.net,
        )
        for row in option_rows
    )

    concentration_points = tuple(
        ConcentrationPoint(
            as_of=snap.as_of,
            portfolio_value_base=snap.portfolio_value_base,
            n_positions=snap.n_positions,
            hhi=snap.hhi,
            effective_n=snap.effective_n,
            top1_share=snap.top1_share,
            top5_share=snap.top5_share,
            top10_share=snap.top10_share,
        )
        for snap in concentration_snapshots
    )

    return ReportSchema(
        title=title,
        accounts=accounts,
        period_start=start,
        period_end=end,
        base_currency=base_currency,
        generated_at=datetime.now(),
        kpis=kpis,
        yearly_summary=tuple(yearly_rows),
        charts=charts,
        narrative=narrative,
        top_contributors=contributors,
        top_detractors=detractors,
        total_attributed_pnl=total_attributed_pnl(contributions),
        option_income=option_income_schema,
        total_option_income=total_option_income(option_rows),
        monthly_concentration=concentration_points,
        include_dollar_amounts=include_dollar_amounts,
    )


def _is_nav_bearing(stmt: Statement) -> bool:
    """Return True if the statement contributes NAV / transactions / holdings.

    Tax-form-derived statements (Schwab 1099 Composite) carry only
    per-symbol realized PnL and a synthetic Jan 1 - Dec 31 window. They
    must be excluded from any audit-window, pooled NAV, or aggregate
    cost-bucket calculation -- those run off the broker-published monthly
    statements -- but their per_symbol_pnl_base still flows into
    position_contributions where it's the authoritative source for
    per-position realized PnL.
    """
    if stmt.broker == "schwab" and stmt.frequency == "A":
        return False
    return True


def _dedupe_schwab_1099_realized(statements: list[Statement]) -> list[Statement]:
    """Zero out monthly Schwab per_symbol_pnl_base when a 1099 covers the year.

    The 1099 Composite (parsed as a Schwab Statement with ``frequency='A'``)
    publishes per-lot realized PnL covering all dispositions for the tax
    year, including options. Monthly statements in the same account / year
    have their own (less reliable) realized rows -- summing both would
    double-count. The 1099 wins.
    """
    covered: set[tuple[str, int]] = {
        (stmt.account_label, stmt.period_end.year)
        for stmt in statements
        if stmt.broker == "schwab" and stmt.frequency == "A"
    }
    if not covered:
        return statements
    return [
        stmt.model_copy(update={"per_symbol_pnl_base": {}})
        if (
            stmt.broker == "schwab"
            and stmt.frequency != "A"
            and (stmt.account_label, stmt.period_end.year) in covered
        )
        else stmt
        for stmt in statements
    ]


def _to_contribution_row(row) -> ContributionRow:  # type: ignore[no-untyped-def]
    return ContributionRow(
        symbol=row.symbol,
        description=row.description,
        accounts=row.accounts,
        dollar_pnl=row.dollar_pnl,
        contribution_pct=row.contribution_pct,
        average_market_value=row.average_market_value,
        ending_market_value=row.ending_market_value,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
    )


# ---------------------------------------------------------------------------
# Period labelling
# ---------------------------------------------------------------------------


def period_label(year: int, audit_start: date, audit_end: date) -> str:
    """Return a smart yearly label like ``2026 (Jan-Apr)`` for partial years."""
    start_month = audit_start.month if year == audit_start.year else 1
    end_month = audit_end.month if year == audit_end.year else 12
    if start_month == 1 and end_month == 12:
        return str(year)
    if start_month == end_month:
        return f"{year} ({_MONTH_ABBR[start_month - 1]})"
    return f"{year} ({_MONTH_ABBR[start_month - 1]}-{_MONTH_ABBR[end_month - 1]})"


# ---------------------------------------------------------------------------
# Whole-window MWR helpers (kept for the Total row)
# ---------------------------------------------------------------------------


def _money_weighted_return_irr_until(statements: list[Statement], audit_end: date) -> float:
    """XIRR-based MWR over external flows + pooled terminal value at audit_end."""
    if not statements:
        return 0.0
    by_account: dict[str, list[Statement]] = {}
    for stmt in statements:
        by_account.setdefault(stmt.account_label, []).append(stmt)

    flows: dict[date, float] = {}
    for stmts in by_account.values():
        stmts_sorted = sorted(stmts, key=lambda s: s.period_start)
        first = stmts_sorted[0]
        flows[first.period_start] = flows.get(first.period_start, 0.0) - float(
            first.beginning_value_base
        )
        for stmt in stmts_sorted:
            for txn in stmt.transactions:
                if txn.is_external_cash_flow and txn.trade_date <= audit_end:
                    flows[txn.trade_date] = flows.get(txn.trade_date, 0.0) - float(txn.amount_base)

    terminal_series = pooled_value_series(statements, end=audit_end)
    terminal = float(terminal_series.iloc[-1]) if not terminal_series.empty else 0.0
    flows[audit_end] = flows.get(audit_end, 0.0) + terminal
    try:
        result = pyxirr.xirr(list(flows.items()))
        return 0.0 if result is None else float(result)
    except Exception:  # noqa: BLE001
        return 0.0


def _money_weighted_return_dietz_until(statements: list[Statement], audit_end: date) -> float:
    """Modified Dietz over the full audit window."""
    if not statements:
        return 0.0
    by_account: dict[str, list[Statement]] = {}
    for stmt in statements:
        by_account.setdefault(stmt.account_label, []).append(stmt)
    start = min(stmt.period_start for stmt in statements)
    beginning = sum(
        float(sorted(stmts, key=lambda s: s.period_start)[0].beginning_value_base)
        for stmts in by_account.values()
    )
    terminal_series = pooled_value_series(statements, end=audit_end)
    ending = float(terminal_series.iloc[-1]) if not terminal_series.empty else 0.0
    flows = [
        (txn.trade_date, float(txn.amount_base))
        for stmts in by_account.values()
        for stmt in stmts
        for txn in stmt.transactions
        if txn.is_external_cash_flow and txn.trade_date <= audit_end
    ]
    return modified_dietz_return(beginning, ending, flows, start, audit_end)


# ---------------------------------------------------------------------------
# Yearly summary
# ---------------------------------------------------------------------------


def _yearly_summary_rows(
    statements: list[Statement],
    pooled_periods: list[PooledPeriod],
    audit_start: date,
    audit_end: date,
    *,
    concentration_by_label: dict[str, object],
    monthly_turnover_series: list,  # type: ignore[type-arg]
) -> list[YearlySummaryRow]:
    """Build per-year YearlySummaryRow entries plus a Total row.

    Beta / alpha / IR are *not* surfaced per year -- those statistics
    live on the :class:`KpiStrip` for the full audit window only, because
    short partial-year rows (3-4 months) regress on too few observations
    to produce a stable point estimate.
    """
    breakdown = _yearly_breakdown(pooled_periods)
    by_year_periods = _periods_by_year(pooled_periods)
    by_year_statements: dict[int, list[Statement]] = defaultdict(list)
    for stmt in statements:
        by_year_statements[stmt.period_end.year].append(stmt)

    rows: list[YearlySummaryRow] = []
    for entry in breakdown:
        year = int(entry["year"])
        label = period_label(year, audit_start, audit_end)
        year_statements = by_year_statements.get(year, [])
        year_periods = by_year_periods.get(year, [])
        avg_nav = _avg_nav_for_periods(year_periods)
        bps, dollars = _contribution_decomposition(year_statements, avg_nav)
        risk = _year_risk_metrics(year_periods)
        year_start = max(audit_start, date(year, 1, 1))
        year_end = min(audit_end, date(year, 12, 31))
        # Per-year turnover sums the monthly two-way ratios for the
        # year's in-window months. Each monthly ratio already weights
        # that month's trading by its own NAV, so a partial-year row
        # (e.g. ``2026 (Jan-Apr)``, which sums 4 monthly ratios) reads
        # as the activity in those months -- not extrapolated.
        year_turnover = yearly_turnover_from_monthly(
            monthly_turnover_series, start=year_start, end=year_end
        )
        concentration_row = concentration_by_label.get(label)
        rows.append(
            YearlySummaryRow(
                label=label,
                twr=entry["twr"],
                mwr_irr=_year_mwr(statements, year, audit_start, audit_end),
                sharpe=risk["sharpe"],
                max_drawdown=risk["max_drawdown"],
                best_month=risk["best_month"],
                worst_month=risk["worst_month"],
                turnover=year_turnover,
                avg_effective_n=getattr(concentration_row, "avg_effective_n", 0.0),
                avg_top5_share=getattr(concentration_row, "avg_top5_share", 0.0),
                price_bps=bps["price"],
                dividend_bps=bps["dividends"],
                option_bps=bps["options"],
                fx_bps=bps["fx"],
                lending_bps=bps["lending"],
                tcost_bps=bps["tcost"],
                price_pnl=dollars["price"],
                dividend_pnl=dollars["dividends"],
                option_pnl=dollars["options"],
                fx_pnl=dollars["fx"],
                lending_pnl=dollars["lending"],
                tcost_pnl=dollars["tcost"],
                pnl=entry["pnl"],
            )
        )

    if rows:
        total_twr = 1.0
        for entry in breakdown:
            total_twr *= 1.0 + entry["twr"]
        total_twr -= 1.0
        total_risk = _full_period_risk_metrics(pooled_periods)
        total_concentration = concentration_by_label.get("Total")
        rows.append(
            YearlySummaryRow(
                label="Total",
                twr=total_twr,
                mwr_irr=_money_weighted_return_irr_until(statements, audit_end),
                sharpe=total_risk["sharpe"],
                max_drawdown=total_risk["max_drawdown"],
                best_month=total_risk["best_month"],
                worst_month=total_risk["worst_month"],
                # turnover is set later by _set_total_turnover so it
                # uses the days-weighted average of the per-year rows
                # rather than the cumulative SEC formula. Left as 0.0
                # here as a placeholder.
                turnover=0.0,
                avg_effective_n=getattr(total_concentration, "avg_effective_n", 0.0),
                avg_top5_share=getattr(total_concentration, "avg_top5_share", 0.0),
                price_bps=sum(row.price_bps for row in rows),
                dividend_bps=sum(row.dividend_bps for row in rows),
                option_bps=sum(row.option_bps for row in rows),
                fx_bps=sum(row.fx_bps for row in rows),
                lending_bps=sum(row.lending_bps for row in rows),
                tcost_bps=sum(row.tcost_bps for row in rows),
                price_pnl=sum(row.price_pnl for row in rows),
                dividend_pnl=sum(row.dividend_pnl for row in rows),
                option_pnl=sum(row.option_pnl for row in rows),
                fx_pnl=sum(row.fx_pnl for row in rows),
                lending_pnl=sum(row.lending_pnl for row in rows),
                tcost_pnl=sum(row.tcost_pnl for row in rows),
                pnl=sum(row.pnl for row in rows),
            )
        )
    return rows


def _average_annual_turnover(
    yearly_rows: list[YearlySummaryRow],
    audit_start: date,
    audit_end: date,
) -> float:
    """Multi-year "average annual turnover" derived from per-year rows.

    Each per-year row's ``turnover`` field is the within-period ratio
    ``min(buys_i, sells_i) / avg_NAV_i`` (un-annualised). Summing those
    ratios gives the cumulative "fraction of NAV traded" over the
    audit window (each year contributes its in-window trading volume
    relative to its own average NAV). Dividing by the number of years
    in the window converts that cumulative figure into a per-year rate
    that is directly comparable both to the per-year rows above it and
    to industry benchmarks like "actively managed equity funds average
    ~70%/year".

    This approach intentionally differs from the SEC formula applied
    over a multi-year window. ``min(cumulative_buys,
    cumulative_sells) / avg_nav`` smooths over cross-year buy/sell
    imbalances and reports a much higher number than the simple
    average of per-year ratios; for a reader trying to interpret the
    Total row alongside the rows above it, the average-of-per-year-
    ratios formulation is what they expect.
    """
    sum_period_ratios = sum(
        row.turnover for row in yearly_rows if row.label != "Total"
    )
    total_days = (audit_end - audit_start).days + 1
    if total_days <= 0:
        return 0.0
    years_in_window = total_days / 365.0
    if years_in_window <= 0:
        return 0.0
    return sum_period_ratios / years_in_window


def _set_total_turnover(
    yearly_rows: list[YearlySummaryRow], turnover: float
) -> list[YearlySummaryRow]:
    """Return a copy of ``yearly_rows`` with the Total row's ``turnover``
    field set to ``turnover``."""
    updated: list[YearlySummaryRow] = []
    for row in yearly_rows:
        if row.label == "Total":
            updated.append(row.model_copy(update={"turnover": turnover}))
        else:
            updated.append(row)
    return updated


def _yearly_breakdown(pooled_periods: list[PooledPeriod]) -> list[dict[str, float]]:
    if not pooled_periods:
        return []
    by_year: dict[int, list[PooledPeriod]] = defaultdict(list)
    for period in pooled_periods:
        by_year[period.period_end.year].append(period)
    out: list[dict[str, float]] = []
    for year, year_periods in sorted(by_year.items()):
        twr = 1.0
        for period in year_periods:
            twr *= 1.0 + period.period_return
        twr -= 1.0
        first = year_periods[0]
        last = year_periods[-1]
        net_cf = sum(period.external_cash_flow for period in year_periods)
        out.append(
            {
                "year": float(year),
                "twr": twr,
                "beginning_value": first.beginning_value,
                "ending_value": last.ending_value,
                "external_cash_flow": net_cf,
                "pnl": last.ending_value - first.beginning_value - net_cf,
            }
        )
    return out


def _periods_by_year(pooled_periods: list[PooledPeriod]) -> dict[int, list[PooledPeriod]]:
    out: dict[int, list[PooledPeriod]] = defaultdict(list)
    for period in pooled_periods:
        out[period.period_end.year].append(period)
    return out


def _avg_nav_for_periods(periods: list[PooledPeriod]) -> float:
    if not periods:
        return 0.0
    return sum((p.beginning_value + p.ending_value) / 2.0 for p in periods) / len(periods)


def _contribution_decomposition(
    year_statements: list[Statement], average_nav: float
) -> tuple[dict[str, float], dict[str, float]]:
    """Return ``(bps, dollars)`` per contribution bucket.

    The price bucket is computed as

        price = (nav_change - external_cash_flows) - dividends - options - lending - fx - tcost

    so the bucket totals add up to the year's actual P&L (which is
    nav_change minus deposits/withdrawals). Without the cash-flow subtraction
    a year with a $200K deposit would attribute the $200K to Price PnL,
    overstating the chart bars by the cumulative deposits across all
    accounts.

    FX is sourced from one of two mutually exclusive places per statement:

      * for IBKR-style statements (``per_symbol_pnl_includes_unrealized``)
        the MTM Performance Summary's Forex rows are the period-over-period
        FX P&L source — the parser keys them under ``_FX_<CCY>`` in
        ``per_symbol_pnl_base``;
      * for other brokers that expose only a cash-balance snapshot's
        unrealized translation P&L, ``fx_translation_pnl`` on each
        :class:`CashBalance` row is consumed instead.

    The two sources are NOT additive — IBKR's ``Forex Balances`` reports a
    cumulative snapshot, not a period delta, so summing both would
    double-count.
    """
    dollars: dict[str, float] = {
        "price": 0.0,
        "dividends": 0.0,
        "options": 0.0,
        "fx": 0.0,
        "lending": 0.0,
        "tcost": 0.0,
    }
    if not year_statements:
        return {key: 0.0 for key in dollars}, dollars

    nav_change = 0.0
    external_cf = 0.0
    for stmt in year_statements:
        nav_change += float(stmt.ending_value_base) - float(stmt.beginning_value_base)
        for txn in stmt.transactions:
            amount = float(txn.amount_base)
            if txn.is_external_cash_flow:
                external_cf += amount
                continue
            if txn.income_bucket is IncomeBucket.CASH_DIVIDEND:
                dollars["dividends"] += amount
            elif txn.income_bucket is IncomeBucket.LENDING_INCOME:
                dollars["lending"] += amount
            elif txn.kind is AssetKind.OPTION:
                dollars["options"] += amount
            if txn.cost_bucket is not None:
                # commission and fees are already populated as positive
                # absolutes by the broker parsers; the prior ``or abs(amount)``
                # fallback would have silently treated the entire transaction
                # principal as cost drag for any future cost_bucket row with
                # zero charges, so it's deleted here in favour of an explicit
                # sum of the two cost columns.
                dollars["tcost"] -= abs(float(txn.commission)) + abs(float(txn.fees))
        if stmt.per_symbol_pnl_includes_unrealized:
            # Pull IBKR's Forex MTM (period delta) out of the per-symbol map
            # so it shows in the FX bar rather than disappearing into the
            # Price residual. CashBalance.fx_translation_pnl is a cumulative
            # snapshot for IBKR statements and is intentionally NOT added on
            # top to avoid double-counting.
            for symbol, pnl in stmt.per_symbol_pnl_base.items():
                if symbol.startswith("_FX_"):
                    dollars["fx"] += float(pnl)
        else:
            for cash in stmt.cash_balances:
                if cash.fx_translation_pnl is not None:
                    dollars["fx"] += float(cash.fx_translation_pnl)
    nav_change -= external_cf
    dollars["price"] = (
        nav_change
        - dollars["dividends"]
        - dollars["options"]
        - dollars["lending"]
        - dollars["fx"]
        - dollars["tcost"]
    )
    if average_nav == 0:
        bps = {key: 0.0 for key in dollars}
    else:
        bps = {key: value / average_nav * 10_000 for key, value in dollars.items()}
    return bps, dollars


def _year_risk_metrics(year_periods: list[PooledPeriod]) -> dict[str, float]:
    """Sharpe / drawdown / best-worst-month for one calendar-year window.

    Beta / alpha / IR are intentionally not computed here -- those are
    full-window-only and live on the KPI strip.
    """
    if not year_periods:
        return {
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "best_month": 0.0,
            "worst_month": 0.0,
        }
    returns = pd.Series(
        [p.period_return for p in year_periods],
        index=pd.to_datetime([p.period_end for p in year_periods]),
    )
    wealth = (1.0 + returns).cumprod()
    drawdowns = wealth / wealth.cummax() - 1.0
    return {
        "sharpe": _sharpe(returns),
        "max_drawdown": float(drawdowns.min()) if not drawdowns.empty else 0.0,
        "best_month": float(returns.max()),
        "worst_month": float(returns.min()),
    }


def _full_period_risk_metrics(
    pooled_periods: list[PooledPeriod],
) -> dict[str, float]:
    if not pooled_periods:
        return {
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "best_month": 0.0,
            "worst_month": 0.0,
        }
    portfolio = pd.Series(
        [p.period_return for p in pooled_periods],
        index=pd.to_datetime([p.period_end for p in pooled_periods]),
    )
    wealth = (1.0 + portfolio).cumprod()
    drawdowns = wealth / wealth.cummax() - 1.0
    return {
        "sharpe": _sharpe(portfolio),
        "max_drawdown": float(drawdowns.min()),
        "best_month": float(portfolio.max()),
        "worst_month": float(portfolio.min()),
    }


def _sharpe(monthly_returns: pd.Series) -> float:
    if monthly_returns.empty or monthly_returns.std(ddof=0) == 0:
        return 0.0
    return float(monthly_returns.mean() / monthly_returns.std(ddof=0) * np.sqrt(12.0))


def _benchmark_regression(
    portfolio_monthly: pd.Series, benchmark_monthly: pd.Series
) -> tuple[float | None, float | None, float | None]:
    if portfolio_monthly.empty or benchmark_monthly.empty:
        return None, None, None
    paired = pd.concat(
        [portfolio_monthly.rename("p"), benchmark_monthly.rename("b")], axis=1, join="inner"
    ).dropna()
    if len(paired) < 6:
        return None, None, None
    cov = float(paired["p"].cov(paired["b"]))
    var = float(paired["b"].var(ddof=0))
    if var == 0:
        return None, None, None
    beta = cov / var
    alpha_monthly = float(paired["p"].mean() - beta * paired["b"].mean())
    alpha_annual = (1.0 + alpha_monthly) ** 12 - 1.0
    active = paired["p"] - paired["b"]
    tracking_error = float(active.std(ddof=0))
    info_ratio = float(active.mean() / tracking_error * np.sqrt(12.0)) if tracking_error else 0.0
    return float(beta), alpha_annual, info_ratio


def _year_mwr(
    all_statements: list[Statement],
    year: int,
    audit_start: date,
    audit_end: date,
) -> float:
    """Period-return MWR for a single calendar year (Modified Dietz, not annualized)."""
    if not all_statements:
        return 0.0
    period_start = max(audit_start, date(year, 1, 1))
    period_end = min(audit_end, date(year, 12, 31))
    if period_start > period_end:
        return 0.0
    nav_series = pooled_value_series(all_statements, end=period_end)
    if nav_series.empty:
        return 0.0
    if period_start == audit_start:
        by_account: dict[str, list[Statement]] = {}
        for stmt in all_statements:
            by_account.setdefault(stmt.account_label, []).append(stmt)
        beginning = sum(
            float(sorted(stmts, key=lambda s: s.period_start)[0].beginning_value_base)
            for stmts in by_account.values()
            if sorted(stmts, key=lambda s: s.period_start)[0].period_start == audit_start
        )
    else:
        prior_month_end_ts = pd.Timestamp(period_start) - pd.Timedelta(days=1)
        prior = nav_series[nav_series.index <= prior_month_end_ts]
        beginning = float(prior.iloc[-1]) if not prior.empty else 0.0
    ending = float(nav_series.iloc[-1])
    flows = [
        (txn.trade_date, float(txn.amount_base))
        for stmt in all_statements
        for txn in stmt.transactions
        if txn.is_external_cash_flow and period_start <= txn.trade_date <= period_end
    ]
    return modified_dietz_return(beginning, ending, flows, period_start, period_end)


def _average_nav(statements: list[Statement]) -> float:
    values = [
        (float(statement.beginning_value_base) + float(statement.ending_value_base)) / 2
        for statement in statements
    ]
    return sum(values) / len(values) if values else 0.0


def _lending_bps(statements: list[Statement], average_nav: float) -> float:
    lending = sum(
        float(txn.amount_base)
        for stmt in statements
        for txn in stmt.transactions
        if txn.income_bucket is IncomeBucket.LENDING_INCOME
    )
    if average_nav == 0:
        return 0.0
    return lending / average_nav * 10_000


def _find_benchmark(benchmarks: list[BenchmarkSeries], ticker: str) -> BenchmarkSeries | None:
    for bench in benchmarks:
        if bench.ticker == ticker:
            return bench
    return benchmarks[0] if benchmarks else None


def _benchmark_to_monthly_returns(bench: BenchmarkSeries) -> pd.Series:
    cumulative = pd.Series(bench.cumulative_returns, index=pd.to_datetime(bench.dates))
    wealth = 1.0 + cumulative
    return wealth.pct_change().dropna()


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _build_charts(
    statements: list[Statement],
    pooled_periods: list[PooledPeriod],
    yearly_rows: list[YearlySummaryRow],
    benchmarks: list[BenchmarkSeries],
    *,
    concentration_snapshots: list,  # type: ignore[type-arg]
) -> tuple[ChartBlock, ...]:
    include_js = True
    charts: list[ChartBlock] = []

    charts.append(
        ChartBlock(
            key="value_vs_contributions",
            title="Value vs. Net Contributions",
            html=_to_html(
                _value_vs_contribution_chart(statements, pooled_periods),
                include_plotlyjs=include_js,
            ),
        )
    )
    include_js = False

    charts.append(
        ChartBlock(
            key="cumulative_return",
            title="Cumulative Rate of Return",
            html=_to_html(
                _cumulative_return_chart(pooled_periods, benchmarks),
                include_plotlyjs=include_js,
            ),
        )
    )

    charts.append(
        ChartBlock(
            key="decomposition_bps",
            title="Return Contribution by Year (bps)",
            html=_to_html(
                _decomposition_chart(yearly_rows, mode="bps"), include_plotlyjs=include_js
            ),
        )
    )
    charts.append(
        ChartBlock(
            key="decomposition_dollars",
            title="Dollar P&L Contribution by Year",
            html=_to_html(
                _decomposition_chart(yearly_rows, mode="dollars"), include_plotlyjs=include_js
            ),
        )
    )
    option_rows = option_income_by_underlying(statements)
    if option_rows:
        charts.append(
            ChartBlock(
                key="option_income_by_underlying",
                title="Option Premium Income by Underlying",
                html=_to_html(
                    _option_income_chart(option_rows), include_plotlyjs=include_js
                ),
            )
        )
    if concentration_snapshots:
        charts.append(
            ChartBlock(
                key="concentration_over_time",
                title="Portfolio Concentration Over Time",
                html=_to_html(
                    _concentration_chart(concentration_snapshots),
                    include_plotlyjs=include_js,
                ),
            )
        )
    return tuple(charts)


def _concentration_chart(snapshots: list) -> go.Figure:  # type: ignore[type-arg]
    """Monthly time series of stock-only concentration.

    The left axis carries the *effective number of equal-weighted
    positions* (``1 / HHI``) -- a portfolio that is becoming more
    diversified plots upward, more concentrated plots downward. The
    right axis carries the cumulative weight of the top-5 and top-10
    names so the absolute share of the largest positions is visible
    alongside the more abstract effective-N metric.
    """
    dates = [snap.as_of for snap in snapshots]
    eff_n = [snap.effective_n for snap in snapshots]
    top5 = [snap.top5_share * 100.0 for snap in snapshots]
    top10 = [snap.top10_share * 100.0 for snap in snapshots]
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=eff_n,
            name="Effective N (1 / HHI)",
            mode="lines+markers",
            line={"color": "#22d3ee", "width": 3},
            yaxis="y1",
            hovertemplate="<b>%{x|%b %Y}</b><br>Effective N: %{y:.1f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=top5,
            name="Top-5 share",
            mode="lines",
            line={"color": "#f97316", "width": 2, "dash": "dot"},
            yaxis="y2",
            hovertemplate="<b>%{x|%b %Y}</b><br>Top-5: %{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=top10,
            name="Top-10 share",
            mode="lines",
            line={"color": "#a855f7", "width": 2, "dash": "dash"},
            yaxis="y2",
            hovertemplate="<b>%{x|%b %Y}</b><br>Top-10: %{y:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        template="plotly_dark",
        height=420,
        hovermode="x unified",
        margin={"l": 60, "r": 60, "t": 30, "b": 60},
        yaxis={"title": "Effective N (positions)", "side": "left"},
        yaxis2={
            "title": "Top-N share (%)",
            "overlaying": "y",
            "side": "right",
            "ticksuffix": "%",
            "range": [0, 100],
        },
        legend={"orientation": "h", "y": -0.18},
    )
    return fig


def _option_income_chart(rows) -> go.Figure:  # type: ignore[no-untyped-def]
    """Sorted-by-net per-underlying option premium income chart.

    Each underlying ticker gets a single bar coloured by sign of ``net``.
    Hover text shows the gross premium received and premium paid so the
    user can see at a glance how much of a contribution was gross writing
    income vs how much was eaten by buy-to-close costs.
    """
    labels = [row.underlying for row in rows]
    nets = [row.net for row in rows]
    received = [row.premium_received for row in rows]
    paid = [row.premium_paid for row in rows]
    colors = ["#22c55e" if n >= 0 else "#ef4444" for n in nets]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels,
                y=nets,
                marker_color=colors,
                customdata=list(zip(received, paid, strict=True)),
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    "Net premium: $%{y:,.0f}<br>"
                    "Premium received: $%{customdata[0]:,.0f}<br>"
                    "Premium paid: $%{customdata[1]:,.0f}<extra></extra>"
                ),
            )
        ]
    )
    fig.update_layout(
        template="plotly_dark",
        height=380,
        margin={"l": 60, "r": 30, "t": 30, "b": 80},
        yaxis_title="Net premium ($)",
        xaxis_title="Underlying",
    )
    fig.update_yaxes(tickformat="$,.0f")
    return fig


def _value_vs_contribution_chart(
    statements: list[Statement], pooled_periods: list[PooledPeriod]
) -> go.Figure:
    audit_end = pooled_periods[-1].period_end if pooled_periods else None
    nav_series = pooled_value_series(statements, end=audit_end)
    dates = list(nav_series.index)
    values = [float(value) for value in nav_series.values]

    sorted_flow_dates: list[tuple[pd.Timestamp, float]] = sorted(
        ((pd.Timestamp(period.period_end), period.external_cash_flow) for period in pooled_periods),
        key=lambda item: item[0],
    )
    cf_running = pooled_periods[0].beginning_value if pooled_periods else 0.0
    cum_flows: list[float] = []
    flow_index = 0
    for snapshot in dates:
        while flow_index < len(sorted_flow_dates) and sorted_flow_dates[flow_index][0] <= snapshot:
            cf_running += sorted_flow_dates[flow_index][1]
            flow_index += 1
        cum_flows.append(cf_running)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=values,
            name="Portfolio value",
            mode="lines",
            line={"color": "#22d3ee", "width": 3},
            fill="tozeroy",
            fillcolor="rgba(34, 197, 94, 0.18)",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=cum_flows,
            name="Net contributions",
            mode="lines",
            line={"color": "#a3a3a3", "dash": "dot", "width": 2},
        )
    )
    fig.update_layout(
        template="plotly_dark",
        height=420,
        hovermode="x unified",
        margin={"l": 40, "r": 30, "t": 30, "b": 60},
        yaxis_tickprefix="$",
        legend={"orientation": "h", "y": -0.18},
    )
    return fig


def _cumulative_return_chart(
    pooled_periods: list[PooledPeriod], benchmarks: list[BenchmarkSeries]
) -> go.Figure:
    cumulative = cumulative_pooled_twr(pooled_periods)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=list(cumulative.index),
            y=[float(value) for value in cumulative.values],
            name="Portfolio TWR",
            mode="lines",
            line={"color": "#38bdf8", "width": 3},
        )
    )
    palette = ["#e5e7eb", "#f472b6", "#f59e0b", "#a3e635", "#34d399", "#c084fc", "#fb7185"]
    audit_start = cumulative.index.min() if not cumulative.empty else None
    for index, bench in enumerate(benchmarks):
        bench_series = pd.Series(bench.cumulative_returns, index=pd.to_datetime(bench.dates))
        if audit_start is not None:
            bench_series = bench_series[bench_series.index >= audit_start]
            if not bench_series.empty:
                base = bench_series.iloc[0]
                bench_series = (1.0 + bench_series) / (1.0 + base) - 1.0
        fig.add_trace(
            go.Scatter(
                x=list(bench_series.index),
                y=[float(value) for value in bench_series.values],
                name=f"{bench.name} ({bench.ticker})",
                mode="lines",
                line={"color": palette[index % len(palette)], "width": 1.5},
                visible=True if index < 3 else "legendonly",
            )
        )
    fig.update_layout(
        template="plotly_dark",
        height=440,
        hovermode="x unified",
        margin={"l": 40, "r": 30, "t": 30, "b": 60},
        yaxis_tickformat=".0%",
        legend={"orientation": "h", "y": -0.18},
    )
    return fig


def _decomposition_chart(yearly_rows: list[YearlySummaryRow], *, mode: str) -> go.Figure:
    rows = [row for row in yearly_rows if row.label != "Total"]
    labels = [row.label for row in rows]
    if mode == "bps":
        components = {
            "Price": [row.price_bps for row in rows],
            "Dividends": [row.dividend_bps for row in rows],
            "Options": [row.option_bps for row in rows],
            "FX": [row.fx_bps for row in rows],
            "Lending": [row.lending_bps for row in rows],
            "Cost drag": [row.tcost_bps for row in rows],
        }
        yaxis_title = "Contribution (bps)"
        tickformat = None
    else:
        components = {
            "Price": [row.price_pnl for row in rows],
            "Dividends": [row.dividend_pnl for row in rows],
            "Options": [row.option_pnl for row in rows],
            "FX": [row.fx_pnl for row in rows],
            "Lending": [row.lending_pnl for row in rows],
            "Cost drag": [row.tcost_pnl for row in rows],
        }
        yaxis_title = "Dollar P&L"
        tickformat = "$,.0f"
    colors = {
        "Price": "#38bdf8",
        "Dividends": "#22c55e",
        "Options": "#f97316",
        "FX": "#a78bfa",
        "Lending": "#facc15",
        "Cost drag": "#ef4444",
    }
    fig = go.Figure()
    for name, values in components.items():
        fig.add_trace(go.Bar(x=labels, y=values, name=name, marker_color=colors[name]))
    fig.update_layout(
        template="plotly_dark",
        barmode="relative",
        height=360,
        margin={"l": 60, "r": 30, "t": 30, "b": 60},
        yaxis_title=yaxis_title,
        legend={"orientation": "h", "y": -0.22},
    )
    if tickformat:
        fig.update_yaxes(tickformat=tickformat)
    return fig


def _to_html(fig: go.Figure, *, include_plotlyjs: bool) -> str:
    return cast(
        str,
        pio.to_html(
            fig,
            include_plotlyjs=bool(include_plotlyjs),
            full_html=False,
            config={"displaylogo": False, "responsive": True},
        ),
    )
