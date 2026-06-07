# SPDX-License-Identifier: MIT
"""Unit tests for the Schwab holdings parser.

These tests target the holdings text-parsing helpers directly so they can run
without committing a real PDF fixture. The end-to-end PDF integration is
covered by the existing local-data tests when a records folder is present.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.domain.models import AssetKind
from stock_portfolio_auditor.parsers.schwab import _extract_holdings


def test_schwab_equities_section_parsed() -> None:
    text = """
Schwab One Account of
Positions - Summary

Positions - Equities
Unrealized Est. Est.Annual
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Yield Income($)
YOU CLEARSECUREINC(M) 500.0000 53.39000 26,695.00 15,885.80 10,809.20 1.12% 300.00
EZPW EZCORPINC(M) 1,330.0000 32.78000 43,597.40 19,827.69 23,769.71 N/A N/A
TotalEquities $70,292.40 $35,713.49 $34,578.91 $300.00
"""
    holdings = _extract_holdings(text, date(2026, 4, 30))
    assert len(holdings) == 2
    you = holdings[0]
    assert you.symbol == "YOU"
    assert you.description == "CLEARSECUREINC(M)"
    assert you.kind is AssetKind.EQUITY
    assert you.quantity == Decimal("500.0000")
    assert you.market_value_base == Decimal("26695.0000")
    assert you.cost_basis_local == Decimal("15885.8000")
    ezpw = holdings[1]
    assert ezpw.symbol == "EZPW"
    assert ezpw.market_value_base == Decimal("43597.4000")


def test_schwab_etfs_and_equities_collected_together() -> None:
    text = """
Positions - Equities
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Yield Income($)
NBIS NEBIUSGROUPNVA F(M) 4,000.0000 138.23000 552,920.00 119,636.00 433,284.00 N/A N/A
TotalEquities $552,920.00 $119,636.00 $433,284.00

Positions - Exchange Traded Funds
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Yield Income($)
FLKR FRANKLINFTSESOUTH(M) 300.0000 52.05000 15,615.00 14,965.50 649.50 N/A N/A
TotalExchangeTradedFunds $15,615.00 $14,965.50 $649.50
"""
    holdings = _extract_holdings(text, date(2026, 4, 30))
    kinds = {h.symbol: h.kind for h in holdings}
    assert kinds == {"NBIS": AssetKind.EQUITY, "FLKR": AssetKind.ETF}
    total_mv = sum(float(h.market_value_base) for h in holdings)
    assert total_mv == 552920.0 + 15615.0


def test_schwab_holding_row_with_decimal_quantity_and_pct_yield() -> None:
    text = """
Positions - Equities
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Yield Income($)
JXN JACKSONFINLINC(M) 1,324.5850 115.77000 153,347.21 53,224.76 100,122.45 3.1% 4,768.51
TotalEquities $153,347.21 $53,224.76 $100,122.45
"""
    holdings = _extract_holdings(text, date(2026, 4, 30))
    assert len(holdings) == 1
    assert holdings[0].symbol == "JXN"
    assert holdings[0].quantity == Decimal("1324.5850")
    assert holdings[0].market_value_base == Decimal("153347.2100")


def test_schwab_ignores_unparseable_lines() -> None:
    text = """
Positions - Equities
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Yield Income($)
random garbage line that should not match
YOU CLEARSECUREINC(M) 500.0000 53.39000 26,695.00 15,885.80 10,809.20 1.12% 300.00
TotalEquities $26,695.00 $15,885.80 $10,809.20
"""
    holdings = _extract_holdings(text, date(2026, 4, 30))
    assert [h.symbol for h in holdings] == ["YOU"]


def test_schwab_returns_empty_when_no_sections_present() -> None:
    holdings = _extract_holdings("This is some unrelated text.", date(2026, 4, 30))
    assert holdings == ()


def test_schwab_ignores_account_holder_name_header_lines() -> None:
    """The account-holder name appears on every page header between holdings
    rows. The parser must skip those name-shaped lines generically instead of
    baking a specific name into a deny-list.
    """
    text = """
