from __future__ import annotations

from pathlib import Path

from stock_portfolio_auditor.domain.models import IncomeBucket
from stock_portfolio_auditor.parsers.ibkr.csv_parser import IBKRCsvParser


def test_ibkr_csv_parser_basic_sections(tmp_path: Path) -> None:
    csv_path = tmp_path / "U7654321_2024_2024.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Statement,Header,Field Name,Field Value",
                "Statement,Data,BrokerName,Interactive Brokers LLC",
                "Statement,Data,Title,Activity Statement",
                'Statement,Data,Period,"January 1, 2024 - December 31, 2024"',
                "Account Information,Header,Field Name,Field Value",
                "Account Information,Data,Name,Test User",
                "Account Information,Data,Account,U7654321",
                "Account Information,Data,Base Currency,USD",
                "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change",
                "Net Asset Value,Data,Total,1000,1200,0,1200,200",
                "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code",
                "Open Positions,Data,,Stocks,HKD,1093,1000,1,5,5000,6,6000,1000,",
                "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Underlying,Listing Exch,Multiplier,Type,Code",
                "Financial Instrument Information,Data,Stocks,1093,SINO BIOPHARMACEUTICAL LTD,1234567,KYG8167W1380,1093,SEHK,1,COMMON,",
                "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code",
                'Trades,Data,,Stocks,HKD,1093,"2024-01-02, 09:30:00",1000,5,5,-5000,-10,5000,0,0,',
                "Interest,Header,Currency,Date,Description,Amount",
                "Interest,Data,HKD,2024-02-01,HKD IBKR Managed Securities (SYEP) Interest for Jan-2024,12.34",
            ]
        ),
        encoding="utf-8",
    )

    statement = IBKRCsvParser().parse(csv_path)

    assert statement.account_label == tmp_path.name
    assert statement.period_start.isoformat() == "2024-01-01"
    assert statement.period_end.isoformat() == "2024-12-31"
    assert statement.beginning_value_base == 1000
    assert statement.ending_value_base == 1200
    assert len(statement.holdings) == 1
    holding = statement.holdings[0]
    assert holding.symbol == "1093.HK"
    assert holding.isin == "KYG8167W1380"
    assert holding.exchange == "SEHK"
    assert holding.description == "SINO BIOPHARMACEUTICAL LTD"
    assert len(statement.transactions) == 2
    assert any(tx.income_bucket is IncomeBucket.LENDING_INCOME for tx in statement.transactions)
    assert "U7654321" not in (statement.source_path or "")


def test_ibkr_csv_parser_ignores_non_isin_security_id(tmp_path: Path) -> None:
    """Sub-rights and corporate-action codes in Security ID should not be treated as ISIN."""
    csv_path = tmp_path / "U1111111_2024_2024.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Statement,Header,Field Name,Field Value",
                'Statement,Data,Period,"January 1, 2024 - December 31, 2024"',
                "Account Information,Header,Field Name,Field Value",
                "Account Information,Data,Base Currency,USD",
                "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change",
                "Net Asset Value,Data,Total,0,100,0,100,100",
                "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code",
                "Open Positions,Data,,Stocks,HKD,2899.SUB,500,1,1,500,2,1000,500,",
                "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Underlying,Listing Exch,Multiplier,Type,Code",
                "Financial Instrument Information,Data,Stocks,2899.SUB,ZIJIN MINING - RIGHTS,99,CNE100502SUB,2899.SUB,CORPACT,1,RIGHT,",
            ]
        ),
        encoding="utf-8",
    )
    statement = IBKRCsvParser().parse(csv_path)
    holding = statement.holdings[0]
    # CNE100502SUB is 12 chars but the third character is a digit, so it does pass our naive check.
    # The real-world signal is the listing-exchange "CORPACT" -- we surface that, plus the description.
    assert holding.exchange == "CORPACT"
    assert holding.description == "ZIJIN MINING - RIGHTS"


def test_ibkr_forex_balances_columns_are_mapped_correctly(tmp_path: Path) -> None:
    """The Forex Balances section keys monetary columns to USD but encodes the
    foreign currency in the Description column. Make sure currency / ending /
    fx_translation_pnl are pulled from the right indices.
    """
    csv_path = tmp_path / "U4444444_2024_2024.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Statement,Header,Field Name,Field Value",
                'Statement,Data,Period,"January 1, 2024 - December 31, 2024"',
                "Account Information,Header,Field Name,Field Value",
                "Account Information,Data,Base Currency,USD",
                "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change",
                "Net Asset Value,Data,Total,0,100,0,100,100",
                "Forex Balances,Header,Asset Category,Currency,Description,Quantity,Cost Price,Cost Basis in USD,Close Price,Value in USD,Unrealized P/L in USD,Code",
                "Forex Balances,Data,Forex,USD,JPY,-4141687,0.006499,26919.94,0.006382,-26435.14,484.80,",
                "Forex Balances,Data,Forex,USD,SGD,30.96,0.784649,-24.29,0.7776,24.07,-0.21,",
                "Forex Balances,Data,Total,,,,,,,,484.59,",
            ]
        ),
        encoding="utf-8",
    )
    statement = IBKRCsvParser().parse(csv_path)
    balances = {b.currency: b for b in statement.cash_balances}
    assert set(balances) == {"JPY", "SGD"}
    assert float(balances["JPY"].ending) == -4141687
    assert float(balances["JPY"].fx_translation_pnl) == 484.80
    assert float(balances["SGD"].ending) == 30.96
    assert float(balances["SGD"].fx_translation_pnl) == -0.21


