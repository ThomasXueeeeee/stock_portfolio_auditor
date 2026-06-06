# SPDX-License-Identifier: MIT
"""IBKR Activity Statement CSV parser."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from stock_portfolio_auditor.domain.errors import ParserError, PeriodDetectionError
from stock_portfolio_auditor.domain.models import (
    CashBalance,
    CostBucket,
    Holding,
    IncomeBucket,
    Statement,
    Transaction,
)
from stock_portfolio_auditor.ingestion.csv_loader import group_sectioned_rows, read_csv_rows
from stock_portfolio_auditor.ingestion.period_detector import (
    StatementPeriod,
    detect_period_from_filename,
    detect_period_from_text,
)
from stock_portfolio_auditor.ingestion.pii import redact_text
from stock_portfolio_auditor.parsers.base import BrokerParser, register_parser
from stock_portfolio_auditor.parsers.detect import Broker, StatementFormat
from stock_portfolio_auditor.parsers.ibkr.common import (
    account_label,
    asset_kind_from_ibkr,
    cost_bucket_for_trade,
    income_bucket_for_interest,
    normalize_symbol,
    parse_decimal,
)


class IBKRCsvParser(BrokerParser):
    """Parse IBKR section-tagged Activity Statement CSV exports."""

    broker = Broker.IBKR
    supported_formats = frozenset({StatementFormat.CSV})
    parser_version = 1

    def parse(self, path: str | Path, *, account_label: str | None = None) -> Statement:
        """Parse an IBKR CSV statement."""
        csv_path = Path(path)
        raw_text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
        rows = read_csv_rows(csv_path)
        sections = group_sectioned_rows(rows)
        period = _period_from_sections(sections, csv_path)
        base_currency = _field_value(sections, "Account Information", "Base Currency") or "USD"
        beginning, ending = _nav_values(sections)

        return Statement(
            account_label=account_label_from_arg(csv_path, account_label),
            broker="ibkr",
            base_currency=base_currency,
            period_start=period.start,
            period_end=period.end,
            frequency=period.frequency,
            beginning_value_base=beginning,
            ending_value_base=ending,
            holdings=tuple(_holdings(sections, period)),
            transactions=tuple(_transactions(sections)),
            cash_balances=tuple(_cash_balances(sections)),
            per_symbol_pnl_base=_per_symbol_pnl(sections),
            per_symbol_pnl_includes_unrealized=True,
            parser_version=self.parser_version,
            raw_text_hash=hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest(),
            source_path=redact_text(str(csv_path)),
        )


def account_label_from_arg(path: Path, override: str | None) -> str:
    """Tiny wrapper to keep parser code readable."""
    return account_label(path, override)


def _field_value(sections: dict[str, list[list[str]]], section: str, field: str) -> str | None:
    for row in sections.get(section, []):
        if len(row) >= 4 and row[1] == "Data" and row[2].strip().lower() == field.lower():
            return row[3].strip()
    return None


def _period_from_sections(sections: dict[str, list[list[str]]], path: Path) -> StatementPeriod:
    period_value = _field_value(sections, "Statement", "Period")
    if period_value:
        try:
            return detect_period_from_text(f'Period,"{period_value}"', source=path)
        except PeriodDetectionError:
            pass
    fallback = detect_period_from_filename(path)
    if fallback is not None:
        return fallback
    raise ParserError("Could not detect IBKR statement period", {"path": str(path)})


def _nav_values(sections: dict[str, list[list[str]]]) -> tuple[Decimal, Decimal]:
    for row in sections.get("Net Asset Value", []):
        if len(row) >= 7 and row[1] == "Data" and row[2].strip().lower() == "total":
            return parse_decimal(row[3]), parse_decimal(row[6])
    starting = _change_in_nav_value(sections, "Starting Value")
    ending = _change_in_nav_value(sections, "Ending Value")
    return starting, ending


def _change_in_nav_value(sections: dict[str, list[list[str]]], field: str) -> Decimal:
    value = _field_value(sections, "Change in NAV", field)
    if value is None:
        raise ParserError("Missing IBKR Change in NAV value", {"field": field})
    return parse_decimal(value)


def _holdings(sections: dict[str, list[list[str]]], period: StatementPeriod) -> list[Holding]:
    """Build :class:`Holding` rows from the IBKR Open Positions section.

    The IBKR CSV stores identifiers (ISIN, listing exchange, full description)
    in a separate ``Financial Instrument Information`` section keyed by Symbol.
    We join the two so the resulting Holding rows carry the cross-broker
    identifiers needed by the security-master and Brinson attribution layers.

    IBKR's Open Positions section interleaves two kinds of Data rows: a
    ``Summary`` row per (symbol, currency) that aggregates all lots, and one
    ``Lot`` row per individual tax lot when the statement is exported with
    lot detail enabled. We must accept the ``Summary`` rows only (the older
    parser was permissive enough to accept Summary today because user
    statements happen to be lot-free, but enabling lot detail would otherwise
    silently duplicate each holding by 1 + N_lots).
    """
    instrument_info = _instrument_info(sections)
    holdings: list[Holding] = []
    for row in sections.get("Open Positions", []):
        if len(row) < 13 or row[1] != "Data":
            continue
        discriminator = row[2].strip().lower()
        # Accept the per-symbol Summary row and the legacy unlabeled row
        # (older IBKR exports leave column 3 blank for the only data row).
        if discriminator and discriminator != "summary":
            continue
        if not row[4].strip() or not row[5].strip():
            continue
        kind = asset_kind_from_ibkr(row[3])
        raw_symbol = row[5]
        symbol = normalize_symbol(raw_symbol)
        info = instrument_info.get(raw_symbol) or instrument_info.get(symbol) or {}
        holdings.append(
            Holding(
                symbol=symbol,
                description=info.get("description") or symbol,
                kind=kind,
                currency=row[4],
                quantity=parse_decimal(row[6]),
                market_value_local=parse_decimal(row[11]),
                # market_value_base / cost_basis_base are FX-translated post-parse
                # via stock_portfolio_auditor.ingestion.fx_reconstruction; the
                # IBKR CSV's Open Positions section only carries values in the
                # trading currency.
                market_value_base=parse_decimal(row[11]),
                cost_basis_local=parse_decimal(row[9]),
                cost_basis_base=parse_decimal(row[9]),
                as_of=period.end,
                isin=info.get("isin"),
                exchange=info.get("exchange"),
            )
        )
    return holdings


def _instrument_info(sections: dict[str, list[list[str]]]) -> dict[str, dict[str, str | None]]:
    """Parse the ``Financial Instrument Information`` section into a symbol map.

    IBKR row layout:
        ``Financial Instrument Information, Header, Asset Category, Symbol,
        Description, Conid, Security ID, Underlying, Listing Exch, Multiplier,
        Type, Code``
    """
    info: dict[str, dict[str, str | None]] = {}
    for row in sections.get("Financial Instrument Information", []):
        if len(row) < 9 or row[1] != "Data":
            continue
        symbol = row[3].strip()
        if not symbol:
            continue
        description = row[4].strip() or None
        security_id = row[6].strip() or None
        listing_exch = row[8].strip() or None
        info[symbol] = {
            "description": description,
            "isin": _looks_like_isin(security_id),
            "exchange": listing_exch,
        }
        normalized = normalize_symbol(symbol)
        if normalized != symbol and normalized not in info:
            info[normalized] = info[symbol]
    return info


def _per_symbol_pnl(sections: dict[str, list[list[str]]]) -> dict[str, Decimal]:
    """Sum per-symbol PnL from the Mark-to-Market Performance Summary section.

    IBKR row layout for the MTM section:

        ``Mark-to-Market Performance Summary, Header, Asset Category, Symbol,
        Prior Quantity, Current Quantity, Prior Price, Current Price,
        Mark-to-Market P/L Position, Mark-to-Market P/L Transaction,
        Mark-to-Market P/L Commissions, Mark-to-Market P/L Other,
        Mark-to-Market P/L Total, Code``

    The ``Mark-to-Market P/L Total`` column is denominated in the base
    currency (USD for accounts we audit), so the values are directly usable
    by downstream contribution analytics. Per-symbol rows for instruments
    that were rolled / renamed during the period (``2899.SUB`` etc.) are
    aggregated with the main ticker by stripping the ``.SUB``/``.PREO``
    suffix when present.

    Non-stock rows are routed to pseudo-symbol buckets so they stay in the
    audit-window aggregate total but do not pollute the stock-level Top
    Contributors / Top Detractors lists (``contribution._is_real_stock``
    filters underscore-prefixed symbols out):

      * ``Equity and Index Options`` rows -> ``_OPT_<UNDERLYING>``
        (one bucket per underlying ticker, so all options on a given
        underlying aggregate cleanly).
      * ``Forex`` rows -> ``_FX_<CCY>``.
    """
    output: dict[str, Decimal] = {}
    for row in sections.get("Mark-to-Market Performance Summary", []):
        if len(row) < 13 or row[1] != "Data":
            continue
        symbol_raw = row[3].strip()
        if not symbol_raw or row[2].strip().lower().startswith("total"):
            continue
        category = row[2].strip().lower()
        try:
            pnl = parse_decimal(row[12])
        except Exception:  # noqa: BLE001
            continue
        if "option" in category:
            underlying = symbol_raw.split(" ", 1)[0].upper()
            key = f"_OPT_{underlying}"
        elif "forex" in category:
            key = f"_FX_{symbol_raw.upper()}"
        else:
            key = normalize_symbol(_canonical_symbol(symbol_raw))
        output[key] = output.get(key, Decimal("0")) + pnl
    return output


def _canonical_symbol(raw: str) -> str:
    """Strip corporate-action suffixes so rights / pref offerings roll up."""
    suffixes = (".SUB", ".PREO", ".RTS", ".ESU")
    upper = raw.upper()
    for suffix in suffixes:
        if upper.endswith(suffix):
            return raw[: -len(suffix)]
    return raw


def _looks_like_isin(value: str | None) -> str | None:
    """ISIN format: 2-letter country code + 9 alphanumerics + 1 checksum = 12 chars.

    IBKR's ``Security ID`` field is overwhelmingly ISIN-formatted, but the
    column also occasionally carries internal codes (e.g. for corporate actions
    or sub-rights), so we apply a light format check.
    """
    if not value or len(value) != 12 or not value.isalnum():
        return None
    if not (value[0].isalpha() and value[1].isalpha()):
        return None
    return value.upper()


def _cash_balances(sections: dict[str, list[list[str]]]) -> list[CashBalance]:
    """Parse Forex Balances rows into per-currency CashBalance entries.

    IBKR Forex Balances row layout::

        Forex Balances, Data, Forex,
            Currency,        # column 3 -- always "USD" (the base): IBKR
                             #            reports every Forex Balances row's
                             #            monetary columns translated to USD
            Description,     # column 4 -- the foreign currency code
                             #            (``JPY``, ``HKD``, ``SGD``...)
            Quantity,        # column 5 -- balance in the foreign currency
            Cost Price,      # column 6 -- average cost rate (foreign per base)
            Cost Basis in USD,
            Close Price,
            Value in USD,    # column 9
            Unrealized P/L in USD,   # column 10 -- FX translation P&L
            Code

    The previous mapping read currency from column 3 (always "USD"), ending
    from column 4 (the string "JPY"), and fx_translation_pnl from column 11
    (the empty Code field) — so every row collapsed to a USD/0/0 CashBalance
    that conveyed nothing. Fix the column indices so per-currency FX P&L is
    actually surfaced.
    """
    balances: list[CashBalance] = []
    for row in sections.get("Forex Balances", []):
        if len(row) < 11 or row[1] != "Data":
            continue
        if row[2].strip().lower() == "total":
            continue
        currency_code = row[4].strip().upper()
        if len(currency_code) != 3 or not currency_code.isalpha():
            continue
        balances.append(
            CashBalance(
                currency=currency_code,
                starting=Decimal("0"),
                ending=parse_decimal(row[5]),
                fx_translation_pnl=parse_decimal(row[10]),
            )
        )
    return balances


def _transactions(sections: dict[str, list[list[str]]]) -> list[Transaction]:
    transactions: list[Transaction] = []
    transactions.extend(_trade_transactions(sections))
    transactions.extend(_deposit_withdrawal_transactions(sections))
    transactions.extend(_dividend_transactions(sections))
    transactions.extend(_interest_transactions(sections))
    transactions.extend(_withholding_tax_transactions(sections))
    transactions.extend(_transaction_fee_transactions(sections))
    return transactions


def _trade_transactions(sections: dict[str, list[list[str]]]) -> list[Transaction]:
    output: list[Transaction] = []
    for row in sections.get("Trades", []):
        if len(row) < 15 or row[1] != "Data":
            continue
        if not row[4].strip() or not row[5].strip() or not row[6].strip():
            continue
        kind = asset_kind_from_ibkr(row[3])
        quantity = parse_decimal(row[7])
        action = "buy" if quantity > 0 else "sell"
        realized_raw = row[13].strip() if len(row) > 13 else ""
        realized_pnl_base = parse_decimal(realized_raw) if realized_raw else None
        output.append(
            Transaction(
                trade_date=_parse_date(row[6]),
                action=action,
                symbol=normalize_symbol(row[5]),
                kind=kind,
                currency=row[4],
                quantity=quantity,
                price=parse_decimal(row[8]),
                amount_local=parse_decimal(row[10]),
                amount_base=parse_decimal(row[10]),
                commission=abs(parse_decimal(row[11])),
                realized_pnl_base=realized_pnl_base,
                cost_bucket=cost_bucket_for_trade(kind),
                source_section="Trades",
            )
        )
    return output


def _deposit_withdrawal_transactions(sections: dict[str, list[list[str]]]) -> list[Transaction]:
    output: list[Transaction] = []
    for row in sections.get("Deposits & Withdrawals", []):
        if len(row) < 6 or row[1] != "Data" or row[2].strip().lower() == "total":
            continue
        if not row[2].strip() or not row[3].strip():
            continue
        amount = parse_decimal(row[5])
        output.append(
            Transaction(
                trade_date=_parse_date(row[3]),
                action="contrib" if amount >= 0 else "wd",
                symbol=None,
                kind=asset_kind_from_ibkr("Cash"),
                currency=row[2],
                amount_local=amount,
                amount_base=amount,
                is_external_cash_flow=True,
                source_section="Deposits & Withdrawals",
                source_description=row[4],
            )
        )
    return output


def _dividend_transactions(sections: dict[str, list[list[str]]]) -> list[Transaction]:
    output: list[Transaction] = []
    for row in sections.get("Dividends", []):
        if len(row) < 5 or row[1] != "Data" or row[2].strip().lower() == "total":
            continue
        if not row[2].strip() or not row[3].strip():
            continue
        output.append(
            Transaction(
                trade_date=_parse_date(row[3]),
                action="div",
                symbol=_symbol_from_description(row[4]),
                kind=asset_kind_from_ibkr("Stock"),
                currency=row[2],
                amount_local=parse_decimal(row[5] if len(row) > 5 else row[4]),
                amount_base=parse_decimal(row[5] if len(row) > 5 else row[4]),
                income_bucket=IncomeBucket.CASH_DIVIDEND,
                source_section="Dividends",
                source_description=row[4],
            )
        )
    return output


def _interest_transactions(sections: dict[str, list[list[str]]]) -> list[Transaction]:
    output: list[Transaction] = []
    for row in sections.get("Interest", []):
        if len(row) < 5 or row[1] != "Data" or row[2].strip().lower() == "total":
            continue
        if not row[2].strip() or not row[3].strip():
            continue
        description = row[4]
        bucket = income_bucket_for_interest(description)
        output.append(
            Transaction(
                trade_date=_parse_date(row[3]),
                action="lend_int" if bucket is IncomeBucket.LENDING_INCOME else "int",
                symbol=_symbol_from_description(description),
                kind=asset_kind_from_ibkr("Cash"),
                currency=row[2],
                amount_local=parse_decimal(row[5] if len(row) > 5 else "0"),
                amount_base=parse_decimal(row[5] if len(row) > 5 else "0"),
                income_bucket=bucket,
                source_section="Interest",
                source_description=description,
            )
        )
    return output


def _withholding_tax_transactions(sections: dict[str, list[list[str]]]) -> list[Transaction]:
    output: list[Transaction] = []
    for row in sections.get("Withholding Tax", []):
        if len(row) < 5 or row[1] != "Data" or row[2].strip().lower() == "total":
            continue
        if not row[2].strip() or not row[3].strip():
            continue
        amount = abs(parse_decimal(row[5] if len(row) > 5 else row[4]))
        output.append(
            Transaction(
                trade_date=_parse_date(row[3]),
                action="tax",
                symbol=_symbol_from_description(row[4]),
                kind=asset_kind_from_ibkr("Stock"),
                currency=row[2],
                amount_local=-amount,
                amount_base=-amount,
                fees=amount,
                cost_bucket=CostBucket.WITHHOLDING_TAX,
                source_section="Withholding Tax",
                source_description=row[4],
            )
        )
    return output


def _transaction_fee_transactions(sections: dict[str, list[list[str]]]) -> list[Transaction]:
    output: list[Transaction] = []
    for row in sections.get("Transaction Fees", []):
        if len(row) < 10 or row[1] != "Data" or row[2].strip().lower() == "total":
            continue
        if not row[3].strip() or not row[5].strip() or not row[4].strip():
            continue
        kind = asset_kind_from_ibkr(row[2])
        amount = abs(parse_decimal(row[9]))
        output.append(
            Transaction(
                trade_date=_parse_date(row[4]),
                action="fee",
                symbol=normalize_symbol(row[5]),
                kind=kind,
                currency=row[3],
                quantity=parse_decimal(row[7]),
                price=parse_decimal(row[8]),
                amount_local=-amount,
                amount_base=-amount,
                fees=amount,
                cost_bucket=CostBucket.REGULATORY_FEE,
                source_section="Transaction Fees",
            )
        )
    return output


def _parse_date(value: str) -> date:
    text = value.split(",")[0].strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ParserError("Could not parse IBKR date", {"value": value})


_CURRENCY_TOKEN_RE = re.compile(r"^[A-Z]{3}$")


def _symbol_from_description(description: str) -> str | None:
    """Extract a stock symbol from an IBKR transaction description.

    Returns ``None`` when the first token is a 3-letter currency code rather
    than a real ticker — IBKR uses descriptions like
    ``"JPY Broker Interest Received"`` for cash-sweep interest, and we don't
    want a fake ``JPY`` position in the contribution panel.
    """
    token = description.split("(", 1)[0].strip().split(" ", 1)[0]
    if not token:
        return None
    if _CURRENCY_TOKEN_RE.match(token):
        return None
    return normalize_symbol(token)


register_parser(Broker.IBKR, StatementFormat.CSV, IBKRCsvParser)