Positions - Equities
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Yield Income($)
JANE DOE
YOU CLEARSECUREINC(M) 500.0000 53.39000 26,695.00 15,885.80 10,809.20 1.12% 300.00
JANE DOE
EZPW EZCORPINC(M) 1,330.0000 32.78000 43,597.40 19,827.69 23,769.71 N/A N/A
TotalEquities $70,292.40 $35,713.49 $34,578.91 $300.00
"""
    holdings = _extract_holdings(text, date(2026, 4, 30))
    assert [h.symbol for h in holdings] == ["YOU", "EZPW"]


def test_schwab_options_section_aggregates_per_underlying() -> None:
    """Multiple option contracts on the same underlying must roll up into a single
    synthetic ``_OPT_<UNDERLYING>`` Holding with summed MV / cost basis.

    The cost basis on Schwab short options is parenthesised (premium received,
    treated as negative). The unrealized = MV − CB on the synthetic holding
    must therefore match the per-contract Gain/(Loss) sum on the statement
    (here 493.34 − 328.32 + 464.31 = 629.33 — same as Schwab's
    ``TotalOptions`` row).
    """
    text = """
Positions - Options
Unrealized Est.Annual
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Est.Yield Income($)
BN PUTBROOKFIELDCORP, (10.0000)S 0.15000 (150.00) (643.34) 493.34
04/17/20 $45 EXP04/17/25
2545.00
P
JXN PUTJACKSONFINLINC, (5.0000)S 1.65000 (825.00) (496.68) (328.32)
04/17/20 $80 EXP04/17/25
2580.00
P
PDD PUTPDDHOLDINGSINC, (1.0000)S 7.55000 (755.00) (1,219.31) 464.31
12/19/20 $95 EXP12/19/25
2595.00
P
TotalOptions ($1,730.00) ($2,359.33) $629.33 $0.00A
"""
    holdings = _extract_holdings(text, date(2025, 3, 31))
    by_symbol = {h.symbol: h for h in holdings}
    assert {"_OPT_BN", "_OPT_JXN", "_OPT_PDD"} <= set(by_symbol)
    bn = by_symbol["_OPT_BN"]
    assert bn.kind is AssetKind.OPTION
    assert bn.quantity == Decimal("-10.0000")
    assert bn.market_value_base == Decimal("-150.0000")
    assert bn.cost_basis_base == Decimal("-643.3400")
    # Aggregate MV / CB across all three contracts reconciles to the
    # ``TotalOptions`` row Schwab itself prints.
    total_mv = sum(
        float(by_symbol[k].market_value_base) for k in ("_OPT_BN", "_OPT_JXN", "_OPT_PDD")
    )
    total_cb = sum(float(by_symbol[k].cost_basis_base) for k in ("_OPT_BN", "_OPT_JXN", "_OPT_PDD"))
    assert total_mv == -150 - 825 - 755
    assert total_cb == -643.34 - 496.68 - 1219.31


def test_schwab_legacy_investment_detail_layout_parsed() -> None:
    """Pre-2024 Schwab statements use a multi-line ``Investment Detail`` layout.

    Each position carries a header line with total qty / MV / unrealized
    gain, followed by per-lot subrows, a ``SYMBOL: <TICKER>`` line, and a
    final ``Cost Basis <total>`` line — none of which the modern
    ``Positions - Equities`` parser knows about. This test pins down the
    multi-line state machine that emits one Holding per position from the
    legacy layout so 2022–2024 audit-window data isn't silently dropped.
    """
    text = """
