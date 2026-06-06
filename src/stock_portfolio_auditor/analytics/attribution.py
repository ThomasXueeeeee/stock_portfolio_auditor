# SPDX-License-Identifier: MIT
"""Return and PnL source attribution."""

from __future__ import annotations

from dataclasses import dataclass

from stock_portfolio_auditor.domain.models import AssetKind, IncomeBucket, Statement


@dataclass(frozen=True, slots=True)
class PnlBreakdown:
    """Dollar PnL by report source bucket."""

    price: float = 0.0
    dividends: float = 0.0
    options: float = 0.0
    fx: float = 0.0
    lending: float = 0.0
    tcost: float = 0.0

    @property
    def total(self) -> float:
        """Sum all components."""
        return self.price + self.dividends + self.options + self.fx + self.lending - self.tcost


def pnl_breakdown(statements: list[Statement]) -> PnlBreakdown:
    """Build a PnL breakdown from parsed transactions and statement NAV changes.

    ``price`` is solved as the residual

        price = (nav_change - external_cash_flow)
                - dividends - options - lending - fx + tcost

    so the buckets sum to the actual P&L for the window. Without the
    cash-flow subtraction a year with a $200K deposit would attribute the
    $200K to Price PnL, overstating the residual by the cumulative external
    flows across all accounts.
    """
    dividends = 0.0
    options = 0.0
    lending = 0.0
    tcost = 0.0
    external_cf = 0.0
    for statement in statements:
        for transaction in statement.transactions:
            amount = float(transaction.amount_base)
            if transaction.is_external_cash_flow:
                external_cf += amount
                continue
            if transaction.income_bucket is IncomeBucket.CASH_DIVIDEND:
                dividends += amount
            elif transaction.income_bucket is IncomeBucket.LENDING_INCOME:
                lending += amount
            elif transaction.kind is AssetKind.OPTION:
                options += amount
            if transaction.cost_bucket is not None:
                # ``commission`` and ``fees`` are always populated as positive
                # absolute values by the broker parsers (the Schwab Expense
                # path sets ``fees=abs(amount)``, IBKR Trade rows pass
                # ``abs(row[11])`` into commission, etc.), so a clean sum is
                # the right cost-drag value. The previous ``or abs(amount)``
                # fallback would have silently counted the full transaction
                # principal as cost drag for any future cost_bucket row with
                # zero charges — a sharp edge to leave in.
                tcost += abs(float(transaction.commission)) + abs(float(transaction.fees))
    nav_change = sum(
        float(statement.ending_value_base - statement.beginning_value_base)
        for statement in statements
    )
    price = (nav_change - external_cf) - dividends - options - lending + tcost
    return PnlBreakdown(
        price=price, dividends=dividends, options=options, lending=lending, tcost=tcost
    )


def contribution_bps(breakdown: PnlBreakdown, beginning_nav: float) -> dict[str, float]:
    """Convert PnL components into basis-point contributions."""
    if beginning_nav == 0:
        return {key: 0.0 for key in ("price", "dividends", "options", "fx", "lending", "tcost")}
    return {
        "price": breakdown.price / beginning_nav * 10_000,
        "dividends": breakdown.dividends / beginning_nav * 10_000,
        "options": breakdown.options / beginning_nav * 10_000,
        "fx": breakdown.fx / beginning_nav * 10_000,
        "lending": breakdown.lending / beginning_nav * 10_000,
        "tcost": -breakdown.tcost / beginning_nav * 10_000,
    }
