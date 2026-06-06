from __future__ import annotations

from stock_portfolio_auditor.analytics.attribution import PnlBreakdown
from stock_portfolio_auditor.analytics.pnl_dollar import waterfall_rows


def test_waterfall_rows_order_and_total() -> None:
    rows = waterfall_rows(PnlBreakdown(price=10, dividends=2, options=-1, fx=3, lending=1, tcost=4))
    assert [row.label for row in rows] == [
        "Equity price",
        "Dividends",
        "Options",
        "FX",
        "Lending",
        "Cost drag",
        "Total PnL",
    ]
    assert rows[-1].amount == 11
