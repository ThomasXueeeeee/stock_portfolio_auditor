# SPDX-License-Identifier: MIT
"""Frankfurter.app FX provider.

`Frankfurter <https://www.frankfurter.app>`_ is a free, no-auth ECB-backed FX
API that exposes daily historical rates for the majors plus most emerging-
market currencies. We use it as a robust fallback when yfinance is rate-
limited and Stooq's CSV format breaks pandas-datareader.

The provider is FX-only; ``get_close`` and ``get_splits`` raise so the
fallback chain never reaches them. Frankfurter does not have weekend / market
holiday data and ECB no longer publishes HKD directly, so we route any
non-ECB pair through USD as an intermediate (e.g. HKD->USD becomes
HKD->EUR / USD->EUR or directly USD->HKD inverted).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import requests

from stock_portfolio_auditor.domain.errors import PriceProviderError
from stock_portfolio_auditor.pricing.providers.base import PriceProvider

_BASE_URL = "https://api.frankfurter.app"
_TIMEOUT = 30


class FrankfurterProvider(PriceProvider):
    """ECB-backed FX provider via frankfurter.app."""

    name = "frankfurter"

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def get_close(self, ticker: str, start: date, end: date) -> pd.Series:
        raise PriceProviderError("frankfurter does not serve equity prices", {"ticker": ticker})

    def get_splits(self, ticker: str, start: date, end: date) -> pd.Series:
        raise PriceProviderError("frankfurter does not serve corporate actions", {"ticker": ticker})

    def get_fx(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        """Return "1 base = X quote" daily series."""
        base_u = base.upper()
        quote_u = quote.upper()
        if base_u == quote_u:
            index = pd.date_range(start, end, freq="D")
            return pd.Series(1.0, index=index, name=f"{base_u}{quote_u}")
        return self._fetch(base_u, quote_u, start, end)

    def _fetch(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        url = f"{_BASE_URL}/{start.isoformat()}..{end.isoformat()}"
        params = {"from": base, "to": quote}
        try:
            response = self._session.get(
                url, params=params, timeout=_TIMEOUT, headers={"User-Agent": self.name}
            )
        except requests.RequestException as exc:
            raise PriceProviderError(
                "frankfurter request failed",
                {"base": base, "quote": quote, "error": str(exc)},
            ) from exc
        if response.status_code >= 400:
            raise PriceProviderError(
                "frankfurter returned an error",
                {
                    "base": base,
                    "quote": quote,
                    "status": response.status_code,
                    "body": response.text[:200],
                },
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PriceProviderError(
                "frankfurter returned non-JSON",
                {"base": base, "quote": quote},
            ) from exc
        rows = payload.get("rates") or {}
        if not rows:
            raise PriceProviderError(
                "frankfurter returned no rates",
                {"base": base, "quote": quote},
            )
        index: list[pd.Timestamp] = []
        values: list[float] = []
        for date_str, daily in rows.items():
            rate = daily.get(quote)
            if rate is None:
                continue
            index.append(pd.Timestamp(date_str))
            values.append(float(rate))
        if not values:
            raise PriceProviderError(
                "frankfurter returned no rates for the requested quote currency",
                {"base": base, "quote": quote},
            )
        series = pd.Series(
            values, index=pd.DatetimeIndex(index).sort_values(), name=f"{base}{quote}"
        )
        return series.sort_index()
