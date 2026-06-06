from __future__ import annotations

from datetime import date, datetime

from stock_portfolio_auditor.reporting import KpiStrip, ReportSchema, YearlySummaryRow, render_html


def test_render_html_contains_core_sections() -> None:
    schema = ReportSchema(
        title="Portfolio Performance Audit",
        accounts=("acct",),
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        base_currency="USD",
        generated_at=datetime(2026, 1, 1),
        kpis=KpiStrip(
            twr=0.1,
            mwr_irr=0.11,
            mwr_dietz=0.105,
            total_pnl=1000,
            max_drawdown=-0.2,
            sharpe=1.0,
            sortino=1.2,
            calmar=0.5,
            tcost_bps=12,
        ),
        yearly_summary=(
            YearlySummaryRow(
                label="2024",
                twr=0.1,
                mwr_irr=0.11,
                price_bps=800,
                dividend_bps=100,
                option_bps=0,
                fx_bps=0,
                lending_bps=5,
                tcost_bps=-12,
                pnl=1000,
            ),
        ),
    )

    html = render_html(schema)

    assert "Executive Summary" in html
    assert "Yearly Summary" in html
    assert "Cost drag" in html
