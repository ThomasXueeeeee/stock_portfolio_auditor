# SPDX-License-Identifier: MIT
"""Price provider interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class PriceProvider(ABC):
    """Abstract market data provider."""

    name: str

    @abstractmethod
    def get_close(self, ticker: str, start: date, end: date) -> pd.Series:
        """Return close prices indexed by date."""

    @abstractmethod
    def get_splits(self, ticker: str, start: date, end: date) -> pd.Series:
        """Return split ratios indexed by date."""

    @abstractmethod
    def get_fx(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        """Return FX close series for base/quote."""
