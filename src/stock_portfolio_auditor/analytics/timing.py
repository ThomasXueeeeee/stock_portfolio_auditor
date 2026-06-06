# SPDX-License-Identifier: MIT
"""Capital timing skill calculations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimingSkill:
    """Summary of timing effects."""

    twr: float
    mwr: float
    mwr_dca: float

    @property
    def naive_timing_effect(self) -> float:
        """MWR minus TWR."""
        return self.mwr - self.twr

    @property
    def timing_vs_dca(self) -> float:
        """Actual MWR minus DCA counterfactual MWR."""
        return self.mwr - self.mwr_dca


def timing_skill(twr: float, mwr: float, mwr_dca: float) -> TimingSkill:
    """Build a timing-skill summary."""
    return TimingSkill(twr=twr, mwr=mwr, mwr_dca=mwr_dca)


def dca_counterfactual_mwr(twr: float, periods: int) -> float:
    """Simple DCA counterfactual approximation for v1.

    The full implementation will replay equal monthly contributions over the
    realized TWR path. This conservative approximation keeps the public API
    stable while report plumbing is built.
    """
    if periods <= 0:
        return twr
    return float((1.0 + twr) ** (1.0 / max(periods, 1)) - 1.0)
