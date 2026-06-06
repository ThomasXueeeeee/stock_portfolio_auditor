# SPDX-License-Identifier: MIT
"""Benchmark price fetching for report charts.

Strategy
--------
1. Maintain an on-disk parquet cache of *monthly close* prices per ticker at
   ``data_cache/benchmarks/<ticker>.parquet``.
2. On each report build, check whether the cache contains a fresh observation
   for every month-end in the requested window. Monthly closes from prior
   months never change, so once cached they are reused forever.
3. Only the *latest* month is refreshed when the cached value is older than
   one day (or if the window's last month is missing entirely).
4. If the upstream provider fails for the latest month, we still return the
   cached series so the chart isn't blank during a yfinance rate-limit
   incident.

The cache schema is identical to ``CachedProvider``'s parquet layout
(``date``, ``value``) so the files are easy to inspect.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from loguru import logger

from stock_portfolio_auditor.analytics.benchmark import BENCHMARKS
from stock_portfolio_auditor.pricing.providers.factory import build_price_provider

DEFAULT_BENCHMARK_TICKERS = ("SPY", "QQQ", "IWM", "DIA", "MCHI", "EWH", "URTH")
BENCHMARK_CACHE_DIR = Path("data_cache/benchmarks")
_LATEST_MONTH_STALENESS_DAYS = 1


@dataclass(frozen=True, slots=True)
class BenchmarkSeries:
    """A benchmark cumulative return series."""

    ticker: str
    name: str
    dates: tuple[pd.Timestamp, ...]
    cumulative_returns: tuple[float, ...]


def fetch_benchmark_series(
    start: date,
    end: date,
    tickers: tuple[str, ...] = DEFAULT_BENCHMARK_TICKERS,
    *,
    cache_dir: Path | None = None,
) -> list[BenchmarkSeries]:
    """Fetch monthly cumulative returns for each benchmark ticker.

    Reads monthly closes from a local parquet cache when available and only
    refreshes the current (incomplete) month from the network. Network
    failures degrade gracefully: any benchmark whose price data can't be
    refreshed and isn't in the cache is silently skipped.
    """
    if os.getenv("SPA_DISABLE_BENCHMARKS"):
        return []

    cache_root = cache_dir or BENCHMARK_CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)

    provider = build_price_provider()
    series: list[BenchmarkSeries] = []
    needed_months = _monthly_index(start, end)

    for ticker in tickers:
        meta = BENCHMARKS.get(ticker)
        name = meta.name if meta is not None else ticker
        try:
            monthly = _load_monthly_closes(
                ticker=ticker,
                needed_months=needed_months,
                provider=provider,
                cache_path=cache_root / f"{_safe_key(ticker)}.parquet",
            )
        except Exception as exc:  # noqa: BLE001 - benchmark fetch is best-effort
            logger.warning("Skipping benchmark", ticker=ticker, error=str(exc))
            continue
        if monthly.empty or len(monthly) < 2:
            continue
        base = float(monthly.iloc[0])
        if base == 0:
            continue
        cumulative = monthly / base - 1.0
        series.append(
            BenchmarkSeries(
                ticker=ticker,
                name=name,
                dates=tuple(cumulative.index),
                cumulative_returns=tuple(float(value) for value in cumulative.values),
            )
        )
    return series


def _monthly_index(start: date, end: date) -> pd.DatetimeIndex:
    return pd.date_range(
        start=pd.Timestamp(start.year, start.month, 1),
        end=pd.Timestamp(end.year, end.month, 1) + pd.offsets.MonthEnd(0),
        freq="ME",
    )


def _load_monthly_closes(
    *,
    ticker: str,
    needed_months: pd.DatetimeIndex,
    provider: object,
    cache_path: Path,
) -> pd.Series:
    """Return monthly close prices for ``needed_months`` for one ticker."""
    cached = _read_cache(cache_path)
    missing = _missing_months(cached, needed_months)
    if not missing.empty:
        # Fetch from earliest missing month to end of requested window, plus a
        # small lookback so the first month's close is captured cleanly.
        fetch_start = (missing.min() - pd.offsets.MonthBegin(1)).date()
        fetch_end = max(needed_months.max().date(), datetime.now().date())
        try:
            raw_closes = provider.get_close(ticker, fetch_start, fetch_end)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - tolerate provider hiccup if cache has data
            if cached.empty:
                raise
            logger.warning(
                "Provider unavailable; using cached monthly closes",
                ticker=ticker,
                error=str(exc),
            )
            raw_closes = pd.Series(dtype="float64")
        if not raw_closes.empty:
            fresh_monthly = raw_closes.resample("ME").last().dropna()
            cached = _merge_monthly(cached, fresh_monthly)
            _write_cache(cache_path, cached)

    if cached.empty:
        return cached
    return cached.reindex(needed_months).dropna()


def _missing_months(cached: pd.Series, needed_months: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if cached.empty:
        return needed_months
    cached_set = set(cached.index)
    today = datetime.now().date()
    missing_list: list[pd.Timestamp] = []
    for month_end in needed_months:
        # Refresh the current month's close every day; it's still moving.
        is_current_month = month_end.date() >= today.replace(day=1)
        if month_end not in cached_set:
            missing_list.append(month_end)
            continue
        if is_current_month:
            last_refresh = cached.index.max().date()
            if (today - last_refresh).days >= _LATEST_MONTH_STALENESS_DAYS:
                missing_list.append(month_end)
    if not missing_list:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(sorted(set(missing_list)))


def _merge_monthly(cached: pd.Series, fresh: pd.Series) -> pd.Series:
    merged = fresh.copy() if cached.empty else pd.concat([cached, fresh])
    merged = merged.sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged.astype("float64")


def _read_cache(path: Path) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype="float64")
    frame = pd.read_parquet(path)
    if frame.empty:
        return pd.Series(dtype="float64")
    series = frame["value"]
    series.index = pd.to_datetime(frame["date"]).dt.normalize()
    return series.astype("float64").sort_index()


def _write_cache(path: Path, series: pd.Series) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {"date": pd.to_datetime(series.index), "value": series.astype("float64").values}
    )
    frame.to_parquet(path, index=False)


def _safe_key(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_").replace("=", "_").replace(":", "_")


__all__ = [
    "BENCHMARK_CACHE_DIR",
    "BenchmarkSeries",
    "DEFAULT_BENCHMARK_TICKERS",
    "fetch_benchmark_series",
]
