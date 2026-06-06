# SPDX-License-Identifier: MIT
"""OCR fallback for scanned broker statements."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
import pytesseract
from PIL import Image

from stock_portfolio_auditor.domain.errors import StatementCorruptError
from stock_portfolio_auditor.ingestion.pdf_loader import PdfDocumentText, PdfPageText


def extract_text_ocr(path: str | Path, *, scale: float = 2.0) -> PdfDocumentText:
    """Render each PDF page and OCR it with Tesseract."""
    pdf_path = Path(path)
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        pages: list[PdfPageText] = []
        for index, page in enumerate(doc):
            bitmap = page.render(scale=scale).to_pil()
            if not isinstance(bitmap, Image.Image):
                raise TypeError("pypdfium2 did not return a PIL image")
            text = pytesseract.image_to_string(bitmap)
            pages.append(PdfPageText(page_number=index + 1, text=text, used_ocr=True))
        return PdfDocumentText(path=pdf_path, pages=tuple(pages))
    except Exception as exc:  # pragma: no cover - depends on local OCR engine
        raise StatementCorruptError(
            "Could not OCR PDF",
            {"path": str(pdf_path), "error": str(exc)},
        ) from exc
