# SPDX-License-Identifier: MIT
"""Unit tests for the Schwab realized-PnL extractor.

The Transaction Details page on Schwab brokerage PDFs carries a per-trade
Realized Gain/(Loss) column tagged with ``,(ST)`` or ``,(LT)``. Two-tax-lot
dispositions split the realized column across the dated row (ST portion) and
an immediately following continuation line (LT portion) that wraps the
description onto the next physical line and therefore has no leading date.

These tests pin down both the simple single-line case and the continuation
case so the long-term half of every two-lot Schwab disposition is captured.
"""

from __future__ import annotations

from decimal import Decimal

from stock_portfolio_auditor.parsers.schwab import _extract_realized_pnl


def test_realized_single_line_short_term_gain() -> None:
    text = (
        "03/11 Sale HAFN HAFNIALTD F (750.0000) 4.1606 0.21 3,120.24 (1,158.51),(ST)\n"
        "IndustryFee$0.21\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {"HAFN": Decimal("-1158.51")}


def test_realized_continuation_line_long_term_gain() -> None:
    """Schwab two-lot dispositions wrap the LT portion onto a dateless line."""
    text = (
        "02/24 Sale PDD PDDHOLDINGSINC F (2,300.0000) 125.0000 8.37 287,491.63 32,784.25,(ST)\n"
        "UNSPONSOREDADR 41,943.06,(LT)\n"
        "IndustryFee$8.37\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl["PDD"] == Decimal("32784.25") + Decimal("41943.06")


def test_realized_continuation_does_not_leak_across_unrelated_dated_rows() -> None:
    """A non-Sale/Purchase dated row in between must reset the continuation context."""
    text = (
        "02/24 Sale PDD PDDHOLDINGSINC F (2,300.0000) 125.0000 8.37 287,491.63 32,784.25,(ST)\n"
        "02/25 Dividend Qual.Dividend NMM NAVIOSMARITIMEPARTNLP 11.00\n"
        # The line below would syntactically look like a continuation if we
        # didn't reset on the dividend row above; it must NOT be attributed
        # to PDD.
        "STALE WRAPPED DESCRIPTION 99,999.99,(LT)\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {"PDD": Decimal("32784.25")}


def test_realized_multiple_trades_per_statement() -> None:
    text = (
        "03/11 Sale HAFN HAFNIALTD F (750.0000) 4.1606 0.21 3,120.24 (1,158.51),(ST)\n"
        "03/11 Sale NMM NAVIOSMARITIMEPARTNLP (220.0000) 39.9964 0.28 8,798.93 (1,281.47),(ST)\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {"HAFN": Decimal("-1158.51"), "NMM": Decimal("-1281.47")}


def test_realized_open_short_premium_row_without_tenor_is_skipped() -> None:
    """``Sale ShortSale`` lines have no realized column and must be ignored."""
    text = (
        "01/03 Sale ShortSale PDD01/17/2025 CALLPDDHOLDINGSINC $110 (23.0000) 0.4700 15.29 1,065.71\n"
        "110.00C EXP01/17/25\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {}


def test_realized_purchase_close_short_routed_to_option_pseudo_symbol() -> None:
    """Closing a short option uses the Purchase action and carries a tenor tag.

    The realized amount must be attributed to a per-underlying option
    pseudo-symbol (prefixed with ``_OPT_`` so the contribution panel filters
    it out of the Top Contributors / Top Detractors stock rankings), not to
    the underlying stock ticker — otherwise BN's stock attribution would be
    distorted by option close P&L.
    """
    text = (
        "04/17 Purchase BN04/17/2025 PUTBROOKFIELDCORP $45 10.0000 0.3000 6.66 (306.66) 330.02,(ST)\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {"_OPT_BN": Decimal("330.02")}
    assert "BN" not in pnl


def test_realized_option_continuation_stays_on_option_bucket() -> None:
    """An option-close continuation row must extend the option key, not the stock."""
    text = (
        "04/17 Purchase BN04/17/2025 PUTBROOKFIELDCORP $45 10.0000 0.3000 6.66 (306.66) 200.00,(ST)\n"
        "WRAPPED DESCRIPTION 130.00,(LT)\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {"_OPT_BN": Decimal("330.00")}


def test_legacy_gain_or_loss_section_inline_symbol_captured() -> None:
    """Pre-2024 Schwab statements record realized PnL in a separate section
    ``Gain or (Loss) on Investments Sold`` rather than on the Transaction
    Details page. Symbol is embedded after the last ``:`` in the description.
    """
    text = """
Gain or (Loss) on Investments Sold
Acquired/ Sold/
Investments Quantity/Par Opened Closed Total Proceeds Cost Basis Gain or (Loss)
INVSC QQQ TRUST SRS 1 ETF: QQQ 0.0118 11/01/21 10/14/22 3.12 4.56 (1.44)
INVSC QQQ TRUST SRS 1 ETF: QQQ 4.0000 05/03/21 10/14/22 1,057.37 1,347.83 (290.46)
Total Gain or (Loss) on Investments Sold 1,060.49 1,352.39 (291.90)
"""
    pnl = _extract_realized_pnl(text)
    assert pnl == {"QQQ": Decimal("-1.44") + Decimal("-290.46")}


def test_legacy_gain_or_loss_section_continuation_symbol_captured() -> None:
    """``ADR REPS: JD``-style continuation lines must contribute the prior
    row's realized gain to the right symbol.
    """
    text = """
Gain or (Loss) on Investments Sold
Acquired/ Sold/
Investments Quantity/Par Opened Closed Total Proceeds Cost Basis Gain or (Loss)
JD COM INC FSPONSORED ADR 1 10.0000 06/03/22 10/18/22 446.79 561.37 (114.58)
ADR REPS: JD
JD COM INC FSPONSORED ADR 1 20.0000 03/21/22 10/18/22 893.58 1,219.18 (325.60)
ADR REPS: JD
Total Gain or (Loss) on Investments Sold 1,340.37 1,780.55 (440.18)
"""
    pnl = _extract_realized_pnl(text)
    assert pnl == {"JD": Decimal("-114.58") + Decimal("-325.60")}


def test_legacy_and_modern_realized_rows_accumulate_into_same_per_symbol_bucket() -> None:
    """A statement with both layouts (transition months) must sum both into
    the per-symbol realized total without double-counting.
    """
    text = """
Gain or (Loss) on Investments Sold
Investments Quantity/Par Opened Closed Total Proceeds Cost Basis Gain or (Loss)
INVSC QQQ TRUST SRS 1 ETF: QQQ 1.0000 05/03/21 10/14/22 200.00 100.00 100.00
Total Gain or (Loss) on Investments Sold 200.00 100.00 100.00

10/15 Sale QQQ INVESCOQQQTRUST F (1.0000) 250.0000 0.10 249.90 50.00,(ST)
"""
    pnl = _extract_realized_pnl(text)
    assert pnl == {"QQQ": Decimal("150.00")}


def test_realized_2026_format_without_st_lt_tag_is_captured() -> None:
    """Schwab dropped the ``,(ST|LT)`` tag from Sale rows in late 2025 / early
    2026 statements. The bare realized money value (with optional trailing
    comma) is still printed in the last column.
    """
    text = (
        "02/27 Sale FTAI FTAIAVIATIONLTD F (151.0000) 294.3550 0.03 44,447.58 20,424.96,\n"
        "IndustryFee$0.03\n"
        "Sale FTAI FTAIAVIATIONLTD F (200.0000) 319.4519 0.04 63,890.34 33,105.92\n"
        "IndustryFee$0.04\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {"FTAI": Decimal("20424.96") + Decimal("33105.92")}


def test_realized_2026_format_sale_with_trailing_paren_negative() -> None:
    """Negative realized in the new format is parenthesized rather than tagged."""
    text = "02/06 Sale BIL STSTERTSPDRBLMBG13MNTBL (670.0000) 91.4100 0.13 61,244.57 (106.66),\n"
    pnl = _extract_realized_pnl(text)
    assert pnl == {"BIL": Decimal("-106.66")}


def test_realized_2026_format_does_not_misread_short_sale_open() -> None:
    """``Sale ShortSale`` rows (option-open premium received) have only four
    trailing money fields and must NOT be mis-attributed as having realized.
    """
    text = (
        "01/03 Sale ShortSale PDD01/17/2025 CALLPDDHOLDINGSINC $110 "
        "(23.0000) 0.4700 15.29 1,065.71\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {}


def test_realized_stock_and_option_on_same_underlying_kept_separate() -> None:
    """A stock close and an option close on the same underlying go to different buckets."""
    text = (
        "04/17 Purchase BN04/17/2025 PUTBROOKFIELDCORP $45 10.0000 0.3000 6.66 (306.66) 100.00,(ST)\n"
        "04/18 Sale BN BROOKFIELDCORP (500.0000) 45.0000 0.40 22,499.60 500.00,(LT)\n"
    )
    pnl = _extract_realized_pnl(text)
    assert pnl == {"_OPT_BN": Decimal("100.00"), "BN": Decimal("500.00")}
