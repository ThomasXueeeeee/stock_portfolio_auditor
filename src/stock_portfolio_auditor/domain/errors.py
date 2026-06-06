# SPDX-License-Identifier: MIT
"""Custom exception hierarchy for Stock Portfolio Auditor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SPAError(Exception):
    """Base exception carrying structured context for UI and logs."""

    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        if not self.context:
            return self.message
        context = ", ".join(f"{key}={value!r}" for key, value in self.context.items())
        return f"{self.message} ({context})"


class ParserError(SPAError):
    """Base class for broker statement parser failures."""


class BrokerNotDetectedError(ParserError):
    """Raised when a file cannot be mapped to a supported broker."""


class FormatNotSupportedError(ParserError):
    """Raised when a detected broker/file format combination is unsupported."""


class StatementCorruptError(ParserError):
    """Raised when a statement appears unreadable or internally inconsistent."""


class PeriodDetectionError(ParserError):
    """Raised when a statement period cannot be parsed."""


class CoverageGapError(SPAError):
    """Raised when strict audit coverage finds uncovered months."""


class TickerResolutionError(SPAError):
    """Raised when a parsed broker symbol cannot be mapped to a price ticker."""


class PriceProviderError(SPAError):
    """Raised when price or FX data cannot be loaded."""


class MissingFxRateError(SPAError):
    """Raised when fx_convert_statements cannot obtain every FX rate required
    to translate the statements' non-base-currency holdings and transactions
    to USD. Carries a structured ``context`` with the missing
    ``(currency, base, date)`` triples so a CLI can render a clear retry
    message ("yfinance rate-limited HKD->USD for 2024-03-15, wait ~15 min
    and retry"). The pipeline raises this *eagerly* -- silently dropping
    untranslated rows would leak HKD-nominal values into USD-denominated
    analytics (turnover, concentration, dollar P&L) by an ~8x factor."""


class ReconciliationError(SPAError):
    """Raised when parsed values do not reconcile to broker statement totals."""
