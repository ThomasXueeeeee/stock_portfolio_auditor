# SPDX-License-Identifier: MIT
"""Stooq fallback price provider."""

from __future__ import annotations

from datetime import date

import pandas as pd
from pandas_datareader import data as web

from stock_portfolio_auditor.domain.errors import PriceProviderError
from stock_portfolio_auditor.pricing.providers.base import PriceProvider


class StooqProvider(PriceProvider):
    """Fallback provider backed by Stooq through pandas-datareader."""

    name = "stooq"

    def get_close(self, ticker: str, start: date, end: date) -> pd.Series:
        """Return close prices from Stooq."""
        candidates = _stooq_symbol_candidates(ticker)
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                data = web.DataReader(candidate, "stooq", start, end)
            except Exception as exc:  # pragma: no cover - network/backend failure
                last_error = exc
                continue
            if data is None or data.empty or "Close" not in data:
                continue
            series = data["Close"].sort_index()
            series.index = pd.to_datetime(series.index).tz_localize(None).normalize()
            cleaned = series.astype("float64").dropna()
            if not cleaned.empty:
                return cleaned
        raise PriceProviderError(
            "stooq returned no close data",
            {
                "ticker": ticker,
                "candidates": candidates,
                "error": str(last_error) if last_error else None,
            },
        )

    def get_splits(self, ticker: str, start: date, end: date) -> pd.Series:
        """Stooq split data is not currently used; return empty series."""
        return pd.Series(dtype="float64")

    def get_fx(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        """Return FX via Stooq when available.

        Stooq quotes most pairs as ``USD<other>`` (USDHKD, USDJPY, ...) rather
        than ``<other>USD``. We try the direct pair first; if that's missing we
        fall back to the inverse pair and reciprocate the series. The function
        returns "1 base = X quote", so a HKD->USD lookup means we want each
        HKD's price in USD; if Stooq only has USDHKD we invert it.
        """
        base_u = base.upper()
        quote_u = quote.upper()
        if base_u == quote_u:
            index = pd.date_range(start, end, freq="D")
            return pd.Series(1.0, index=index, name=f"{base_u}{quote_u}")
        direct = f"{base.lower()}{quote.lower()}"
        try:
            return self.get_close(direct, start, end)
        except PriceProviderError:
            inverse = f"{quote.lower()}{base.lower()}"
            inverse_series = self.get_close(inverse, start, end)
            return (1.0 / inverse_series).rename(f"{base_u}{quote_u}")


def _stooq_symbol_candidates(ticker: str) -> list[str]:
    """Return likely Stooq ticker variants for a given input symbol."""
    raw = ticker.strip()
    lowered = raw.lower()
    candidates = [lowered]
    if "." not in lowered and "-" not in lowered:
        candidates.append(f"{lowered}.us")
    if "." in lowered and not lowered.endswith(".us"):
        candidates.append(lowered.replace(".", "-"))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique
