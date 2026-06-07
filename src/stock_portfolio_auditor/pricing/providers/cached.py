# SPDX-License-Identifier: MIT
"""Parquet-backed provider cache.

Behaviour:
    * If the local cache already covers the requested ``[start, end]`` window
      (allowing for non-trading days and weekends), the cached slice is
      returned without hitting the network.
    * Otherwise the upstream provider is queried; the result is merged into
      the cache and the requested slice is returned.
    * If the upstream provider raises (e.g. yfinance rate limit) and the
      cache contains *any* overlapping data, the cached slice is returned
      instead of propagating the error.

The cache stores one parquet file per series key with columns ``date`` and
``value`` so files are easy to inspect with any DataFrame tool.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

from stock_portfolio_auditor.pricing.providers.base import PriceProvider

# Reasonable buffer for non-trading-day padding (weekends and short holidays).
_COVERAGE_BUFFER_DAYS = 7
# A series whose newest data is older than this many days will be refreshed
# even if the requested window is "covered" by older cached data.
_MAX_STALENESS_DAYS = 1
# Maximum tolerable gap between two consecutive observations inside the
# requested window. A weekend + national holiday bridge is at most 4-5
# days; anything beyond ~10 days is almost certainly a partial-fetch
# artefact from a previous rate-limited run and should NOT be served
# from cache silently -- the analytics downstream sum rows on those
# dates and a missing FX rate inflates HKD/JPY trade nominals by ~8x.
_MAX_INTERNAL_GAP_DAYS = 10


class CachedProvider(PriceProvider):
    """Cache provider responses as one parquet file per series key."""

    name = "cached"

    def __init__(
        self,
        provider: PriceProvider,
        cache_dir: str | Path = "data_cache/prices",
        *,
        max_staleness_days: int = _MAX_STALENESS_DAYS,
    ) -> None:
        self.provider = provider
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_staleness_days = max_staleness_days

    def get_close(self, ticker: str, start: date, end: date) -> pd.Series:
        return self._get_or_fetch("close", ticker, start, end, self.provider.get_close)

    def get_splits(self, ticker: str, start: date, end: date) -> pd.Series:
        return self._get_or_fetch("splits", ticker, start, end, self.provider.get_splits)

    def get_fx(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        key = f"{base.upper()}{quote.upper()}"
        return self._get_or_fetch(
            "fx",
            key,
            start,
            end,
            lambda _ticker, s, e: self.provider.get_fx(base, quote, s, e),
        )

    def _get_or_fetch(
        self,
        kind: str,
        ticker: str,
        start: date,
        end: date,
        fetcher: Callable[[str, date, date], pd.Series],
    ) -> pd.Series:
        path = self.cache_dir / kind / f"{_safe_key(ticker)}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        cached = _read_series(path)

        if _covers(cached, start, end, self.max_staleness_days):
            return _slice(cached, start, end)

        try:
            series = fetcher(ticker, start, end)
        except Exception as exc:  # noqa: BLE001 - upstream may be flaky
            if not cached.empty and _has_any_overlap(cached, start, end):
                logger.warning(
                    "Price provider failed; falling back to cached series",
                    ticker=ticker,
                    kind=kind,
                    error=str(exc),
                )
                return _slice(cached, start, end)
            raise

        merged = series if cached.empty else pd.concat([cached, series]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        _write_series(path, merged)
        return _slice(merged, start, end)


def _read_series(path: Path) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype="float64")
    frame = pd.read_parquet(path)
    series = frame["value"]
    series.index = pd.to_datetime(frame["date"]).dt.normalize()
    return series.astype("float64").sort_index()


def _write_series(path: Path, series: pd.Series) -> None:
    frame = pd.DataFrame(
        {"date": pd.to_datetime(series.index), "value": series.astype("float64").values}
    )
    frame.to_parquet(path, index=False)


def _covers(series: pd.Series, start: date, end: date, max_staleness_days: int) -> bool:
    """Return True when cached data spans the requested window with some tolerance.

    Three checks have to pass:

    1. The cache's earliest observation must be within
       ``_COVERAGE_BUFFER_DAYS`` of the requested ``start`` (so we can
       bridge a weekend / holiday at the leading edge).
    2. The cache's latest observation must be within
       ``_COVERAGE_BUFFER_DAYS`` of the requested ``end``, and not stale
       relative to today if ``end`` is current.
    3. The cache must have no *internal* gap larger than
       ``_MAX_INTERNAL_GAP_DAYS`` inside the requested ``[start, end]``
       window. A partial-fetch artefact -- e.g. a Jan-2024 row followed
       by a Dec-2025 row with 715 days in between -- has matching
       endpoints but a 2-year hole in the middle, and silently
       serving that as "covered" would let analytics use stale FX
       for any trade landing in the hole. Refreshing the cache fills
       the gap.
    """
    if series.empty:
        return False
    earliest = series.index.min().date()
    latest = series.index.max().date()
    if earliest > start + timedelta(days=_COVERAGE_BUFFER_DAYS):
        return False
    if latest < end - timedelta(days=_COVERAGE_BUFFER_DAYS):
        return False
    today = datetime.now().date()
    if end >= today - timedelta(days=1) and (today - latest).days > max_staleness_days:
        return False
    # Internal-gap check: pull only the observations that fall inside
    # ``[start, end]`` and look for any pair of consecutive timestamps
    # more than ``_MAX_INTERNAL_GAP_DAYS`` apart. A cache that satisfies
    # the endpoint checks but has a multi-month hole here cannot serve
    # the gap dates correctly, so we treat it as not-covered and let the
    # upstream provider be re-queried to fill in.
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    inside = series[(series.index >= start_ts) & (series.index <= end_ts)]
    if len(inside) < 2:
        # If the cache covers the endpoints (per checks above) but has
        # fewer than 2 in-window points, the requested window is too
        # narrow to look for gaps -- treat as covered.
        return True
    diffs_days = inside.index.to_series().diff().dt.days.dropna()
    return not (diffs_days > _MAX_INTERNAL_GAP_DAYS).any()


def _has_any_overlap(series: pd.Series, start: date, end: date) -> bool:
    if series.empty:
        return False
    earliest = series.index.min().date()
    latest = series.index.max().date()
    return not (latest < start or earliest > end)


def _slice(series: pd.Series, start: date, end: date) -> pd.Series:
    index = pd.to_datetime(series.index)
    mask = (index.date >= start) & (index.date <= end)
    return series.loc[mask]


def _safe_key(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_").replace("=", "_").replace(":", "_")
