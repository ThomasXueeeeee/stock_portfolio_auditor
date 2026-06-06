# SPDX-License-Identifier: MIT
"""Utilities that turn parsed statements into a coherent account history."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from loguru import logger

from stock_portfolio_auditor.domain.models import Statement, Transaction
from stock_portfolio_auditor.ingestion.coverage import MonthKey, covered_months


@dataclass(frozen=True, slots=True)
class ReconstructedAccount:
    """A deduplicated statement and transaction set for one account."""

    account_label: str
    statements: tuple[Statement, ...]
    transactions: tuple[Transaction, ...]


def deduplicate_overlapping_statements(statements: list[Statement]) -> list[ReconstructedAccount]:
    """Deduplicate monthly/quarterly/annual overlaps by month.

    For each account-month the most granular statement owns that month:
    monthly > quarterly > annual. Transactions from coarser statements are
    dropped when their trade month is owned by a finer statement.
    """
    grouped: dict[str, list[Statement]] = defaultdict(list)
    for statement in statements:
        grouped[statement.account_label].append(statement)

    reconstructed: list[ReconstructedAccount] = []
    for account_label, account_statements in grouped.items():
        owner_by_month = _month_owners(account_statements)
        kept_transactions: list[Transaction] = []
        for statement in account_statements:
            owned_months = {month for month, owner in owner_by_month.items() if owner is statement}
            for transaction in statement.transactions:
                key = (transaction.trade_date.year, transaction.trade_date.month)
                if key in owned_months:
                    kept_transactions.append(transaction)
                else:
                    logger.warning(
                        "Dropped overlapping transaction from coarser statement",
                        account_label=account_label,
                        symbol=transaction.symbol,
                        trade_date=str(transaction.trade_date),
                        source_path=statement.source_path,
                    )
        reconstructed.append(
            ReconstructedAccount(
                account_label=account_label,
                statements=tuple(sorted(account_statements, key=lambda stmt: stmt.period_end)),
                transactions=tuple(sorted(kept_transactions, key=lambda txn: txn.trade_date)),
            )
        )
    return reconstructed


def _month_owners(statements: list[Statement]) -> dict[MonthKey, Statement]:
    candidates: dict[MonthKey, list[Statement]] = defaultdict(list)
    for statement in statements:
        for month in covered_months(statement):
            candidates[month].append(statement)

    owners: dict[MonthKey, Statement] = {}
    for month, month_statements in candidates.items():
        owners[month] = min(
            month_statements,
            key=lambda stmt: (_frequency_rank(stmt.frequency), stmt.period_end),
        )
    return owners


def _frequency_rank(freq: str) -> int:
    return {"M": 0, "Q": 1, "A": 2}.get(freq, 99)
