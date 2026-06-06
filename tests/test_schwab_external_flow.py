# SPDX-License-Identifier: MIT
"""Unit tests for the Schwab external cash flow detector.

Schwab uses many different action names for cash that comes in or out of an
account: ``Deposit MoneyLink``, ``Withdrawal MoneyLink``, ``Journaled Funds
IRA ROLLOVER CONTRIB``, ``Conversion``, ``Recharacterization``, bank-sweep
auto-transfers. Rather than enumerate all variants per row, the parser
emits one synthetic external-flow transaction per statement based on the
broker's own Deposits / Withdrawals totals — modern Transactions - Summary
single-row layout, or legacy Cash Transactions Summary multi-row table.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.parsers.schwab import _extract_transactions


def test_modern_transactions_summary_net_deposit_emitted_as_external_flow() -> None:
    text = """
Transactions - Summary
BeginningCash*asof03/01 + Deposits + Withdrawals + Purchases + Sales/Redemptions + Dividends/Interest + Expenses = EndingCash*asof03/31
$131,816.03 $9,000.00 ($36,000.00) ($67,882.36) $13,745.86 $1,155.14 $0.00 $51,834.67
"""
    txns = _extract_transactions(text, date(2025, 3, 1), date(2025, 3, 31))
    flows = [t for t in txns if t.is_external_cash_flow]
    assert len(flows) == 1
    # Net = deposits + withdrawals = 9,000 + (-36,000) = -27,000.
    assert flows[0].amount_base == Decimal("-27000.0000")
    assert flows[0].action == "wd"
    assert flows[0].trade_date == date(2025, 3, 31)


def test_modern_transactions_summary_net_positive_deposit() -> None:
    text = """
Transactions - Summary
BeginningCash*asof01/01 + Deposits + Withdrawals + Purchases + Sales/Redemptions + Dividends/Interest + Expenses = EndingCash*asof01/31
$0.00 $50,000.00 $0.00 $0.00 $0.00 $0.00 $0.00 $50,000.00
"""
    txns = _extract_transactions(text, date(2025, 1, 1), date(2025, 1, 31))
    flows = [t for t in txns if t.is_external_cash_flow]
    assert len(flows) == 1
    assert flows[0].amount_base == Decimal("50000.0000")
    assert flows[0].action == "contrib"


def test_legacy_cash_transactions_summary_captures_ira_rollover() -> None:
    """Pre-2024 IRA / Roth IRA statements record large rollover-style inflows
    as ``Journaled Funds IRA ROLLOVER CONTRIB`` rows, which the per-row
    Deposit/Withdrawal parser previously couldn't see. Reading the
    summary's Deposits total catches the full amount.
    """
    text = """
Cash Transactions Summary
Description This Period Year to Date
Starting Cash* $7.63 $7.71
Deposits 52,303.29 52,303.29
Withdrawals 0.00 0.00
Investments Sold 270.46 270.46
Dividends and Interest 0.00 0.10
Withdrawals and other Debits 0.00 0.00
Investments Purchased (52,339.75) (52,354.59)
Fees and Charges 0.00 (0.10)
Total Cash Transaction Detail 234.00 233.92
Ending Cash* $241.63 $241.63
"""
    txns = _extract_transactions(text, date(2024, 3, 1), date(2024, 3, 31))
    flows = [t for t in txns if t.is_external_cash_flow]
    assert len(flows) == 1
    assert flows[0].amount_base == Decimal("52303.2900")
    assert flows[0].action == "contrib"


def test_legacy_cash_transactions_with_long_row_labels() -> None:
    """Non-retirement brokerage statements label the inflow/outflow rows as
    ``Deposits and other Cash Credits`` / ``Withdrawals and other Debits``
    rather than the plain ``Deposits`` / ``Withdrawals`` labels the
    retirement-account statements use. The regex must accept both.
    """
    text = """
Cash Transactions Summary This Period Year to Date
Starting Cash* $ 1,711.32 $ 84.59
Deposits and other Cash Credits 20,000.00 207,000.00
Investments Sold 25,000.00 247,888.49
Dividends and Interest 1,646.06 3,913.58
Withdrawals and other Debits (20,000.00) (26,500.00)
Investments Purchased (25,220.52) (429,126.08)
Fees and Charges (33.11) (156.83)
Total Cash Transaction Detail 1,392.43 3,019.16
Ending Cash* $ 3,103.75 $ 3,103.75
"""
    txns = _extract_transactions(text, date(2023, 6, 1), date(2023, 6, 30))
    flows = [t for t in txns if t.is_external_cash_flow]
    # Net = +20,000 - 20,000 = 0 -> no flow row emitted.
    assert flows == []


def test_no_external_flow_emitted_when_period_has_zero_net_flow() -> None:
    text = """
Transactions - Summary
BeginningCash*asof04/01 + Deposits + Withdrawals + Purchases + Sales/Redemptions + Dividends/Interest + Expenses = EndingCash*asof04/30
$71.19 $0.00 $0.00 ($63,492.03) $63,287.88 $152.52 $0.00 $19.56
"""
    txns = _extract_transactions(text, date(2025, 4, 1), date(2025, 4, 30))
    flows = [t for t in txns if t.is_external_cash_flow]
    assert flows == []
