# SPDX-License-Identifier: MIT
"""Risk metric calculations."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class Drawdown:
    """Maximum drawdown summary."""

    max_drawdown: float
    peak_date: pd.Timestamp | None
    trough_date: pd.Timestamp | None


def cumulative_index(returns: pd.Series) -> pd.Series:
    """Convert periodic returns into a wealth index starting at 1."""
    if returns.empty:
        return pd.Series(dtype="float64")
    return (1.0 + returns.astype("float64")).cumprod()


def max_drawdown(wealth_index: pd.Series) -> Drawdown:
    """Compute max drawdown from a wealth index."""
    if wealth_index.empty:
        return Drawdown(0.0, None, None)
    running_max = wealth_index.cummax()
    drawdowns = wealth_index / running_max - 1.0
    trough_pos = int(np.argmin(drawdowns.to_numpy()))
    peak_pos = int(np.argmax(wealth_index.iloc[: trough_pos + 1].to_numpy()))
    trough = wealth_index.index[trough_pos]
    peak = wealth_index.index[peak_pos]
    return Drawdown(float(drawdowns.iloc[trough_pos]), pd.Timestamp(peak), pd.Timestamp(trough))


def sharpe_ratio(
    returns: pd.Series, *, risk_free_per_period: float = 0.0, periods_per_year: int = 12
) -> float:
    """Annualized Sharpe ratio."""
    excess = returns.astype("float64") - risk_free_per_period
    std = excess.std(ddof=1)
    if np.isclose(std, 0.0) or np.isnan(std):
        return 0.0
    return float(excess.mean() / std * np.sqrt(periods_per_year))


def sortino_ratio(
    returns: pd.Series, *, mar_per_period: float = 0.0, periods_per_year: int = 12
) -> float:
    """Annualized Sortino ratio."""
    excess = returns.astype("float64") - mar_per_period
    downside = excess[excess < 0]
    downside_std = downside.std(ddof=1)
    if np.isclose(downside_std, 0.0) or np.isnan(downside_std):
        return 0.0
    return float(excess.mean() / downside_std * np.sqrt(periods_per_year))


def calmar_ratio(annual_return: float, max_drawdown_value: float) -> float:
    """Calmar ratio using absolute max drawdown in denominator."""
    if np.isclose(max_drawdown_value, 0.0):
        return 0.0
    return float(annual_return / abs(max_drawdown_value))
