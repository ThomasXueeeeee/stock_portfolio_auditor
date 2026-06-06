# SPDX-License-Identifier: MIT
"""Broker symbol to market data ticker resolution."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TickerResolution:
    """Resolved market data ticker and the reason it was chosen."""

    source_symbol: str
    ticker: str
    reason: str


@dataclass(slots=True)
class TickerResolver:
    """Resolve broker symbols into yfinance/Stooq-compatible tickers."""

    overrides: dict[str, str] = field(default_factory=dict)

    def resolve(self, symbol: str, *, currency: str | None = None) -> TickerResolution:
        """Resolve a broker symbol to a price-provider ticker."""
        clean = symbol.strip()
        if clean in self.overrides:
            return TickerResolution(clean, self.overrides[clean], "manual_override")
        if _looks_like_hk_numeric(clean, currency):
            return TickerResolution(clean, f"{clean}.HK", "hk_numeric")
        if "." in clean:
            left, right = clean.split(".", 1)
            if len(right) == 1 and clean.upper() not in _KNOWN_SUFFIXED_EXCHANGES:
                return TickerResolution(clean, f"{left}-{right}", "class_share")
        return TickerResolution(clean, clean, "identity")


def _looks_like_hk_numeric(symbol: str, currency: str | None) -> bool:
    return symbol.isdigit() and (currency is None or currency.upper() == "HKD")


_KNOWN_SUFFIXED_EXCHANGES = {
    "0700.HK",
    "2800.HK",
}
