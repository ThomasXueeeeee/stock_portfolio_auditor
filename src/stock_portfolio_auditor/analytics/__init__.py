"""Analytics primitives."""

from stock_portfolio_auditor.analytics.attribution import (
    PnlBreakdown,
    contribution_bps,
    pnl_breakdown,
)
from stock_portfolio_auditor.analytics.benchmark import (
    BENCHMARKS,
    Benchmark,
    BenchmarkStats,
    compare_to_benchmark,
)
from stock_portfolio_auditor.analytics.composite import beginning_nav_weights, pooled_nav_statements
from stock_portfolio_auditor.analytics.concentration import (
    ConcentrationSnapshot,
    YearlyConcentration,
    concentration_metrics,
    monthly_concentration_series,
    stock_holdings_at,
    yearly_concentration_aggregates,
)
from stock_portfolio_auditor.analytics.fx_attribution import (
    CurrencyExposure,
    allocate_cross_term_to_fx,
    cash_fx_attribution,
)
from stock_portfolio_auditor.analytics.nav import NavPoint, beginning_nav, monthly_nav
from stock_portfolio_auditor.analytics.option_income import (
    OptionIncomeRow,
    option_income_by_underlying,
    total_option_income,
)
from stock_portfolio_auditor.analytics.pnl_dollar import DollarPnlRow, waterfall_rows
from stock_portfolio_auditor.analytics.returns import (
    PeriodReturn,
    annualize_return,
    modified_dietz_return,
    money_weighted_return_dietz,
    money_weighted_return_irr,
    monthly_twr,
    time_weighted_return,
)
from stock_portfolio_auditor.analytics.risk import (
    Drawdown,
    calmar_ratio,
    cumulative_index,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
)
from stock_portfolio_auditor.analytics.tcost import CostLedgerRow, cost_ledger, total_cost_drag_bps
from stock_portfolio_auditor.analytics.timing import (
    TimingSkill,
    dca_counterfactual_mwr,
    timing_skill,
)
from stock_portfolio_auditor.analytics.turnover import (
    MonthlyTurnover,
    TurnoverStats,
    monthly_two_way_turnover_series,
    portfolio_turnover,
    yearly_turnover_from_monthly,
)

__all__ = [
    "Drawdown",
    "BENCHMARKS",
    "Benchmark",
    "BenchmarkStats",
    "ConcentrationSnapshot",
    "CurrencyExposure",
    "DollarPnlRow",
    "NavPoint",
    "OptionIncomeRow",
    "PeriodReturn",
    "PnlBreakdown",
    "MonthlyTurnover",
    "TimingSkill",
    "TurnoverStats",
    "YearlyConcentration",
    "annualize_return",
    "allocate_cross_term_to_fx",
    "beginning_nav",
    "beginning_nav_weights",
    "calmar_ratio",
    "concentration_metrics",
    "contribution_bps",
    "cost_ledger",
    "cash_fx_attribution",
    "compare_to_benchmark",
    "cumulative_index",
    "dca_counterfactual_mwr",
    "max_drawdown",
    "modified_dietz_return",
    "money_weighted_return_dietz",
    "money_weighted_return_irr",
    "monthly_concentration_series",
    "monthly_nav",
    "monthly_twr",
    "monthly_two_way_turnover_series",
    "option_income_by_underlying",
    "pnl_breakdown",
    "pooled_nav_statements",
    "portfolio_turnover",
    "sharpe_ratio",
    "sortino_ratio",
    "stock_holdings_at",
    "timing_skill",
    "time_weighted_return",
    "total_cost_drag_bps",
    "total_option_income",
    "yearly_concentration_aggregates",
    "yearly_turnover_from_monthly",
    "CostLedgerRow",
    "waterfall_rows",
]
