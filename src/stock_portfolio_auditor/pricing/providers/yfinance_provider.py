# SPDX-License-Identifier: MIT
"""Yahoo Finance price provider."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from stock_portfolio_auditor.domain.errors import PriceProviderError
from stock_portfolio_auditor.pricing.providers.base import PriceProvider


class YFinanceProvider(PriceProvider):
    """Market data provider backed by yfinance."""

    name = "yfinance"

    def get_close(self, ticker: str, start: date, end: date) -> pd.Series:
        """Return adjusted close when available, otherwise close."""
        try:
            data = yf.download(
                ticker,
                start=start.isoformat(),
                end=_exclusive_end(end).isoformat(),
                auto_adjust=False,
                progress=False,
            )
        except Exception as exc:  # pragma: no cover - network/backend failure
            raise PriceProviderError("yfinance close download failed", {"ticker": ticker}) from exc
        if data.empty:
            raise PriceProviderError("yfinance returned no close data", {"ticker": ticker})
        column = "Adj Close" if "Adj Close" in data.columns else "Close"
        series = data[column]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[:, 0]
        return _clean_series(series)

    def get_splits(self, ticker: str, start: date, end: date) -> pd.Series:
        """Return split ratios from yfinance."""
        try:
            splits = yf.Ticker(ticker).splits
        except Exception as exc:  # pragma: no cover - network/backend failure
            raise PriceProviderError("yfinance split download failed", {"ticker": ticker}) from exc
        if splits.empty:
            return pd.Series(dtype="float64")
        splits.index = pd.to_datetime(splits.index).tz_localize(None).normalize()
        mask = (splits.index.date >= start) & (splits.index.date <= end)
        return _clean_series(splits.loc[mask])

    def get_fx(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        """Return FX rate using Yahoo's BASEQUOTE=X convention."""
        if base.upper() == quote.upper():
            index = pd.date_range(start, end, freq="D")
            return pd.Series(1.0, index=index, name=f"{base}{quote}")
        ticker = f"{base.upper()}{quote.upper()}=X"
        return self.get_close(ticker, start, end)


def _exclusive_end(value: date) -> date:
    return value + timedelta(days=1)


def _clean_series(series: pd.Series) -> pd.Series:
    out = series.copy()
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out = out.astype("float64").sort_index()
    return out.dropna()
