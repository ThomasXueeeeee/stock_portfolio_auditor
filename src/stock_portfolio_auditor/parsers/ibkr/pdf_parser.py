# SPDX-License-Identifier: MIT
"""IBKR Activity Statement PDF parser scaffold."""

from __future__ import annotations

from pathlib import Path

from stock_portfolio_auditor.domain.errors import FormatNotSupportedError
from stock_portfolio_auditor.domain.models import Statement
from stock_portfolio_auditor.ingestion.pdf_loader import extract_text_layout
from stock_portfolio_auditor.parsers.base import BrokerParser, register_parser
from stock_portfolio_auditor.parsers.detect import Broker, StatementFormat


class IBKRPdfParser(BrokerParser):
    """Parse IBKR Activity Statement PDF exports."""

    broker = Broker.IBKR
    supported_formats = frozenset({StatementFormat.PDF})
    parser_version = 1

    def parse(self, path: str | Path, *, account_label: str | None = None) -> Statement:
        """Parse an IBKR PDF statement.

        PDF support is scaffolded for dispatch parity. CSV is strongly preferred
        because IBKR CSV is section-tagged and deterministic.
        """
        pdf_path = Path(path)
        extract_text_layout(pdf_path)
        raise FormatNotSupportedError(
            "IBKR PDF parser is scaffolded; use CSV export until PDF fixtures are mapped",
            {"path": str(pdf_path), "account_label": account_label},
        )


register_parser(Broker.IBKR, StatementFormat.PDF, IBKRPdfParser)
