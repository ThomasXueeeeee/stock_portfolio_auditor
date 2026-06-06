from __future__ import annotations

from pathlib import Path

import pytest

from stock_portfolio_auditor.parsers.base import parse_statement
from stock_portfolio_auditor.parsers.detect import Broker, StatementFormat, detect_broker
from tests.local_data import discover_statement_files

STATEMENT_FILES = discover_statement_files()


@pytest.mark.local_data
@pytest.mark.parametrize(
    "statement_path",
    STATEMENT_FILES,
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_local_statement_detection(statement_path: Path) -> None:
    result = detect_broker(statement_path)
    assert result.broker in {Broker.SCHWAB, Broker.IBKR, Broker.FIDELITY, Broker.ROBINHOOD}
    assert result.format in {StatementFormat.PDF, StatementFormat.CSV, StatementFormat.HTML}


@pytest.mark.local_data
@pytest.mark.parametrize(
    "statement_path",
    [path for path in STATEMENT_FILES if path.suffix.lower() == ".csv"],
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_local_csv_statement_parses(statement_path: Path) -> None:
    statement = parse_statement(statement_path)
    assert statement.account_label == statement_path.parent.name
    assert statement.period_start <= statement.period_end
    assert statement.raw_text_hash
    assert not statement.account_label.lower().startswith("u")