Investment Detail - Equities
% of
Account Unrealized Estimated Estimated
Quantity Market Price Market Value Assets Gain or (Loss) Yield Annual Income
Equities Units Purchased Cost Per Share Cost Basis Acquired
BERKSHIRE HATHAWAY (cid:224) 14.0000 295.09000 4,131.26 25% (33.27) N/A N/A
CLASS B 1.0000 301.6700 301.67 02/24/22 (6.58)
SYMBOL: BRKB 1.0000 301.6700 301.67 02/24/22 (6.58)
2.0000 300.9500 601.90 02/24/22 (11.72)
Cost Basis 4,164.53
TENCENT HOLDINGS F 475.0000 26.28000 12,483.00 75% (3,348.37) 0.77% 96.86
UNSPONSORED ADR 147.0000 38.8572 5,712.02 09/09/22 (1,848.86)
SYMBOL: TCEHY 33.0000 30.6796 1,012.43 10/19/22 (145.19)
Cost Basis 15,831.37
Total Equities 489.0000 16,614.26 100% (3,381.64) 96.86
"""
    holdings = _extract_holdings(text, date(2022, 10, 31))
    by_symbol = {h.symbol: h for h in holdings}
    assert {"BRKB", "TCEHY"} <= set(by_symbol)
    brkb = by_symbol["BRKB"]
    assert brkb.kind is AssetKind.EQUITY
    assert brkb.quantity == Decimal("14.0000")
    assert brkb.market_value_base == Decimal("4131.2600")
    assert brkb.cost_basis_base == Decimal("4164.5300")
    tcehy = by_symbol["TCEHY"]
    assert tcehy.quantity == Decimal("475.0000")
    assert tcehy.market_value_base == Decimal("12483.0000")
    assert tcehy.cost_basis_base == Decimal("15831.3700")


def test_schwab_intermediate_investment_detail_layout_parsed() -> None:
    """Schwab's Aug 2023 - Dec 2024 statements use the Investment Detail
    section header without the dedicated ``Cost Basis NNN`` subtotal row.
    The cost basis is instead glued onto the first sub-description line
    under each position header, e.g. ``CLASS B 63,692.88``. This format
    sat between the original legacy (with explicit Cost Basis rows) and
    the modern Positions - Equities single-row layout and silently
    dropped all per-position holdings for ~17 months of audit coverage
    until the parser learned to recognise it.
    """
    text = """
Investment Detail - Equities
% of
Account Unrealized Estimated Estimated
Quantity Market Price Market Value Assets Gain or (Loss) Yield Annual Income
Equities Cost Basis
BERKSHIRE HATHAWAY (M) 218.0000 360.20000 78,523.60 16% 14,830.72 N/A N/A
CLASS B 63,692.88
SYMBOL: BRKB
BRIT AMER TOBACCO F (M),(cid:224), 132.1246 33.20000 4,386.54 1% 53.00 8.84% 388.13
UNSPONSORED ADR 4,333.54
1 ADR REPS 1 ORD SHS
SYMBOL: BTI
BROOKFIELD ASSET MANAG F (M) 200.0000 34.55000 6,910.00 1% 509.27 N/A N/A
CLASS A 6,400.73
SYMBOL: BAM
Total Equities 550.1246 89,820.14 18% 15,392.99 388.13
"""
    holdings = _extract_holdings(text, date(2023, 8, 31))
    by_symbol = {h.symbol: h for h in holdings}
    assert {"BRKB", "BTI", "BAM"} <= set(by_symbol)
    brkb = by_symbol["BRKB"]
    assert brkb.quantity == Decimal("218.0000")
    assert brkb.market_value_base == Decimal("78523.6000")
    assert brkb.cost_basis_base == Decimal("63692.8800")
    bti = by_symbol["BTI"]
    # BTI has a *second* sub-description line ("1 ADR REPS 1 ORD SHS")
    # after the one carrying the cost basis. The parser must NOT
    # overwrite the captured cb with that later line's lack-of-money.
    assert bti.cost_basis_base == Decimal("4333.5400")
    bam = by_symbol["BAM"]
    assert bam.cost_basis_base == Decimal("6400.7300")


def test_schwab_intermediate_inline_cost_basis_on_symbol_line() -> None:
    """A second intermediate-format variant (Jun-Oct 2024 brokerage PDFs)
    puts the cost basis on the SYMBOL line itself instead of on a
    preceding sub-description line::

        CONSOL ENERGY INC (M) 400.0000 102.03000 40,812.00 7% 5,102.05 N/A N/A
        SYMBOL: CEIX 35,709.95

    Without this rule the parser would commit BRKB / JXN with
    sub-description-glued cost bases but drop CEIX entirely, undercounting
    the position list.
    """
    text = """
