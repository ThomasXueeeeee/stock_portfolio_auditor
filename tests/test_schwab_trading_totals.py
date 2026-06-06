# SPDX-License-Identifier: MIT
"""Regression tests for the Schwab Cash Transactions Summary trading-totals extractor.

The modern 8-column ``Transactions - Summary`` row order is::

    BeginningCash + Deposits + Withdrawals + Purchases +
    Sales/Redemptions + Dividends/Interest + Expenses = EndingCash

A previous version of this parser had ``Purchases`` at column [5] and
``Sales`` at column [3], which silently counted the entire
Dividends/Interest column as stock-purchase notional and inflated the
monthly turnover analytic by an order of magnitude. These tests pin
the right columns.
"""

from __future__ import annotations

from decimal import Decimal

from stock_portfolio_auditor.parsers.schwab import (
    _extract_stock_trading_totals,
    _extract_stock_trading_totals_legacy,
    _extract_stock_trading_totals_modern,
)


def test_modern_extracts_purchases_at_column_3_and_sales_at_column_4() -> None:
    """Real layout from a 2025 Schwab brokerage statement."""
    text = (
        "BeginningCash*asof03/01 + Deposits + Withdrawals + Purchases + "
        "Sales/Redemptions + Dividends/Interest + Expenses = "
        "EndingCash*asof03/31\n"
        "$131,816.03 $9,000.00 ($36,000.00) ($67,882.36) $13,745.86 "
        "$1,155.14 $0.00 $51,834.67\n"
    )
    purchases, sales = _extract_stock_trading_totals_modern(text)
    assert purchases == Decimal("67882.36")
    assert sales == Decimal("13745.86")


def test_modern_does_not_misread_dividends_as_purchases() -> None:
    """A month with zero trading and a non-trivial dividends/interest
    column must report zero purchases and zero sales -- the historical
    column-swap bug counted dividends as buys.
    """
    text = (
        "BeginningCash*asof06/01 + Deposits + Withdrawals + Purchases + "
        "Sales/Redemptions + Dividends/Interest + Expenses = "
        "EndingCash*asof06/30\n"
        "$3.26 $0.00 $0.00 $0.00 $0.00 $1,420.27 $0.00 $1,423.53\n"
    )
    purchases, sales = _extract_stock_trading_totals_modern(text)
    assert purchases == Decimal("0.00")
    assert sales == Decimal("0.00")


def test_legacy_reads_labelled_investments_rows() -> None:
    """Pre-2024 ``Cash Transactions Summary`` table layout."""
    text = (
        "Cash Transactions Summary\n"
        "Starting Cash $7.63 $7.71\n"
        "Deposits 2,500.00 5,000.00\n"
        "Investments Sold 0.00 0.00\n"
        "Investments Purchased (39,492.76) (45,000.00)\n"
        "Dividends and Interest 123.45 234.56\n"
        "Fees and Charges (1.00) (1.00)\n"
        "Ending Cash 100.00 100.00\n"
    )
    purchases, sales = _extract_stock_trading_totals_legacy(text)
    assert purchases == Decimal("39492.76")
    assert sales == Decimal("0")


def test_extract_stock_trading_totals_returns_zero_when_no_summary_found() -> None:
    """A statement without either summary layout returns (0, 0) so the
    turnover analytic can blindly add the values without a None check."""
    text = "Some unrelated statement content\nwith no Cash Transactions Summary section.\n"
    assert _extract_stock_trading_totals(text) == (Decimal("0"), Decimal("0"))


def test_extract_stock_trading_totals_prefers_modern_when_both_present() -> None:
    """If both layouts somehow appear in the same document (header
    boilerplate plus actual data), the modern extractor wins -- the
    modern format is more reliable because the columns are positional
    rather than label-driven and don't break when Schwab tweaks
    wording."""
    modern = (
        "BeginningCash + Deposits + Withdrawals + Purchases + "
        "Sales/Redemptions + Dividends/Interest + Expenses = EndingCash\n"
        "$1.00 $0.00 $0.00 $100.00 $50.00 $0.00 $0.00 $1.00\n"
        "Cash Transactions Summary\n"
        "Investments Sold 999.00 999.00\n"
        "Investments Purchased 999.00 999.00\n"
    )
    purchases, sales = _extract_stock_trading_totals(modern)
    assert purchases == Decimal("100.00")
    assert sales == Decimal("50.00")
