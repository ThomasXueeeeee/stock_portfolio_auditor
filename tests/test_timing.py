from __future__ import annotations

import pytest

from stock_portfolio_auditor.analytics.timing import dca_counterfactual_mwr, timing_skill


def test_timing_skill_properties() -> None:
    result = timing_skill(twr=0.10, mwr=0.15, mwr_dca=0.12)
    assert result.naive_timing_effect == pytest.approx(0.05)
    assert result.timing_vs_dca == pytest.approx(0.03)


def test_dca_counterfactual_handles_empty_periods() -> None:
    assert dca_counterfactual_mwr(0.10, 0) == 0.10
