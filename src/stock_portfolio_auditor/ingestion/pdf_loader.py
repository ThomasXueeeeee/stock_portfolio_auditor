# SPDX-License-Identifier: MIT
"""PDF text and table extraction utilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber
import pypdfium2 as pdfium
from loguru import logger

from stock_portfolio_auditor.domain.errors import StatementCorruptError

MIN_TEXT_CHARS_PER_PAGE = 200


@dataclass(frozen=True, slots=True)
class PdfPageText:
    """Extracted text for a single PDF page."""

    page_number: int
    text: str
    used_ocr: bool = False


@dataclass(frozen=True, slots=True)
class PdfDocumentText:
    """Extracted PDF text with per-page metadata."""

    path: Path
    pages: tuple[PdfPageText, ...]

    @property
    def text(self) -> str:
        """Return all page text joined by form-feed separators."""
        return "\f".join(page.text for page in self.pages)

    @property
    def looks_scanned(self) -> bool:
        """True when most pages have little or no embedded text."""
        if not self.pages:
            return False
        sparse = sum(len(page.text.strip()) < MIN_TEXT_CHARS_PER_PAGE for page in self.pages)
        return sparse / len(self.pages) >= 0.5


def extract_text_fast(path: str | Path) -> str:
    """Extract raw PDF text with pypdfium2 for cheap detection passes."""
    pdf_path = Path(path)
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        chunks: list[str] = []
        for page in doc:
            text_page = page.get_textpage()
            chunks.append(text_page.get_text_range())
        return "\f".join(chunks)
    except Exception as exc:  # pragma: no cover - backend-specific failures
        raise StatementCorruptError(
            "Could not extract fast text from PDF",
            {"path": str(pdf_path), "error": str(exc)},
        ) from exc


def extract_text_layout(path: str | Path, *, ocr_if_needed: bool = False) -> PdfDocumentText:
    """Extract layout-aware text with pdfplumber.

    OCR fallback is intentionally opt-in because it is slow and rarely needed for
    broker statements. The actual OCR implementation lives in ``ocr_fallback`` to
    keep this module lightweight.
    """
    pdf_path = Path(path)
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = tuple(
                PdfPageText(page_number=index + 1, text=page.extract_text() or "")
                for index, page in enumerate(pdf.pages)
            )
    except Exception as exc:  # pragma: no cover - backend-specific failures
        raise StatementCorruptError(
            "Could not extract layout text from PDF",
            {"path": str(pdf_path), "error": str(exc)},
        ) from exc

    document = PdfDocumentText(path=pdf_path, pages=pages)
    if ocr_if_needed and document.looks_scanned:
        logger.info("PDF appears scanned; using OCR fallback", path=str(pdf_path))
        from stock_portfolio_auditor.ingestion.ocr_fallback import extract_text_ocr

        return extract_text_ocr(pdf_path)

    return document


def extract_tables(
    path: str | Path, *, table_settings: dict[str, Any] | None = None
) -> list[list[list[str]]]:
    """Extract all tables from a PDF using pdfplumber."""
    pdf_path = Path(path)
    settings = table_settings or {}
    try:
        tables: list[list[list[str]]] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_tables = page.extract_tables(table_settings=settings)
                for table in page_tables:
                    tables.append([[cell or "" for cell in row] for row in table])
        return tables
    except Exception as exc:  # pragma: no cover - backend-specific failures
        raise StatementCorruptError(
            "Could not extract tables from PDF",
            {"path": str(pdf_path), "error": str(exc)},
        ) from exc