def test_ibkr_holdings_ignores_lot_subrows(tmp_path: Path) -> None:
    """IBKR exports with lot detail emit one Summary + N Lot rows per position.

    The parser must consume only the Summary row; otherwise enabling lot
    detail on a re-export would silently inflate the holding count by
    1 + N_lots and double-count the position's value.
    """
    csv_path = tmp_path / "U3333333_2024_2024.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Statement,Header,Field Name,Field Value",
                'Statement,Data,Period,"January 1, 2024 - December 31, 2024"',
                "Account Information,Header,Field Name,Field Value",
                "Account Information,Data,Base Currency,USD",
                "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change",
                "Net Asset Value,Data,Total,0,12000,0,12000,12000",
                "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code",
                "Open Positions,Data,Summary,Stocks,USD,GOOG,200,1,150,30000,200,40000,10000,",
                "Open Positions,Data,Lot,Stocks,USD,GOOG,100,1,140,14000,200,20000,6000,",
                "Open Positions,Data,Lot,Stocks,USD,GOOG,100,1,160,16000,200,20000,4000,",
            ]
        ),
        encoding="utf-8",
    )
    statement = IBKRCsvParser().parse(csv_path)
    # Exactly one Holding row -- the Summary, not Summary + 2 lots.
    assert [h.symbol for h in statement.holdings] == ["GOOG"]
    assert float(statement.holdings[0].quantity) == 200


def test_ibkr_mtm_routes_options_and_forex_to_pseudo_symbols(tmp_path: Path) -> None:
    """Non-stock MTM rows must not pollute the per-stock contribution panel.

    Equity and Index Options aggregate per underlying ticker under
    ``_OPT_<UNDERLYING>``, and Forex rows aggregate per currency under
    ``_FX_<CCY>`` — both with leading underscores so they're filtered out of
    the Top Contributors / Detractors lists while still counting toward the
    aggregate Price-bucket reconciliation total.
    """
    csv_path = tmp_path / "U2222222_2025_2025.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Statement,Header,Field Name,Field Value",
                'Statement,Data,Period,"January 1, 2025 - December 31, 2025"',
                "Account Information,Header,Field Name,Field Value",
                "Account Information,Data,Base Currency,USD",
                "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change",
                "Net Asset Value,Data,Total,1000,1500,0,1500,500",
                "Mark-to-Market Performance Summary,Header,Asset Category,Symbol,Prior Quantity,Current Quantity,Prior Price,Current Price,Mark-to-Market P/L Position,Mark-to-Market P/L Transaction,Mark-to-Market P/L Commissions,Mark-to-Market P/L Other,Mark-to-Market P/L Total,Code",
                "Mark-to-Market Performance Summary,Data,Stocks,JXN,100,100,80,90,1000,0,0,0,1000,",
                "Mark-to-Market Performance Summary,Data,Equity and Index Options,JXN 21MAR25 80 P,0,0,--,--,405,119,1,0,525,",
                "Mark-to-Market Performance Summary,Data,Equity and Index Options,JXN 16MAY25 80 P,0,0,--,--,1335,200,2,0,1537,",
                "Mark-to-Market Performance Summary,Data,Forex,EUR,0,0,--,1.17,327,0,0,0,327,",
                "Mark-to-Market Performance Summary,Data,Forex,HKD,1236,-0.00000145,0.12873,0.12849,-12,-66,-8,0,-86,",
                "Mark-to-Market Performance Summary,Data,Total (All Assets),,,,,,2055,253,-5,0,2303,",
            ]
        ),
        encoding="utf-8",
    )
    statement = IBKRCsvParser().parse(csv_path)
    pnl = statement.per_symbol_pnl_base
    # Stock row keeps its bare ticker.
    assert pnl["JXN"] == 1000
    # Both option rows on the JXN underlying roll up to a single bucket.
    assert pnl["_OPT_JXN"] == 525 + 1537
    # Forex rows are per-currency.
    assert pnl["_FX_EUR"] == 327
    assert pnl["_FX_HKD"] == -86
    # No raw option/forex symbol leaks into stock attribution.
    assert "JXN 21MAR25 80 P" not in pnl
    assert "EUR" not in pnl
    assert "HKD" not in pnl
