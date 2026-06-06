from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.ingestion.reconstruct import deduplicate_overlapping_statements
from tests.factories import make_statement, make_transaction


def test_monthly_statement_wins_over_quarterly_overlap() -> None:
    jan_tx = make_transaction(date(2024, 1, 10), Decimal("1"))
    feb_tx = make_transaction(date(2024, 2, 10), Decimal("2"))
    mar_tx = make_transaction(date(2024, 3, 10), Decimal("3"))
    quarterly = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        frequency="Q",
        transactions=(jan_tx, feb_tx, mar_tx),
    )
    january = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
        transactions=(jan_tx,),
    )

    reconstructed = deduplicate_overlapping_statements([quarterly, january])

    txns = reconstructed[0].transactions
    assert len(txns) == 3
    assert [txn.trade_date.month for txn in txns] == [1, 2, 3]
    assert sum(1 for txn in txns if txn.trade_date.month == 1) == 1
