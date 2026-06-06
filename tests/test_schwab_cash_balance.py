# SPDX-License-Identifier: MIT
"""Unit tests for the Schwab Transactions - Summary cash balance extractor."""

from __future__ import annotations

from decimal import Decimal

from stock_portfolio_auditor.parsers.schwab import _extract_cash_balance


def test_cash_balance_from_standard_summary_line() -> None:
    text = """
Transactions - Summary
BeginningCash*asof03/01 + Deposits + Withdrawals + Purchases + Sales/Redemptions + Dividends/Interest + Expenses = EndingCash*asof03/31
$131,816.03 $9,000.00 ($36,000.00) ($67,882.36) $13,745.86 $1,155.14 $0.00 $51,834.67
OtherActivity $0.00 Otheractivityincludestransactionswhichdon'taffectthecashbalance
"""
    balance = _extract_cash_balance(text)
    assert balance.currency == "USD"
    assert balance.starting == Decimal("131816.0300")
    assert balance.ending == Decimal("51834.6700")


def test_cash_balance_handles_negative_starting_value() -> None:
    """Parenthesised negatives in the cash summary row must be respected."""
    text = """
Transactions - Summary
BeginningCash*asof01/01 + Deposits + Withdrawals + Purchases + Sales/Redemptions + Dividends/Interest + Expenses = EndingCash*asof01/31
($500.00) $1,000.00 $0.00 $0.00 $0.00 $0.00 $0.00 $500.00
"""
    balance = _extract_cash_balance(text)
    assert balance.starting == Decimal("-500.0000")
    assert balance.ending == Decimal("500.0000")


def test_cash_balance_returns_zero_when_section_absent() -> None:
    """Older or truncated statements without the section degrade to zero/zero."""
    balance = _extract_cash_balance("No transactions section here at all.")
    assert balance.starting == 0
    assert balance.ending == 0


def test_cash_balance_legacy_cash_transactions_summary() -> None:
    """Pre-2024 Schwab statements use a "Cash Transactions Summary" table with
    separate rows for Starting Cash / Deposits / Withdrawals / Ending Cash
    rather than the single-row Transactions - Summary layout.
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
    balance = _extract_cash_balance(text)
    assert balance.starting == Decimal("7.6300")
    assert balance.ending == Decimal("241.6300")
