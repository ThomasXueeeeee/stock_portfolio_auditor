# SPDX-License-Identifier: MIT
"""Persist parsed statements to CSV for inspection and reuse.

Two layers of artifacts are produced under ``data_cache/parsed/``:

* **Per account** files
    - ``<account>_nav.csv``           — one row per statement (period_end, beginning/ending NAV, net flows)
    - ``<account>_holdings.csv``      — one row per (period_end, symbol) holding snapshot
    - ``<account>_transactions.csv``  — every transaction with action, amount, bucket tags
* **Pooled aggregate** files (all accounts combined into a single sheet)
    - ``pooled_nav.csv``
    - ``pooled_holdings.csv``
    - ``pooled_transactions.csv``

These files are intended for two purposes:

1. **Transparency** — you can open them in Excel / Pandas and verify the
   parser interpreted each statement correctly. If the report is showing a
   suspect number, the CSV is the first place to look.
2. **Decoupling** — analytics functions only need a few columns and can be
   tested against synthetic CSVs without dragging the parser stack along.

Everything is written as plain CSV with consistent column names; Decimal
fields are serialized as strings to avoid float drift.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from stock_portfolio_auditor.domain.models import Holding, Statement, Transaction

DEFAULT_OUTPUT_DIR = Path("data_cache/parsed")

NAV_COLUMNS = (
    "account_label",
    "broker",
    "base_currency",
    "period_start",
    "period_end",
    "frequency",
    "beginning_value_base",
    "ending_value_base",
    "external_cash_flow",
    "parser_version",
    "source_path",
)

HOLDING_COLUMNS = (
    "account_label",
    "period_end",
    "symbol",
    "description",
    "kind",
    "currency",
    "exchange",
    "isin",
    "figi",
    "quantity",
    "market_value_local",
    "market_value_base",
    "cost_basis_local",
    "cost_basis_base",
)

TRANSACTION_COLUMNS = (
    "account_label",
    "period_end",
    "trade_date",
    "settle_date",
    "action",
    "symbol",
    "kind",
    "currency",
    "quantity",
    "price",
    "amount_local",
    "amount_base",
    "commission",
    "fees",
    "cost_bucket",
    "income_bucket",
    "is_external_cash_flow",
    "source_section",
    "source_description",
)


def write_parsed_csvs(
    statements: Iterable[Statement],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> dict[str, Path]:
    """Write per-account and pooled CSVs for a list of statements.

    Returns a dict mapping logical artifact name to its path so callers can
    log or test against the generated files.
    """
    statements = list(statements)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    by_account: dict[str, list[Statement]] = {}
    for stmt in statements:
        by_account.setdefault(stmt.account_label, []).append(stmt)

    paths: dict[str, Path] = {}

    # Per-account
    for account, account_stmts in by_account.items():
        account_stmts.sort(key=lambda stmt: stmt.period_end)
        safe = _safe_account(account)
        nav_path = output_path / f"{safe}_nav.csv"
        holdings_path = output_path / f"{safe}_holdings.csv"
        transactions_path = output_path / f"{safe}_transactions.csv"
        _write_nav(nav_path, account_stmts)
        _write_holdings(holdings_path, account_stmts)
        _write_transactions(transactions_path, account_stmts)
        paths[f"{account}::nav"] = nav_path
        paths[f"{account}::holdings"] = holdings_path
        paths[f"{account}::transactions"] = transactions_path

    # Pooled
    pooled_nav = output_path / "pooled_nav.csv"
    pooled_holdings = output_path / "pooled_holdings.csv"
    pooled_transactions = output_path / "pooled_transactions.csv"
    _write_nav(pooled_nav, sorted(statements, key=lambda s: (s.period_end, s.account_label)))
    _write_holdings(
        pooled_holdings, sorted(statements, key=lambda s: (s.period_end, s.account_label))
    )
    _write_transactions(
        pooled_transactions, sorted(statements, key=lambda s: (s.period_end, s.account_label))
    )
    paths["pooled::nav"] = pooled_nav
    paths["pooled::holdings"] = pooled_holdings
    paths["pooled::transactions"] = pooled_transactions

    return paths


def _safe_account(label: str) -> str:
    return label.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")


def _write_nav(path: Path, statements: list[Statement]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=NAV_COLUMNS)
        writer.writeheader()
        for stmt in statements:
            net_cf = sum(
                (txn.amount_base for txn in stmt.transactions if txn.is_external_cash_flow),
                Decimal("0"),
            )
            writer.writerow(
                {
                    "account_label": stmt.account_label,
                    "broker": stmt.broker,
                    "base_currency": stmt.base_currency,
                    "period_start": stmt.period_start.isoformat(),
                    "period_end": stmt.period_end.isoformat(),
                    "frequency": stmt.frequency,
                    "beginning_value_base": str(stmt.beginning_value_base),
                    "ending_value_base": str(stmt.ending_value_base),
                    "external_cash_flow": str(net_cf),
                    "parser_version": stmt.parser_version,
                    "source_path": stmt.source_path or "",
                }
            )


def _write_holdings(path: Path, statements: list[Statement]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HOLDING_COLUMNS)
        writer.writeheader()
        for stmt in statements:
            for holding in stmt.holdings:
                writer.writerow(_holding_row(stmt, holding))


def _write_transactions(path: Path, statements: list[Statement]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRANSACTION_COLUMNS)
        writer.writeheader()
        for stmt in statements:
            for txn in stmt.transactions:
                writer.writerow(_transaction_row(stmt, txn))


def _holding_row(stmt: Statement, holding: Holding) -> dict[str, str]:
    return {
        "account_label": stmt.account_label,
        "period_end": stmt.period_end.isoformat(),
        "symbol": holding.symbol,
        "description": holding.description,
        "kind": holding.kind.value,
        "currency": holding.currency,
        "exchange": holding.exchange or "",
        "isin": holding.isin or "",
        "figi": holding.figi or "",
        "quantity": str(holding.quantity),
        "market_value_local": str(holding.market_value_local),
        "market_value_base": str(holding.market_value_base),
        "cost_basis_local": (
            str(holding.cost_basis_local) if holding.cost_basis_local is not None else ""
        ),
        "cost_basis_base": (
            str(holding.cost_basis_base) if holding.cost_basis_base is not None else ""
        ),
    }


def _transaction_row(stmt: Statement, txn: Transaction) -> dict[str, str]:
    return {
        "account_label": stmt.account_label,
        "period_end": stmt.period_end.isoformat(),
        "trade_date": txn.trade_date.isoformat(),
        "settle_date": txn.settle_date.isoformat() if txn.settle_date else "",
        "action": txn.action,
        "symbol": txn.symbol or "",
        "kind": txn.kind.value,
        "currency": txn.currency,
        "quantity": str(txn.quantity),
        "price": str(txn.price) if txn.price is not None else "",
        "amount_local": str(txn.amount_local),
        "amount_base": str(txn.amount_base),
        "commission": str(txn.commission),
        "fees": str(txn.fees),
        "cost_bucket": txn.cost_bucket.value if txn.cost_bucket else "",
        "income_bucket": txn.income_bucket.value if txn.income_bucket else "",
        "is_external_cash_flow": "true" if txn.is_external_cash_flow else "false",
        "source_section": txn.source_section or "",
        "source_description": txn.source_description or "",
    }
