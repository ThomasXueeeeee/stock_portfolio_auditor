# SPDX-License-Identifier: MIT
"""IBKR Activity Statement HTML parser.

IBKR's HTML export presents the same logical sections as CSV, but as labelled
HTML tables. The full table-to-section mapper is implemented after synthetic
HTML fixtures are introduced. This module exists now so detection/dispatch can
route HTML files consistently.
"""

from __future__ import annotations

from pathlib import Path

from stock_portfolio_auditor.domain.errors import FormatNotSupportedError
from stock_portfolio_auditor.domain.models import Statement
from stock_portfolio_auditor.ingestion.html_loader import read_html_tables, read_html_text
from stock_portfolio_auditor.parsers.base import BrokerParser, register_parser
from stock_portfolio_auditor.parsers.detect import Broker, StatementFormat


class IBKRHtmlParser(BrokerParser):
    """Parse IBKR Activity Statement HTML exports."""

    broker = Broker.IBKR
    supported_formats = frozenset({StatementFormat.HTML})
    parser_version = 1

    def parse(self, path: str | Path, *, account_label: str | None = None) -> Statement:
        """Parse an IBKR HTML statement.

        HTML support is part of the public dispatch surface, but the robust
        implementation waits for fixture-backed table mapping. CSV is preferred
        whenever available.
        """
        html_path = Path(path)
        read_html_text(html_path)
        read_html_tables(html_path)
        raise FormatNotSupportedError(
            "IBKR HTML parser is scaffolded; use CSV export until HTML fixtures are mapped",
            {"path": str(html_path), "account_label": account_label},
        )


register_parser(Broker.IBKR, StatementFormat.HTML, IBKRHtmlParser)
