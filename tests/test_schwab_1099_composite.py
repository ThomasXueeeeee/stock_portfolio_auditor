# SPDX-License-Identifier: MIT
"""Unit tests for the Schwab Form 1099 Composite / Year-End Summary parser.

Pre-2024 schwab_individual brokerage statements collapse stock realized
PnL into a single ``Investments Sold $X`` aggregate row with no per-symbol
detail. The annual Form 1099 Composite is the only Schwab document that
publishes per-lot realized data for the brokerage account, so it is the
authoritative source for those audit-window years.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.domain.models import AssetKind
from stock_portfolio_auditor.parsers.schwab import (
    _extract_1099_composite_dispositions,
    _extract_1099_composite_realized,
    _looks_like_1099_composite,
)


def test_looks_like_1099_composite_detects_year_end_summary_pdf() -> None:
    assert _looks_like_1099_composite("FORM 1099 COMPOSITE\nTAX YEAR 2023\n...") is True
    assert _looks_like_1099_composite("Brokerage Statement\nFebruary 1-28, 2025") is False


def test_extract_1099_composite_realized_stock_dispositions() -> None:
    """A Security Subtotal line carries the per-symbol realized that we want."""
    text = """
SHORT-TERM TRANSACTIONS FOR WHICH BASIS IS REPORTED TO THE IRS
1a-Description of property 6-Reported to IRS:
CUSIP Number / Symbol disposed indicated) other basis Loss Disallowed Gain or (Loss) tax withheld
235 ALPHABET INC. CLASS S VARIOUS $ 20,627.83 $ 26,841.83 -- $ (6,214.00)$ 0.00
02079K305 / GOOGL 01/10/23 --
Security Subtotal $ 20,627.83 $ 26,841.83 -- $ (6,214.00) $ 0.00
--
98 AMAZON.COM INC S VARIOUS $ 8,785.99 $ 11,988.45 -- $ (3,202.46)$ 0.00
023135106 / AMZN 01/10/23 --
98 AMAZON.COM INC S VARIOUS $ 8,791.87 $ 11,462.31 -- $ (2,670.44)$ 0.00
023135106 / AMZN 01/10/23 --
Security Subtotal $ 17,577.86 $ 23,450.76 -- $ (5,872.90) $ 0.00
"""
    per_symbol = _extract_1099_composite_realized(text)
    assert per_symbol["GOOGL"] == Decimal("-6214.00")
    assert per_symbol["AMZN"] == Decimal("-5872.90")


def test_extract_1099_composite_realized_handles_redacted_cusip() -> None:
    """The PII redactor masks 9-digit CUSIPs as ``[REDACTED]``; the regex
    must still attach the symbol to the subtotal that follows.
    """
    text = """
98 AMAZON.COM INC S VARIOUS $ 8,785.99 $ 11,988.45 -- $ (3,202.46)$ 0.00
[REDACTED] / AMZN 01/10/23 --
Security Subtotal $ 8,785.99 $ 11,988.45 -- $ (3,202.46) $ 0.00
"""
    per_symbol = _extract_1099_composite_realized(text)
    assert per_symbol == {"AMZN": Decimal("-3202.46")}


def test_extract_1099_composite_realized_option_routed_to_pseudo_symbol() -> None:
    """Option dispositions are keyed by ``_OPT_<UNDERLYING>`` to match the
    monthly-statement parser convention and stay out of the stock rankings.
    """
    text = """
23S CALL PDD HOLDINGS INC $110 E X 11/27/24 $ 766.72 $ 0.00 -- $ 766.72$ 0.00
PDD 12/06/2024 110.00 C 12/06/24 --
Security Subtotal $ 766.72 $ 0.00 -- $ 766.72 $ 0.00
"""
    per_symbol = _extract_1099_composite_realized(text)
    assert per_symbol == {"_OPT_PDD": Decimal("766.72")}


def test_extract_1099_composite_realized_brk_b_normalised_to_dot_form() -> None:
    """Schwab writes Class B shares as ``BRK/B``; the rest of the codebase
    uses ``BRK.B`` so we normalise on parse.
    """
    text = """
