# SPDX-License-Identifier: MIT
"""Unit tests for the Schwab option-transaction emitter.

Schwab option Sale / Purchase rows carry the option expiration glued onto
the underlying ticker (``BN04/17/2025``). These rows previously fell into
the catch-all "skip" branch of ``_extract_transactions`` so their premium
cash flow ended up absorbed into the Price residual instead of the Options
bucket. The emitter now produces ``Transaction(kind=OPTION)`` rows keyed by
the ``_OPT_<UNDERLYING>`` pseudo-symbol.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.domain.models import AssetKind
from stock_portfolio_auditor.parsers.schwab import _extract_transactions


def test_short_sale_open_emits_option_transaction_with_positive_premium() -> None:
    text = (
        "01/03 Sale ShortSale PDD01/17/2025 CALLPDDHOLDINGSINC $110 (23.0000) "
        "0.4700 15.29 1,065.71\n"
        "110.00C EXP01/17/25\n"
    )
    txns = _extract_transactions(text, date(2025, 1, 1), date(2025, 1, 31))
    assert len(txns) == 1
    txn = txns[0]
    assert txn.kind is AssetKind.OPTION
    assert txn.symbol == "_OPT_PDD"
    assert txn.amount_base == Decimal("1065.71")
    assert txn.action == "sell"


def test_implicit_short_sale_open_without_shortsale_keyword() -> None:
    """Schwab also prints option opens as plain ``Sale TICKERDATE ...``."""
    text = (
        "03/10 Sale BN04/17/2025 PUTBROOKFIELDCORP $45 (10.0000) 0.6500 6.66 643.34\n"
        "45.00P EXP04/17/25\n"
    )
    txns = _extract_transactions(text, date(2025, 3, 1), date(2025, 3, 31))
    assert len(txns) == 1
    assert txns[0].symbol == "_OPT_BN"
    assert txns[0].amount_base == Decimal("643.34")


def test_option_close_purchase_with_realized_tag_emits_negative_premium() -> None:
    """Buy-to-close on a short option pays out cash; the realized G/L tag at
    the end must not stop the cash-flow row from being emitted as well."""
    text = (
        "04/17 Purchase BN04/17/2025 PUTBROOKFIELDCORP $45 10.0000 0.3000 "
        "6.66 (306.66) 330.02,(ST)\n"
    )
    txns = _extract_transactions(text, date(2025, 4, 1), date(2025, 4, 30))
    assert len(txns) == 1
    txn = txns[0]
    assert txn.symbol == "_OPT_BN"
    assert txn.kind is AssetKind.OPTION
    assert txn.amount_base == Decimal("-306.66")
    assert txn.action == "buy"


def test_option_close_purchase_with_parenthesised_realized_loss_is_captured() -> None:
    """A losing BTC prints the realized G/L in parens (``(6,881.63),(ST)``).
    The previous regex required ``[\\d,.]+`` for the trailing realized value
    which only matched bare digits and silently dropped the entire row when
    the realized was a loss in parens. Make sure parenthesised realized
    tags now accept the row.
    """
    text = (
        "03/13 Purchase DAL03/28/2025 PUTDELTAAIRLINESINC$60 5.0000 14.9500 "
        "3.30 (7,478.30) (6,881.63),(ST)\n"
    )
    txns = _extract_transactions(text, date(2025, 3, 1), date(2025, 3, 31))
    assert len(txns) == 1
    txn = txns[0]
    assert txn.symbol == "_OPT_DAL"
    assert txn.kind is AssetKind.OPTION
    assert txn.amount_base == Decimal("-7478.30")
    assert txn.action == "buy"


def test_stock_sale_is_not_emitted_as_option_transaction() -> None:
    """Plain stock sales (no expiry glued to ticker) must not be emitted here.

    Realized P&L for stock sales lives on the per-statement
    ``per_symbol_pnl_base`` map (parsed by ``_extract_realized_pnl``);
    emitting them as Transactions would double-count them into per-symbol
    contribution attribution.
    """
    text = (
        "03/11 Sale HAFN HAFNIALTD F (750.0000) 4.1606 0.21 3,120.24 (1,158.51),(ST)\n"
        "04/03 Purchase TDW TIDEWATERINCNEW 250.0000 43.5300 (10,882.50)\n"
    )
    txns = _extract_transactions(text, date(2025, 3, 1), date(2025, 4, 30))
    # No option rows present -> no option transactions emitted. Dividend /
    # interest / expense / deposit / withdrawal handlers don't fire on these
    # lines either, so the output must be empty.
    assert txns == ()


def test_sibling_option_row_without_leading_date_inherits_dated_predecessor() -> None:
    """Same-day option trades print only the first row's date prefix; the
    subsequent ``Sale TICKERDATE ...`` row has no date and must inherit
    the trade date from the dated row above.
    """
    text = (
        "02/12 Sale ShortSale PDD02/21/2025 CALLPDDHOLDINGSINC $125 "
        "(23.0000) 0.6800 15.30 1,548.70\n"
        "Sale DAL03/28/2025 PUTDELTAAIRLINESINC$60 (5.0000) 1.2000 3.33 596.67\n"
    )
    txns = _extract_transactions(text, date(2025, 2, 1), date(2025, 2, 28))
    by_symbol = {t.symbol: t for t in txns}
    assert by_symbol["_OPT_DAL"].amount_base == Decimal("596.67")
    # Both rows ought to be stamped at 02/12.
    assert by_symbol["_OPT_DAL"].trade_date == date(2025, 2, 12)
    assert by_symbol["_OPT_PDD"].trade_date == date(2025, 2, 12)


def test_multiple_option_trades_on_same_underlying_aggregate_via_symbol() -> None:
    """Two option transactions on the same underlying share the option pseudo-symbol
    so they aggregate naturally in downstream attribution.
    """
    text = (
        "02/12 Sale ShortSale PDD02/21/2025 CALLPDDHOLDINGSINC $125 (23.0000) "
        "0.6800 15.30 1,548.70\n"
        "02/24 Sale PDD03/14/2025 PUTPDDHOLDINGSINC $115 (5.0000) 1.3800 3.33 686.67\n"
    )
    txns = _extract_transactions(text, date(2025, 2, 1), date(2025, 2, 28))
    assert {t.symbol for t in txns} == {"_OPT_PDD"}
    assert sum(float(t.amount_base) for t in txns) == 1548.70 + 686.67
