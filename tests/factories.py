from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.domain.models import AssetKind, Statement, Transaction


def make_statement(
    *,
    account_label: str = "acct",
    start: date,
    end: date,
    frequency: str,
    transactions: tuple[Transaction, ...] = (),
) -> Statement:
    return Statement(
        account_label=account_label,
        broker="ibkr",
        base_currency="USD",
        period_start=start,
        period_end=end,
        frequency=frequency,
        beginning_value_base=Decimal("100"),
        ending_value_base=Decimal("110"),
        transactions=transactions,
        parser_version=1,
        raw_text_hash="hash",
    )


def make_transaction(day: date, amount: Decimal = Decimal("1")) -> Transaction:
    return Transaction(
        trade_date=day,
        action="buy",
        symbol="AAPL",
        kind=AssetKind.EQUITY,
        currency="USD",
        amount_local=amount,
        amount_base=amount,
    )
