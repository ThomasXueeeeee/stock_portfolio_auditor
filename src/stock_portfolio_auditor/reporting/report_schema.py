# SPDX-License-Identifier: MIT
"""Typed bridge between analytics outputs and report templates."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

FIELD_LABELS = {
    "twr": "TWR",
    "mwr_irr": "MWR-IRR",
    "mwr_dietz": "MWR-Dietz",
    "total_pnl": "Total $ PnL",
    "max_drawdown": "Max DD",
    "sharpe": "Sharpe",
    "sortino": "Sortino",
    "calmar": "Calmar",
    "tcost_bps": "Cost drag (bps)",
    "turnover": "Turnover",
}


class ReportModel(BaseModel):
    """Base report schema model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class KpiStrip(ReportModel):
    """Top-level KPI values.

    Beta / alpha / information-ratio are regressed against the primary
    benchmark over the **full audit window** here. They are
    deliberately not surfaced per-year in the yearly summary table:
    short partial-year rows (3 or 4 months for the audit's first and
    last calendar years) regress on too few observations to produce a
    stable point estimate, and a single full-window number is the
    correct unit for comparing to a fund-prospectus reference anyway.

    ``lending_bps`` is computed but not surfaced in the Key Metrics
    panel because securities-lending income is shown in the dollar-
    PnL decomposition chart. ``turnover`` is the average annual
    turnover over the audit window (sum of per-year ratios / years).
    """

    twr: float
    mwr_irr: float
    mwr_dietz: float
    total_pnl: float
    max_drawdown: float
    sharpe: float
    sortino: float
    calmar: float
    tcost_bps: float
    turnover: float = 0.0
    lending_bps: float = 0.0
    beta: float | None = None
    alpha: float | None = None
    info_ratio: float | None = None
    primary_benchmark_ticker: str = ""


class YearlySummaryRow(ReportModel):
    """One row in the yearly summary table.

    The ``*_bps`` and ``*_pnl`` fields are kept on the model so the contribution
    bar charts can read them; the HTML table itself only renders the high-level
    risk/return columns.

    ``turnover`` is the SEC-convention annualised turnover for the row's
    period; ``avg_effective_n`` and ``avg_top5_share`` are the year's
    average monthly portfolio-concentration metrics (HHI-derived
    effective number of equal-weighted positions, and cumulative weight
    of the top 5 names). Beta / alpha / information-ratio are
    deliberately *not* carried on this row -- those regression statistics
    are reported once on the :class:`KpiStrip` for the full audit window,
    because short partial-year rows (3-4 months) regress on too few
    observations to produce a stable point estimate.
    """

    label: str
    twr: float
    mwr_irr: float
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    best_month: float = 0.0
    worst_month: float = 0.0
    turnover: float = 0.0
    avg_effective_n: float = 0.0
    avg_top5_share: float = 0.0
    # Contribution decomposition (in bps and dollars)
    price_bps: float = 0.0
    dividend_bps: float = 0.0
    option_bps: float = 0.0
    fx_bps: float = 0.0
    lending_bps: float = 0.0
    tcost_bps: float = 0.0
    price_pnl: float = 0.0
    dividend_pnl: float = 0.0
    option_pnl: float = 0.0
    fx_pnl: float = 0.0
    lending_pnl: float = 0.0
    tcost_pnl: float = 0.0
    pnl: float = 0.0


class ChartBlock(ReportModel):
    """Pre-rendered Plotly chart HTML block."""

    key: str
    title: str
    html: str


class ContributionRow(ReportModel):
    """One row in the top-contributors / top-detractors holdings panel."""

    symbol: str
    description: str
    accounts: tuple[str, ...]
    dollar_pnl: float
    contribution_pct: float  # contribution as a fraction of starting portfolio value
    average_market_value: float
    ending_market_value: float
    first_seen: date
    last_seen: date


class OptionIncomeRow(ReportModel):
    """One row in the per-underlying option-income breakdown.

    Income is measured as **transaction cash flow** -- premium received on
    opens (and on long-position closes) minus premium paid on closes (and
    on long-position opens). This correctly attributes assignment events
    to the resulting stock-side position (the option leg has zero cash
    flow at assignment) rather than reading them as option-leg losses the
    way Schwab's 1099 does.
    """

    underlying: str
    premium_received: float
    premium_paid: float
    net: float


class ConcentrationPoint(ReportModel):
    """A single month-end concentration measurement.

    Stock-only (options / cash / FX are excluded). ``hhi`` is the
    Herfindahl-Hirschman Index over per-symbol weights (sum of squared
    weights, in [0, 1]); ``effective_n`` is its reciprocal, the number
    of equal-weighted positions that would produce the same HHI;
    ``top5_share`` / ``top10_share`` are the cumulative weight of the
    five / ten largest names by USD-translated market value.
    """

    as_of: date
    portfolio_value_base: float
    n_positions: int
    hhi: float
    effective_n: float
    top1_share: float
    top5_share: float
    top10_share: float


class ReportSchema(ReportModel):
    """Complete report payload consumed by Jinja templates.

    ``include_dollar_amounts`` controls whether the rendered HTML
    surfaces absolute portfolio scale. When ``True`` (the default) the
    report shows $ P&L on the KPI strip, dollar columns on the
    contribution tables, the dollar-PnL waterfall / decomposition
    charts, the value-vs-contribution chart, and the dollar option-
    income figures. When ``False`` those surfaces are hidden so the
    report can be shared without revealing portfolio size while
    keeping returns, ratios, concentration, turnover, and the bps
    decomposition visible. The KPI strip's ``total_pnl`` field stays
    on the model (chart code reads it) but the template skips its row.
    """

    title: str
    accounts: tuple[str, ...]
    period_start: date
    period_end: date
    base_currency: str
    generated_at: datetime
    kpis: KpiStrip
    yearly_summary: tuple[YearlySummaryRow, ...] = ()
    charts: tuple[ChartBlock, ...] = ()
    top_contributors: tuple[ContributionRow, ...] = ()
    top_detractors: tuple[ContributionRow, ...] = ()
    total_attributed_pnl: float = 0.0
    option_income: tuple[OptionIncomeRow, ...] = ()
    total_option_income: float = 0.0
    monthly_concentration: tuple[ConcentrationPoint, ...] = ()
    include_dollar_amounts: bool = True
    field_labels: dict[str, str] = FIELD_LABELS
    narrative: str = ""
