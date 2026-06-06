from __future__ import annotations

from datetime import date, datetime

from stock_portfolio_auditor.reporting import KpiStrip, ReportSchema, executive_summary


def test_report_schema_has_user_facing_cost_drag_label() -> None:
    schema = ReportSchema(
        title="Audit",
        accounts=("acct",),
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        base_currency="USD",
        generated_at=datetime(2026, 1, 1),
        kpis=KpiStrip(
            twr=0.1,
            mwr_irr=0.12,
            mwr_dietz=0.11,
            total_pnl=1000,
            max_drawdown=-0.2,
            sharpe=1.0,
            sortino=1.1,
            calmar=0.5,
            tcost_bps=15,
        ),
    )
    assert schema.field_labels["tcost_bps"] == "Cost drag (bps)"
    summary = executive_summary(schema)
    assert "TWR" in summary
    assert "MWR" in summary
