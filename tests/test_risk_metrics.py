from __future__ import annotations

import pandas as pd
import pytest

from stock_portfolio_auditor.analytics.risk import (
    calmar_ratio,
    cumulative_index,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
)


def test_cumulative_index_and_drawdown() -> None:
    returns = pd.Series(
        [0.10, -0.20, 0.05], index=pd.date_range("2024-01-31", periods=3, freq="ME")
    )
    wealth = cumulative_index(returns)
    drawdown = max_drawdown(wealth)
    assert wealth.iloc[0] == pytest.approx(1.10)
    assert drawdown.max_drawdown == pytest.approx(-0.20)


def test_sharpe_and_sortino_return_floats() -> None:
    returns = pd.Series([0.02, -0.01, 0.03, -0.02, 0.01])
    assert isinstance(sharpe_ratio(returns), float)
    assert isinstance(sortino_ratio(returns), float)


def test_calmar_ratio() -> None:
    assert calmar_ratio(0.12, -0.20) == pytest.approx(0.60)
