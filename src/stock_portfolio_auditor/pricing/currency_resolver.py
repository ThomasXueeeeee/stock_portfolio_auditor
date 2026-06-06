# SPDX-License-Identifier: MIT
"""Trading-currency resolution for market symbols."""

from __future__ import annotations

from dataclasses import dataclass, field

SUFFIX_CURRENCIES = {
    ".HK": "HKD",
    ".T": "JPY",
    ".TO": "CAD",
    ".V": "CAD",
    ".L": "GBP",
    ".PA": "EUR",
    ".AS": "EUR",
    ".DE": "EUR",
    ".MI": "EUR",
    ".SW": "CHF",
    ".SS": "CNY",
    ".SZ": "CNY",
}


@dataclass(slots=True)
class CurrencyResolver:
    """Resolve ticker currency using broker-provided values and suffix rules."""

    overrides: dict[str, str] = field(default_factory=dict)

    def resolve(self, ticker: str, *, broker_currency: str | None = None) -> str:
        """Return ISO-style trading currency for a ticker."""
        if ticker in self.overrides:
            return self.overrides[ticker].upper()
        if broker_currency:
            return broker_currency.upper()
        upper = ticker.upper()
        for suffix, currency in sorted(SUFFIX_CURRENCIES.items(), key=lambda item: -len(item[0])):
            if upper.endswith(suffix):
                return currency
        return "USD"
