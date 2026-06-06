from __future__ import annotations

from stock_portfolio_auditor.gui_streamlit import _placeholder_schema


def test_placeholder_schema_is_valid() -> None:
    schema = _placeholder_schema()
    assert schema.title == "Portfolio Performance Audit"
    assert schema.kpis.tcost_bps == 0
