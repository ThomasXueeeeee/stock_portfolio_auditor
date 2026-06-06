# SPDX-License-Identifier: MIT
"""Fallback provider composition."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pandas as pd
from loguru import logger

from stock_portfolio_auditor.domain.errors import PriceProviderError
from stock_portfolio_auditor.pricing.providers.base import PriceProvider


class FallbackProvider(PriceProvider):
    """Try providers in order until one returns data."""

    name = "fallback"

    def __init__(self, providers: tuple[PriceProvider, ...]) -> None:
        self.providers = providers

    def get_close(self, ticker: str, start: date, end: date) -> pd.Series:
        return self._try(lambda provider: provider.get_close(ticker, start, end), ticker)

    def get_splits(self, ticker: str, start: date, end: date) -> pd.Series:
        return self._try(lambda provider: provider.get_splits(ticker, start, end), ticker)

    def get_fx(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        return self._try(
            lambda provider: provider.get_fx(base, quote, start, end), f"{base}/{quote}"
        )

    def _try(self, fn: Callable[[PriceProvider], pd.Series], label: str) -> pd.Series:
        errors: list[str] = []
        for provider in self.providers:
            try:
                series = fn(provider)
                if not series.empty:
                    return series
            except PriceProviderError as exc:
                errors.append(str(exc))
                logger.warning(
                    "Price provider failed", provider=provider.name, label=label, error=str(exc)
                )
        raise PriceProviderError("All price providers failed", {"label": label, "errors": errors})
