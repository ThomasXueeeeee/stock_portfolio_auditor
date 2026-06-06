# SPDX-License-Identifier: MIT
"""Transaction cost and tax ledger."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from stock_portfolio_auditor.domain.models import CostBucket, Statement


@dataclass(frozen=True, slots=True)
class CostLedgerRow:
    """Aggregated cost row."""

    bucket: CostBucket
    amount: float
    bps: float


def cost_ledger(statements: list[Statement], *, average_nav: float) -> tuple[CostLedgerRow, ...]:
    """Aggregate transaction costs and taxes by bucket."""
    totals: dict[CostBucket, float] = defaultdict(float)
    for statement in statements:
        for transaction in statement.transactions:
            if transaction.cost_bucket is None:
                continue
            amount = float(abs(transaction.commission) + abs(transaction.fees))
            if amount == 0:
                amount = abs(float(transaction.amount_base))
            totals[transaction.cost_bucket] += amount
    return tuple(
        CostLedgerRow(bucket=bucket, amount=amount, bps=_bps(amount, average_nav))
        for bucket, amount in sorted(totals.items(), key=lambda item: item[0].value)
    )


def total_cost_drag_bps(rows: tuple[CostLedgerRow, ...]) -> float:
    """Total cost drag in basis points."""
    return sum(row.bps for row in rows)


def _bps(amount: float, average_nav: float) -> float:
    if average_nav == 0:
        return 0.0
    return amount / average_nav * 10_000
