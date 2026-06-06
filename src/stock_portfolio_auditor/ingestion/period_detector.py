# SPDX-License-Identifier: MIT
"""Statement period detection across supported broker formats."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from stock_portfolio_auditor.domain.errors import PeriodDetectionError

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

ISO_DATE_RE = re.compile(r"(?P<y>20\d{2})[-_/](?P<m>\d{1,2})[-_/](?P<d>\d{1,2})")
MONTH_DATE_RE = re.compile(
    r"(?P<m1>[A-Za-z]+)\s+(?P<d1>\d{1,2}),?\s+(?P<y1>20\d{2})\s*"
    r"(?:-|to|through|–|—)\s*"
    r"(?P<m2>[A-Za-z]+)\s+(?P<d2>\d{1,2}),?\s+(?P<y2>20\d{2})",
    re.IGNORECASE,
)
MONTH_DATE_SAME_YEAR_RE = re.compile(
    r"(?P<m1>[A-Za-z]+)\s+(?P<d1>\d{1,2})\s*"
    r"(?:-|to|through|–|—)\s*"
    r"(?P<m2>[A-Za-z]+)\s+(?P<d2>\d{1,2}),?\s+(?P<y>20\d{2})",
    re.IGNORECASE,
)
IBKR_PERIOD_RE = re.compile(r"Period,?\"?(?P<period>[^\"\n\r]+)\"?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StatementPeriod:
    """Normalized statement coverage period."""

    start: date
    end: date
    frequency: str


def infer_frequency(start: date, end: date) -> str:
    """Infer M/Q/A from date span."""
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    if months <= 1:
        return "M"
    if months <= 3:
        return "Q"
    return "A"


def _month_number(value: str) -> int:
    key = value.strip().lower()
    if key not in MONTHS:
        raise ValueError(f"Unknown month name: {value!r}")
    return MONTHS[key]


def detect_period_from_text(text: str, *, source: str | Path | None = None) -> StatementPeriod:
    """Detect a statement period from free text."""
    normalized = text.replace("\u2013", "-").replace("\u2014", "-")

    ibkr_match = IBKR_PERIOD_RE.search(normalized)
    if ibkr_match:
        parsed = _parse_period_phrase(ibkr_match.group("period"))
        if parsed is not None:
            return parsed

    for pattern in (MONTH_DATE_RE, MONTH_DATE_SAME_YEAR_RE):
        match = pattern.search(normalized)
        if not match:
            continue
        if "y" in match.groupdict() and match.group("y"):
            y1 = y2 = int(match.group("y"))
        else:
            y1 = int(match.group("y1"))
            y2 = int(match.group("y2"))
        start = date(y1, _month_number(match.group("m1")), int(match.group("d1")))
        end = date(y2, _month_number(match.group("m2")), int(match.group("d2")))
        return StatementPeriod(start=start, end=end, frequency=infer_frequency(start, end))

    raise PeriodDetectionError(
        "Could not detect statement period",
        {"source": str(source) if source is not None else None},
    )


def detect_period_from_filename(path: str | Path) -> StatementPeriod | None:
    """Best-effort period fallback from common filename dates."""
    file_path = Path(path)
    matches = list(ISO_DATE_RE.finditer(file_path.name))
    if not matches:
        year_only = re.search(r"(?P<y>20\d{2})_(?P<y2>20\d{2})", file_path.name)
        if year_only:
            year = int(year_only.group("y"))
            start = date(year, 1, 1)
            end = date(year, 12, 31)
            return StatementPeriod(start=start, end=end, frequency="A")
        return None
    end_match = matches[-1]
    end = date(
        int(end_match.group("y")),
        int(end_match.group("m")),
        int(end_match.group("d")),
    )
    start = date(end.year, end.month, 1)
    return StatementPeriod(start=start, end=end, frequency="M")


def _parse_period_phrase(value: str) -> StatementPeriod | None:
    phrase = value.strip().strip('"')
    for pattern in (MONTH_DATE_RE, MONTH_DATE_SAME_YEAR_RE):
        match = pattern.search(phrase)
        if match:
            if "y" in match.groupdict() and match.group("y"):
                y1 = y2 = int(match.group("y"))
            else:
                y1 = int(match.group("y1"))
                y2 = int(match.group("y2"))
            start = date(y1, _month_number(match.group("m1")), int(match.group("d1")))
            end = date(y2, _month_number(match.group("m2")), int(match.group("d2")))
            return StatementPeriod(start=start, end=end, frequency=infer_frequency(start, end))
    return None
