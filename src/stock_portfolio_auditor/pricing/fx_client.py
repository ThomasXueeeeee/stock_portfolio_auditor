# SPDX-License-Identifier: MIT
"""FX series helper."""

from __future__ import annotations

from datetime import date

import pandas as pd

from stock_portfolio_auditor.pricing.providers.base import PriceProvider


def get_fx_to_base(
    provider: PriceProvider,
    currency: str,
    base_currency: str,
    start: date,
    end: date,
) -> pd.Series:
    """Return a currency-to-base FX series."""
    return provider.get_fx(currency.upper(), base_currency.upper(), start, end)
