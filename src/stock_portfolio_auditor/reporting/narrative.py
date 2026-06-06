# SPDX-License-Identifier: MIT
"""Report narrative generation.

The narrative is deliberately deterministic and reads like a quarterly
factsheet rather than a marketing blurb. Each block is a short, factual
paragraph that interprets the numbers already presented elsewhere in the
report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from stock_portfolio_auditor.reporting.benchmark_data import BenchmarkSeries
from stock_portfolio_auditor.reporting.report_schema import (
    KpiStrip,
    ReportSchema,
    YearlySummaryRow,
)


@dataclass(frozen=True, slots=True)
class NarrativeBlock:
    """A heading + body paragraph in the executive narrative."""

    heading: str
    body: str


def build_narrative(
    *,
    kpis: KpiStrip,
    yearly_rows: list[YearlySummaryRow],
    period_start: date,
    period_end: date,
    benchmark_series: list[BenchmarkSeries],
) -> str:
    """Return an HTML narrative block for the executive summary section."""
    blocks = [
        _performance_block(kpis, period_start, period_end, benchmark_series),
        _capital_timing_block(yearly_rows),
        _source_of_return_block(yearly_rows),
        _risk_block(kpis, yearly_rows),
        _year_by_year_block(yearly_rows),
    ]
    rendered = "\n".join(
        f'<div class="narrative-block"><h4>{block.heading}</h4><p>{block.body}</p></div>'
        for block in blocks
        if block.body
    )
    return rendered


def executive_summary(schema: ReportSchema) -> str:
    """One-sentence fallback summary used when richer narrative is unavailable."""
    kpis = schema.kpis
    return (
        f"From {schema.period_start:%b %Y} through {schema.period_end:%b %Y}, the portfolio "
        f"returned {kpis.twr:.2%} annualized (TWR) and {kpis.mwr_irr:.2%} dollar-weighted (MWR), "
        f"with max drawdown of {kpis.max_drawdown:.2%}."
    )


def _performance_block(
    kpis: KpiStrip,
    start: date,
    end: date,
    benchmarks: list[BenchmarkSeries],
) -> NarrativeBlock:
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    years = months / 12.0
    benchmark_note = _benchmark_comparison(kpis.twr, years, benchmarks)
    body = (
        f"Over {start:%b %Y}\u2013{end:%b %Y} (~{years:.1f} years of data), the portfolio compounded at "
        f"<strong>{kpis.twr:.1%} annualized</strong> on a time-weighted basis "
        f"and <strong>{kpis.mwr_irr:.1%} annualized</strong> on a dollar-weighted basis. "
        f"{benchmark_note}"
    )
    return NarrativeBlock("Performance", body.strip())


def _benchmark_comparison(twr: float, years: float, benchmarks: list[BenchmarkSeries]) -> str:
    if not benchmarks or years <= 0:
        return ""
    comparisons: list[str] = []
    for bench in benchmarks[:3]:
        if not bench.cumulative_returns:
            continue
        bench_cum = float(bench.cumulative_returns[-1])
        bench_ann = (1.0 + bench_cum) ** (1.0 / years) - 1.0
        diff = twr - bench_ann
        marker = "ahead of" if diff >= 0 else "behind"
        comparisons.append(
            f"{marker} <strong>{bench.ticker}</strong> ({bench_ann:.1%} ann., {diff:+.1%} active)"
        )
    if not comparisons:
        return ""
    return "That is " + "; ".join(comparisons) + "."


def _capital_timing_block(yearly_rows: list[YearlySummaryRow]) -> NarrativeBlock:
    yearly = [row for row in yearly_rows if row.label != "Total"]
    if not yearly:
        return NarrativeBlock("Capital Timing", "")
    timing_gaps = [(row.label, row.mwr_irr - row.twr) for row in yearly]
    best_year, best_gap = max(timing_gaps, key=lambda item: item[1])
    worst_year, worst_gap = min(timing_gaps, key=lambda item: item[1])
    avg_gap = sum(gap for _, gap in timing_gaps) / len(timing_gaps)
    if abs(avg_gap) < 0.005:
        verdict = "near-zero capital timing impact on average"
    elif avg_gap > 0:
        verdict = f"positive capital timing of <strong>{avg_gap:+.1%}</strong> per year on average"
    else:
        verdict = f"slightly negative capital timing of <strong>{avg_gap:+.1%}</strong> per year on average"
    body = (
        f"Comparing MWR vs TWR across years shows {verdict}. "
        f"Best timing year was <strong>{best_year}</strong> ({best_gap:+.1%}), "
        f"worst was <strong>{worst_year}</strong> ({worst_gap:+.1%}). "
        f"A positive gap means deposits were added before strong months; "
        f"a negative gap implies the opposite."
    )
    return NarrativeBlock("Capital Timing Skill", body)


def _source_of_return_block(yearly_rows: list[YearlySummaryRow]) -> NarrativeBlock:
    yearly = [row for row in yearly_rows if row.label != "Total"]
    if not yearly:
        return NarrativeBlock("Source of Return", "")
    totals = {
        "Price": sum(row.price_pnl for row in yearly),
        "Dividends": sum(row.dividend_pnl for row in yearly),
        "Options": sum(row.option_pnl for row in yearly),
        "Lending": sum(row.lending_pnl for row in yearly),
        "Cost drag": sum(row.tcost_pnl for row in yearly),
    }
    total_pnl = sum(abs(v) for v in totals.values()) or 1.0
    parts = []
    for name, value in totals.items():
        share = value / total_pnl
        if abs(share) < 0.01 and abs(value) < 1000:
            continue
        parts.append(f"{name} {value:+,.0f} ({share:+.1%})")
    body = (
        "Dollar P&L was driven primarily by price appreciation, with the breakdown shown below. "
        + ", ".join(parts)
        + "."
    )
    return NarrativeBlock("Source of Return", body)


def _risk_block(kpis: KpiStrip, yearly_rows: list[YearlySummaryRow]) -> NarrativeBlock:
    beta_text = ""
    if kpis.beta is not None:
        bench = kpis.primary_benchmark_ticker or "benchmark"
        beta_text = (
            f" Versus {bench}, portfolio beta is <strong>{kpis.beta:.2f}</strong>, "
            f"alpha is <strong>{(kpis.alpha or 0.0):.2%}</strong> annualized "
            f"(information ratio {kpis.info_ratio or 0.0:.2f})."
        )
    body = (
        f"Maximum drawdown was <strong>{kpis.max_drawdown:.1%}</strong>, "
        f"with a Sharpe of <strong>{kpis.sharpe:.2f}</strong> "
        f"and Sortino of <strong>{kpis.sortino:.2f}</strong>. "
        f"Cost drag totaled <strong>{kpis.tcost_bps:.0f} bps</strong> of average NAV." + beta_text
    )
    return NarrativeBlock("Risk &amp; Cost", body)


def _year_by_year_block(yearly_rows: list[YearlySummaryRow]) -> NarrativeBlock:
    yearly = [row for row in yearly_rows if row.label != "Total"]
    if not yearly:
        return NarrativeBlock("By Year", "")
    pieces = [
        f"<strong>{row.label}</strong> {row.twr:+.1%} TWR "
        f"(max DD {row.max_drawdown:.1%}, turnover {row.turnover:.0%})"
        for row in yearly
    ]
    body = "; ".join(pieces) + "."
    return NarrativeBlock("By Year", body)
