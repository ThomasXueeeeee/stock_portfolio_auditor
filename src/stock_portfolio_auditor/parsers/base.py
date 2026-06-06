# SPDX-License-Identifier: MIT
"""Parser interfaces and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from stock_portfolio_auditor.domain.errors import FormatNotSupportedError
from stock_portfolio_auditor.domain.models import Statement
from stock_portfolio_auditor.parsers.detect import (
    Broker,
    DetectionResult,
    StatementFormat,
    detect_broker,
)


class BrokerParser(ABC):
    """Base class for all broker parsers."""

    broker: Broker
    supported_formats: frozenset[StatementFormat]
    parser_version: int = 1

    def supports(self, fmt: StatementFormat) -> bool:
        """Return true if parser supports the given format."""
        return fmt in self.supported_formats

    @abstractmethod
    def parse(self, path: str | Path, *, account_label: str | None = None) -> Statement:
        """Parse a statement file into a normalized Statement."""


ParserFactory = Callable[[], BrokerParser]
_REGISTRY: dict[tuple[Broker, StatementFormat], ParserFactory] = {}


def register_parser(broker: Broker, fmt: StatementFormat, factory: ParserFactory) -> None:
    """Register a parser factory."""
    _REGISTRY[(broker, fmt)] = factory


def get_parser(broker: Broker, fmt: StatementFormat) -> BrokerParser:
    """Instantiate a registered parser."""
    key = (broker, fmt)
    if key not in _REGISTRY:
        raise FormatNotSupportedError(
            "No parser registered for broker and format",
            {"broker": broker.value, "format": fmt.value},
        )
    return _REGISTRY[key]()


def parse_statement(path: str | Path, *, account_label: str | None = None) -> Statement:
    """Detect broker/format and parse a statement."""
    detection = detect_broker(path)
    return parse_detected(path, detection=detection, account_label=account_label)


def parse_detected(
    path: str | Path,
    *,
    detection: DetectionResult,
    account_label: str | None = None,
) -> Statement:
    """Parse a statement with an existing detection result."""
    parser = get_parser(detection.broker, detection.format)
    if not parser.supports(detection.format):
        raise FormatNotSupportedError(
            "Parser does not support detected statement format",
            {"broker": detection.broker.value, "format": detection.format.value, "path": str(path)},
        )
    return parser.parse(path, account_label=account_label)
