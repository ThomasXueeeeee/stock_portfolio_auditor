from __future__ import annotations

from stock_portfolio_auditor.reporting import iter_build_stages


def test_iter_build_stages_reports_progress() -> None:
    stages = tuple(iter_build_stages(("one", "two")))
    assert len(stages) == 2
    assert stages[0].progress == 0.5
    assert stages[-1].completed is True
