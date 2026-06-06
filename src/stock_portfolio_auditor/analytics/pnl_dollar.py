# SPDX-License-Identifier: MIT
"""Dollar PnL decomposition utilities."""

from __future__ import annotations

from dataclasses import dataclass

from stock_portfolio_auditor.analytics.attribution import PnlBreakdown


@dataclass(frozen=True, slots=True)
class DollarPnlRow:
    """Single row for the dollar PnL table."""

    label: str
    amount: float


def waterfall_rows(breakdown: PnlBreakdown) -> tuple[DollarPnlRow, ...]:
    """Return waterfall rows in report order."""
    return (
        DollarPnlRow("Equity price", breakdown.price),
        DollarPnlRow("Dividends", breakdown.dividends),
        DollarPnlRow("Options", breakdown.options),
        DollarPnlRow("FX", breakdown.fx),
        DollarPnlRow("Lending", breakdown.lending),
        DollarPnlRow("Cost drag", -breakdown.tcost),
        DollarPnlRow("Total PnL", breakdown.total),
    )
