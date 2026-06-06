"""Broker parser interfaces."""

from stock_portfolio_auditor.parsers.base import (
    BrokerParser,
    get_parser,
    parse_detected,
    parse_statement,
    register_parser,
)
from stock_portfolio_auditor.parsers.detect import (
    Broker,
    DetectionResult,
    StatementFormat,
    detect_broker,
)
from stock_portfolio_auditor.parsers.ibkr import IBKRCsvParser, IBKRHtmlParser, IBKRPdfParser
from stock_portfolio_auditor.parsers.schwab import SchwabParser

__all__ = [
    "Broker",
    "BrokerParser",
    "DetectionResult",
    "IBKRCsvParser",
    "IBKRHtmlParser",
    "IBKRPdfParser",
    "SchwabParser",
    "StatementFormat",
    "detect_broker",
    "get_parser",
    "parse_detected",
    "parse_statement",
    "register_parser",
]