Investment Detail - Equities
% of
Account Unrealized Estimated Estimated
Quantity Market Price Market Value Assets Gain or (Loss) Yield Annual Income
Equities Cost Basis
CONSOL ENERGY INC (M) 400.0000 102.03000 40,812.00 7% 5,102.05 N/A N/A
SYMBOL: CEIX 35,709.95
JACKSON FINL INC (M) 1,020.5850 74.26000 75,788.64 14% 45,927.33 3.77% 2,857.64
CLASS A 29,861.31
SYMBOL: JXN
Total Equities 1,420.5850 116,600.64 21% 51,029.38 2,857.64
"""
    holdings = _extract_holdings(text, date(2024, 6, 30))
    by_symbol = {h.symbol: h for h in holdings}
    assert {"CEIX", "JXN"} <= set(by_symbol)
    assert by_symbol["CEIX"].cost_basis_base == Decimal("35709.9500")
    assert by_symbol["JXN"].cost_basis_base == Decimal("29861.3100")


def test_schwab_legacy_layout_does_not_overwrite_modern_layout_holdings() -> None:
    """When both modern and legacy section headers appear in the same text,
    modern Holdings are kept and legacy duplicates are filtered out. This is
    the natural state in a monthly statement just after the format switch
    where Schwab still prints a footnote referencing the legacy header.
    """
    text = """
Positions - Equities
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Yield Income($)
JXN JACKSONFINLINC(M) 100.0000 100.00000 10,000.00 9,000.00 1,000.00 N/A N/A
TotalEquities $10,000.00 $9,000.00 $1,000.00

Investment Detail - Equities
% of
Account Unrealized Estimated Estimated
Quantity Market Price Market Value Assets Gain or (Loss) Yield Annual Income
JACKSON FINANCIAL 100.0000 100.00000 10,000.00 100% 1,000.00 N/A N/A
SYMBOL: JXN 100.0000 90.0000 9,000.00 01/01/22 1,000.00
Cost Basis 9,000.00
Total Equities 100.0000 10,000.00 100% 1,000.00
"""
    holdings = _extract_holdings(text, date(2024, 3, 31))
    symbols = [h.symbol for h in holdings]
    assert symbols.count("JXN") == 1


def test_schwab_options_aggregate_multiple_contracts_on_same_underlying() -> None:
    """Two contracts on the same underlying must collapse into a single Holding."""
    text = """
Positions - Options
Symbol Description Quantity Price($) Market Value($) CostBasis($) Gain/(Loss)($) Est.Yield Income($)
NBIS PUTNEBIUSGROUPNV, (5.0000)S 1.00000 (500.00) (3,000.00) 2,500.00
03/21/20 $30 EXP03/21/25
2530.00
P
NBIS PUTNEBIUSGROUPNV, (3.0000)S 0.50000 (150.00) (1,200.00) 1,050.00
06/20/20 $25 EXP06/20/25
2525.00
P
TotalOptions ($650.00) ($4,200.00) $3,550.00 $0.00A
"""
    holdings = _extract_holdings(text, date(2025, 3, 31))
    by_symbol = {h.symbol: h for h in holdings}
    assert list(by_symbol) == ["_OPT_NBIS"]
    nbis = by_symbol["_OPT_NBIS"]
    assert nbis.quantity == Decimal("-8.0000")
    assert nbis.market_value_base == Decimal("-650.0000")
    assert nbis.cost_basis_base == Decimal("-4200.0000")
