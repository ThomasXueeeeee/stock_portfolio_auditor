# SPDX-License-Identifier: MIT
"""CSV loading helpers."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from stock_portfolio_auditor.domain.errors import StatementCorruptError


def read_csv_rows(path: str | Path) -> list[list[str]]:
    """Read a CSV file as raw rows without assuming a rectangular shape."""
    csv_path = Path(path)
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))
    except UnicodeDecodeError:
        with csv_path.open("r", encoding="latin-1", newline="") as handle:
            return list(csv.reader(handle))
    except Exception as exc:  # pragma: no cover - filesystem/backend failures
        raise StatementCorruptError(
            "Could not read CSV statement",
            {"path": str(csv_path), "error": str(exc)},
        ) from exc


def read_csv_frame(path: str | Path, **kwargs: object) -> pd.DataFrame:
    """Read a rectangular CSV into a DataFrame."""
    csv_path = Path(path)
    try:
        return pd.read_csv(csv_path, encoding="utf-8-sig", **kwargs)
    except UnicodeDecodeError:
        return pd.read_csv(csv_path, encoding="latin-1", **kwargs)
    except Exception as exc:  # pragma: no cover - filesystem/backend failures
        raise StatementCorruptError(
            "Could not parse CSV statement as a table",
            {"path": str(csv_path), "error": str(exc)},
        ) from exc


def group_sectioned_rows(rows: list[list[str]]) -> dict[str, list[list[str]]]:
    """Group IBKR-style section-tagged rows by the first CSV column."""
    sections: dict[str, list[list[str]]] = {}
    for row in rows:
        if not row:
            continue
        section = row[0].strip()
        if not section:
            continue
        sections.setdefault(section, []).append(row)
    return sections
