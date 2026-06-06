# SPDX-License-Identifier: MIT
"""Broker and file-format detection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from stock_portfolio_auditor.domain.errors import BrokerNotDetectedError, StatementCorruptError
from stock_portfolio_auditor.ingestion.csv_loader import read_csv_rows
from stock_portfolio_auditor.ingestion.html_loader import read_html_text
from stock_portfolio_auditor.ingestion.pdf_loader import extract_text_fast


class StatementFormat(StrEnum):
    """Supported statement file formats."""

    PDF = "pdf"
    CSV = "csv"
    HTML = "html"


class Broker(StrEnum):
    """Supported broker identifiers."""

    SCHWAB = "schwab"
    IBKR = "ibkr"
    FIDELITY = "fidelity"
    ROBINHOOD = "robinhood"


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Detected broker and file format."""

    broker: Broker
    format: StatementFormat
    confidence: float
    evidence: str


def detect_format(path: str | Path) -> StatementFormat:
    """Detect statement format by extension."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return StatementFormat.PDF
    if suffix == ".csv":
        return StatementFormat.CSV
    if suffix in {".html", ".htm"}:
        return StatementFormat.HTML
    raise StatementCorruptError("Unsupported statement extension", {"path": str(path)})


def detect_broker(path: str | Path, *, text_sample: str | None = None) -> DetectionResult:
    """Detect broker and format from a file."""
    file_path = Path(path)
    fmt = detect_format(file_path)
    text = text_sample or _read_sample(file_path, fmt)
    lowered = text.lower()

    if (
        "interactive brokers" in lowered
        or "activity statement" in lowered
        and "u" in file_path.name.lower()
    ):
        return DetectionResult(Broker.IBKR, fmt, 0.95, "interactive brokers/activity statement")
    if "charles schwab" in lowered or "schwab" in lowered and "brokerage statement" in lowered:
        return DetectionResult(Broker.SCHWAB, fmt, 0.95, "schwab brokerage statement")
    if "fidelity" in lowered and ("account summary" in lowered or "portfolio summary" in lowered):
        return DetectionResult(Broker.FIDELITY, fmt, 0.90, "fidelity summary text")
    if "robinhood" in lowered:
        return DetectionResult(Broker.ROBINHOOD, fmt, 0.90, "robinhood text")

    raise BrokerNotDetectedError(
        "Could not detect broker",
        {"path": str(file_path), "format": fmt.value},
    )


def _read_sample(path: Path, fmt: StatementFormat) -> str:
    if fmt is StatementFormat.PDF:
        return extract_text_fast(path)[:10_000]
    if fmt is StatementFormat.HTML:
        return read_html_text(path)[:10_000]
    rows = read_csv_rows(path)
    return "\n".join(",".join(row[:6]) for row in rows[:80])
