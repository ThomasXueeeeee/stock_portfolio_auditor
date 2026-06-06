# SPDX-License-Identifier: MIT
"""Provider factory based on environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from stock_portfolio_auditor.pricing.providers.base import PriceProvider
from stock_portfolio_auditor.pricing.providers.cached import CachedProvider
from stock_portfolio_auditor.pricing.providers.chain import FallbackProvider
from stock_portfolio_auditor.pricing.providers.frankfurter_provider import FrankfurterProvider
from stock_portfolio_auditor.pricing.providers.stooq_provider import StooqProvider
from stock_portfolio_auditor.pricing.providers.yfinance_provider import YFinanceProvider


def build_price_provider(cache_dir: str | Path = "data_cache/prices") -> PriceProvider:
    """Build the active provider chain.

    ``SPA_PRICE_PROVIDER`` can be ``yfinance``, ``stooq``, ``frankfurter``, or
    ``chain`` (default). The chain tries yfinance first, falls back to Stooq
    for equities, and uses Frankfurter for FX when both upstream providers
    fail (yfinance rate-limits FX quotes heavily, Stooq's pandas-datareader
    integration is currently broken for FX). ``SPA_OFFLINE=1`` is honored by
    the cache layer.
    """
    provider_name = os.getenv("SPA_PRICE_PROVIDER", "chain").lower()
    if provider_name == "yfinance":
        provider: PriceProvider = YFinanceProvider()
    elif provider_name == "stooq":
        provider = StooqProvider()
    elif provider_name == "frankfurter":
        provider = FrankfurterProvider()
    else:
        provider = FallbackProvider((YFinanceProvider(), StooqProvider(), FrankfurterProvider()))
    return CachedProvider(provider, cache_dir=cache_dir)
