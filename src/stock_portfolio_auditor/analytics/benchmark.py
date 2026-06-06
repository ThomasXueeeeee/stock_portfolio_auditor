# SPDX-License-Identifier: MIT
"""Benchmark catalog and comparison helpers."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class Benchmark:
    """Benchmark catalog entry."""

    name: str
    ticker: str
    currency: str = "USD"


BENCHMARKS: dict[str, Benchmark] = {
    "SPY": Benchmark("S&P 500", "SPY", "USD"),
    "QQQ": Benchmark("Nasdaq 100", "QQQ", "USD"),
    "IWM": Benchmark("Russell 2000", "IWM", "USD"),
    "DIA": Benchmark("Dow Jones Industrial Average", "DIA", "USD"),
    "MCHI": Benchmark("MSCI China", "MCHI", "USD"),
    "EWH": Benchmark("Hong Kong", "EWH", "USD"),
    "2800.HK": Benchmark("Tracker Fund of Hong Kong", "2800.HK", "HKD"),
    "URTH": Benchmark("MSCI World", "URTH", "USD"),
    "ACWI": Benchmark("MSCI ACWI", "ACWI", "USD"),
}


@dataclass(frozen=True, slots=True)
class BenchmarkStats:
    """Benchmark comparison statistics."""

    beta: float
    alpha: float
    tracking_error: float
    information_ratio: float
    hit_rate: float


def compare_to_benchmark(
    portfolio_returns: pd.Series, benchmark_returns: pd.Series
) -> BenchmarkStats:
    """Compute simple monthly benchmark comparison stats."""
    aligned = pd.concat([portfolio_returns, benchmark_returns], axis=1, join="inner").dropna()
    if aligned.empty:
        return BenchmarkStats(0.0, 0.0, 0.0, 0.0, 0.0)
    aligned.columns = ["portfolio", "benchmark"]
    covariance = aligned["portfolio"].cov(aligned["benchmark"])
    variance = aligned["benchmark"].var()
    beta = 0.0 if np.isclose(variance, 0.0) else float(covariance / variance)
    alpha = float(aligned["portfolio"].mean() - beta * aligned["benchmark"].mean())
    active = aligned["portfolio"] - aligned["benchmark"]
    tracking_error = float(active.std(ddof=1))
    information_ratio = (
        0.0 if np.isclose(tracking_error, 0.0) else float(active.mean() / tracking_error)
    )
    hit_rate = float((aligned["portfolio"] > aligned["benchmark"]).mean())
    return BenchmarkStats(beta, alpha, tracking_error, information_ratio, hit_rate)
