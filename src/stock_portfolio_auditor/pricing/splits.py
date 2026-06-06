# SPDX-License-Identifier: MIT
"""Synthetic split transaction helpers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.domain.models import AssetKind, Transaction
from stock_portfolio_auditor.pricing.providers.base import PriceProvider


def split_transactions(
    provider: PriceProvider,
    ticker: str,
    start: date,
    end: date,
    *,
    currency: str = "USD",
) -> tuple[Transaction, ...]:
    """Create zero-cash split transactions from provider split ratios."""
    splits = provider.get_splits(ticker, start, end)
    transactions: list[Transaction] = []
    for timestamp, ratio in splits.items():
        transactions.append(
            Transaction(
                trade_date=timestamp.date(),
                action="split",
                symbol=ticker,
                kind=AssetKind.EQUITY,
                currency=currency,
                quantity=Decimal(str(ratio)),
                amount_local=Decimal("0"),
                amount_base=Decimal("0"),
                source_section="synthetic_split",
            )
        )
    return tuple(transactions)
