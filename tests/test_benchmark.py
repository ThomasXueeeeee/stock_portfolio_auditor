from __future__ import annotations

import pandas as pd

from stock_portfolio_auditor.analytics.benchmark import BENCHMARKS, compare_to_benchmark


def test_benchmark_catalog_includes_required_defaults() -> None:
    assert {"SPY", "QQQ", "IWM", "DIA", "MCHI", "EWH", "URTH"}.issubset(BENCHMARKS)


def test_compare_to_benchmark_returns_stats() -> None:
    index = pd.date_range("2024-01-31", periods=4, freq="ME")
    portfolio = pd.Series([0.02, 0.01, -0.01, 0.03], index=index)
    benchmark = pd.Series([0.01, 0.01, -0.02, 0.02], index=index)
    stats = compare_to_benchmark(portfolio, benchmark)
    assert stats.hit_rate > 0
