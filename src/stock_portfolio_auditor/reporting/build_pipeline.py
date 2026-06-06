# SPDX-License-Identifier: MIT
"""Staged report build pipeline with progress events."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from time import perf_counter

from loguru import logger


@dataclass(frozen=True, slots=True)
class BuildStage:
    """Progress event emitted by the report build pipeline."""

    index: int
    total: int
    name: str
    elapsed_seconds: float
    completed: bool

    @property
    def progress(self) -> float:
        """Progress from 0 to 1."""
        return self.index / self.total if self.total else 1.0


DEFAULT_STAGES = (
    "Loading parsed statements from cache",
    "Resolving tickers and currencies",
    "Fetching prices",
    "Fetching FX series",
    "Reconstructing NAV series",
    "Computing return decomposition",
    "Computing risk metrics",
    "Computing capital-timing skill",
    "Computing benchmark stats",
    "Rendering figures",
    "Assembling HTML",
)


def iter_build_stages(stages: tuple[str, ...] = DEFAULT_STAGES) -> Iterator[BuildStage]:
    """Yield deterministic progress events.

    The current implementation emits stage boundaries only. Later iterations run
    actual build functions between these events.
    """
    total = len(stages)
    started = perf_counter()
    for index, name in enumerate(stages, start=1):
        event = BuildStage(
            index=index,
            total=total,
            name=name,
            elapsed_seconds=perf_counter() - started,
            completed=index == total,
        )
        logger.info("Report build stage", stage=index, total=total, name=name)
        yield event
