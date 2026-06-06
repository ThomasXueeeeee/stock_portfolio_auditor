# SPDX-License-Identifier: MIT
"""HTML loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pandas as pd
from bs4 import BeautifulSoup

from stock_portfolio_auditor.domain.errors import StatementCorruptError


def read_html_text(path: str | Path) -> str:
    """Read HTML and return visible-ish text."""
    html_path = Path(path)
    try:
        html = html_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        html = html_path.read_text(encoding="latin-1")
    except Exception as exc:  # pragma: no cover - filesystem failures
        raise StatementCorruptError(
            "Could not read HTML statement",
            {"path": str(html_path), "error": str(exc)},
        ) from exc

    soup = BeautifulSoup(html, "lxml")
    return soup.get_text("\n", strip=True)


def read_html_tables(path: str | Path) -> list[pd.DataFrame]:
    """Read all tables from an HTML statement."""
    html_path = Path(path)
    try:
        return cast(list[pd.DataFrame], pd.read_html(html_path))
    except Exception as exc:  # pragma: no cover - backend-specific failures
        raise StatementCorruptError(
            "Could not extract HTML statement tables",
            {"path": str(html_path), "error": str(exc)},
        ) from exc
