from __future__ import annotations

from stock_portfolio_auditor.parsers.detect import Broker, StatementFormat, detect_broker


def test_detect_ibkr_csv_from_text_sample() -> None:
    result = detect_broker(
        "U1234567_2024_2024.csv",
        text_sample="Statement,Data,BrokerName,Interactive Brokers LLC\nStatement,Data,Title,Activity Statement",
    )
    assert result.broker is Broker.IBKR
    assert result.format is StatementFormat.CSV


def test_detect_schwab_pdf_from_text_sample() -> None:
    result = detect_broker(
        "Brokerage Statement_2024-03-31_755.PDF",
        text_sample="Charles Schwab Brokerage Statement",
    )
    assert result.broker is Broker.SCHWAB
    assert result.format is StatementFormat.PDF


def test_detect_fidelity_pdf_from_text_sample() -> None:
    result = detect_broker(
        "fidelity.pdf",
        text_sample="Fidelity Account Summary Portfolio Summary",
    )
    assert result.broker is Broker.FIDELITY


def test_detect_robinhood_pdf_from_text_sample() -> None:
    result = detect_broker("rh.pdf", text_sample="Robinhood Account Summary")
    assert result.broker is Broker.ROBINHOOD


def test_detect_ibkr_html_format_from_extension() -> None:
    result = detect_broker(
        "activity.html",
        text_sample="Interactive Brokers Activity Statement",
    )
    assert result.broker is Broker.IBKR
    assert result.format is StatementFormat.HTML