50 BERKSHIRE HATHAWAY S VARIOUS $ 17,500.00 $ 15,000.00 -- $ 2,500.00$ 0.00
084670702 / BRK/B 03/15/23 --
Security Subtotal $ 17,500.00 $ 15,000.00 -- $ 2,500.00 $ 0.00
"""
    per_symbol = _extract_1099_composite_realized(text)
    assert per_symbol == {"BRK.B": Decimal("2500.00")}


def test_extract_1099_composite_dispositions_emits_dated_transactions() -> None:
    """Each disposition row becomes a Transaction with ``trade_date`` and
    ``realized_pnl_base`` populated so the audit-window filter can keep
    only the in-window dispositions when a 1099's tax year overflows the
    requested audit window.
    """
    text = """
235 ALPHABET INC. CLASS S VARIOUS $ 20,627.83 $ 26,841.83 -- $ (6,214.00)$ 0.00
02079K305 / GOOGL 01/10/23 --
Security Subtotal $ 20,627.83 $ 26,841.83 -- $ (6,214.00) $ 0.00
50 BERKSHIRE HATHAWAY S VARIOUS $ 17,500.00 $ 15,000.00 -- $ 2,500.00$ 0.00
084670702 / BRK/B 03/15/23 --
Security Subtotal $ 17,500.00 $ 15,000.00 -- $ 2,500.00 $ 0.00
"""
    dispositions = _extract_1099_composite_dispositions(text, default_year=2023)
    assert len(dispositions) == 2
    by_symbol = {t.symbol: t for t in dispositions}
    assert by_symbol["GOOGL"].trade_date == date(2023, 1, 10)
    assert by_symbol["GOOGL"].realized_pnl_base == Decimal("-6214.00")
    assert by_symbol["GOOGL"].action == "sell"
    assert by_symbol["GOOGL"].kind == AssetKind.EQUITY
    assert by_symbol["BRK.B"].trade_date == date(2023, 3, 15)
    assert by_symbol["BRK.B"].realized_pnl_base == Decimal("2500.00")


def test_extract_1099_composite_dispositions_emits_option_pseudo_symbol() -> None:
    """Option dispositions route to ``_OPT_<UNDERLYING>`` matching the
    monthly-statement convention."""
    text = """
23S CALL PDD HOLDINGS INC $110 E X 11/27/24 $ 766.72 $ 0.00 -- $ 766.72$ 0.00
PDD 12/06/2024 110.00 C 12/06/24 --
Security Subtotal $ 766.72 $ 0.00 -- $ 766.72 $ 0.00
"""
    dispositions = _extract_1099_composite_dispositions(text, default_year=2024)
    assert len(dispositions) == 1
    txn = dispositions[0]
    assert txn.symbol == "_OPT_PDD"
    assert txn.kind == AssetKind.OPTION
    assert txn.trade_date == date(2024, 12, 6)
    assert txn.realized_pnl_base == Decimal("766.72")


def test_extract_1099_composite_dispositions_handles_multi_lot_per_symbol() -> None:
    """Two lots of the same symbol sold on different dates emit two
    Transaction rows so the audit-window filter can attribute each one
    to its own day."""
    text = """
98 AMAZON.COM INC S VARIOUS $ 8,785.99 $ 11,988.45 -- $ (3,202.46)$ 0.00
023135106 / AMZN 01/10/23 --
98 AMAZON.COM INC S VARIOUS $ 8,791.87 $ 11,462.31 -- $ (2,670.44)$ 0.00
023135106 / AMZN 11/15/23 --
Security Subtotal $ 17,577.86 $ 23,450.76 -- $ (5,872.90) $ 0.00
"""
    dispositions = _extract_1099_composite_dispositions(text, default_year=2023)
    assert len(dispositions) == 2
    dates = sorted(t.trade_date for t in dispositions)
    assert dates == [date(2023, 1, 10), date(2023, 11, 15)]
    by_date = {t.trade_date: t.realized_pnl_base for t in dispositions}
    assert by_date[date(2023, 1, 10)] == Decimal("-3202.46")
    assert by_date[date(2023, 11, 15)] == Decimal("-2670.44")
