# SPDX-License-Identifier: MIT
"""Charles Schwab brokerage statement parser."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from loguru import logger

from stock_portfolio_auditor.domain.errors import ParserError, PeriodDetectionError
from stock_portfolio_auditor.domain.models import (
    AssetKind,
    CashBalance,
    CostBucket,
    Holding,
    IncomeBucket,
    Statement,
    Transaction,
)
from stock_portfolio_auditor.ingestion.pdf_loader import extract_text_fast, extract_text_layout
from stock_portfolio_auditor.ingestion.period_detector import (
    StatementPeriod,
    detect_period_from_filename,
    detect_period_from_text,
)
from stock_portfolio_auditor.ingestion.pii import account_label_from_path, redact_text
from stock_portfolio_auditor.parsers.base import BrokerParser, register_parser
from stock_portfolio_auditor.parsers.detect import Broker, StatementFormat

MONEY_RE = r"\$?\s*\(?-?[\d,]+\.\d{2}\)?"
BEGINNING_PATTERNS = [
    re.compile(rf"Starting\s+Value\s+({MONEY_RE})", re.IGNORECASE),
    re.compile(rf"Beginning\s+(?:Account|Portfolio)?\s*Value\s+({MONEY_RE})", re.IGNORECASE),
    re.compile(rf"Starting\s+(?:Account|Portfolio)?\s*Value\s+({MONEY_RE})", re.IGNORECASE),
    re.compile(rf"Opening\s+Balance\s+({MONEY_RE})", re.IGNORECASE),
]
ENDING_PATTERNS = [
    re.compile(
        rf"Ending\s+Value\s+on\s+\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\s+\$?\s*({MONEY_RE})", re.IGNORECASE
    ),
    re.compile(
        rf"Account\s+Value\s+as\s+of\s+\d{{1,2}}/\d{{1,2}}/\d{{2,4}}:\$?\s*({MONEY_RE})",
        re.IGNORECASE,
    ),
    re.compile(rf"Ending\s+(?:Account|Portfolio)?\s*Value\s+({MONEY_RE})", re.IGNORECASE),
    re.compile(rf"Closing\s+Balance\s+({MONEY_RE})", re.IGNORECASE),
]

# Holdings parsing -----------------------------------------------------------
#
# Schwab brokerage PDFs lay positions out in sections like ``Positions -
# Equities`` and ``Positions - Exchange Traded Funds``. Each section is
# rendered as a fixed-column table that pdfplumber extracts as one row per
# holding:
#
#   YOU CLEARSECUREINC(M) 500.0000 53.39000 26,695.00 15,885.80 10,809.20 1.12% 300.00
#
# Symbol, description, quantity, price, market value, cost basis,
# gain/(loss), yield, estimated annual income. Each section ends with a
# ``Total<SectionName>`` line. Schwab statements do not carry ISIN or
# listing-exchange data for US holdings, so those fields are left blank on the
# Holding row and resolved later by the security-master pipeline.
#
# Options have a multi-line row layout (strike + expiration are on separate
# lines) and are not yet captured here; v1 of the holdings parser focuses on
# Equities and ETFs which cover the bulk of the value in the statements we
# audit. Cash is captured from the existing Account Summary fields.

_HOLDING_SECTIONS: tuple[tuple[str, AssetKind, str], ...] = (
    ("Positions - Equities", AssetKind.EQUITY, "TotalEquities"),
    ("Positions - Exchange Traded Funds", AssetKind.ETF, "TotalExchangeTradedFunds"),
    ("Positions - Mutual Funds", AssetKind.MUTUAL_FUND, "TotalMutualFunds"),
)
_OPTIONS_SECTION_HEADER = "Positions - Options"
_OPTIONS_SECTION_TOTAL = "TotalOptions"

_HOLDING_ROW_RE = re.compile(
    r"^"
    r"(?P<symbol>[A-Z][A-Z0-9.\-]{0,6})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<quantity>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<price>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<market_value>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<cost_basis>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<gain_loss>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<yield_pct>N/A|[\d.]+%)\s+"
    r"(?P<annual_income>N/A|\(?-?[\d,]+(?:\.\d+)?\)?)"
    # IRA / Roth IRA statements append a "% of Account" column that does not
    # appear on the regular brokerage statement. Allow it as an optional
    # trailing field so the same regex parses both layouts.
    r"(?:\s+[\d.]+%)?"
    r"\s*$"
)
_NON_HOLDING_PREFIXES: tuple[str, ...] = (
    "Symbol",
    "Total",
    "Unrealized",
    "Positions",
    "Schwab",
    "Page",
    "AccountNumber",
    "OptionCustomers",
    "EstimatedAnnualIncome",
    "Option",
)
# Statement headers repeat the account-holder's name on each page (e.g.
# ``JANE DOE     March 1-31, 2025``). Detect those generically instead of
# baking a specific name into the prefix list -- a line of one or two all-
# uppercase tokens (3+ chars each) that is *not* one of our known section
# headers is treated as a name header and skipped.
_NAME_HEADER_RE = re.compile(r"^[A-Z]{3,}(?:\s+[A-Z]{2,})?\s*$")


class SchwabParser(BrokerParser):
    """Parse Schwab PDF brokerage statements."""

    broker = Broker.SCHWAB
    supported_formats = frozenset({StatementFormat.PDF})
    parser_version = 2

    def parse(self, path: str | Path, *, account_label: str | None = None) -> Statement:
        """Parse a Schwab PDF into a normalized Statement.

        Schwab publishes two distinct PDF document families for the same
        brokerage account:

          * ``Brokerage Statement`` -- monthly snapshot with holdings,
            transactions, and a Cash Transactions Summary.
          * ``1099 Composite and Year-End Summary`` -- annual tax document
            with per-lot realized gain/loss tables, dividends, and ADR /
            margin / foreign-tax breakdowns.

        Both flow through this parser; we peek at the document title before
        dispatching so each gets the layout it expects.
        """
        pdf_path = Path(path)
        raw_text = _extract_text(pdf_path)
        text = redact_text(raw_text)
        if _looks_like_1099_composite(text):
            return _parse_1099_composite(
                pdf_path,
                raw_text,
                text,
                account_label=account_label,
                parser_version=self.parser_version,
            )
        period = _detect_period(text, pdf_path)
        beginning = _extract_money(text, BEGINNING_PATTERNS)
        ending = _extract_money(text, ENDING_PATTERNS)
        # Holdings and the Transaction Details page are column-aligned in the
        # original PDF; the fast text extractor flattens them, so use the
        # layout-aware extractor for both even though it is slower.
        layout_text = redact_text(extract_text_layout(pdf_path).text)
        holdings = _extract_holdings(layout_text, period.end)
        per_symbol_realized = _extract_realized_pnl(layout_text)
        transactions = _extract_transactions(layout_text, period.start, period.end)

        label = account_label or account_label_from_path(pdf_path)
        raw_hash = hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest()
        cash_balance = _extract_cash_balance(layout_text)
        purchases_base, sales_base = _extract_stock_trading_totals(layout_text)

        return Statement(
            account_label=label,
            broker="schwab",
            base_currency="USD",
            period_start=period.start,
            period_end=period.end,
            frequency=period.frequency,
            beginning_value_base=beginning,
            ending_value_base=ending,
            holdings=holdings,
            transactions=transactions,
            cash_balances=(cash_balance,),
            per_symbol_pnl_base=per_symbol_realized,
            per_symbol_pnl_includes_unrealized=False,
            stock_purchases_base=purchases_base,
            stock_sales_base=sales_base,
            parser_version=self.parser_version,
            raw_text_hash=raw_hash,
            source_path=redact_text(str(pdf_path)),
        )


def _looks_like_1099_composite(text: str) -> bool:
    """Return True for the annual 1099 Composite / Year-End Summary."""
    return "Form 1099 Composite" in text or "1099 COMPOSITE" in text.upper()


def _detect_period(text: str, path: Path) -> StatementPeriod:
    try:
        return detect_period_from_text(text, source=path)
    except PeriodDetectionError:
        fallback = detect_period_from_filename(path)
        if fallback is not None:
            return fallback
        raise


def _extract_text(path: Path) -> str:
    """Use fast text first; fall back to pdfplumber layout extraction."""
    text = extract_text_fast(path)
    if len(text.strip()) >= 500:
        return text
    return extract_text_layout(path).text


def _extract_money(text: str, patterns: list[re.Pattern[str]]) -> Decimal:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return _parse_money(match.group(1))
    raise ParserError("Could not extract account value from Schwab statement")


def _parse_money(value: str) -> Decimal:
    stripped = value.replace("$", "").replace(",", "").strip()
    is_negative = stripped.startswith("(") and stripped.endswith(")")
    stripped = stripped.strip("()")
    result = Decimal(stripped)
    return -result if is_negative else result


_TXN_DATE_PREFIXES = tuple(f"{m:02d}/" for m in range(1, 13))


# The modern "Transactions - Summary" section on Schwab brokerage PDFs
# emits a header line containing the literal ``BeginningCash*asof`` and
# ``EndingCash`` tokens, followed by a single data row with eight money
# amounts in the same order as the header.
_SCHWAB_CASH_MONEY_RE = re.compile(r"\(?-?\$[\d,]+\.\d{2}\)?")
_SCHWAB_CASH_HEADER_TOKEN = "BeginningCash"
_SCHWAB_CASH_HEADER_TERMINATOR = "EndingCash"

# The pre-2024 layout uses a "Cash Transactions Summary" table with two
# columns ("This Period" and "Year to Date") and rows for Starting Cash,
# Deposits, Withdrawals, Investments Sold, Dividends and Interest,
# Withdrawals and other Debits, Investments Purchased, Fees and Charges,
# Total Cash Transaction Detail, Ending Cash. Each money cell ends with
# ``.xx`` and may be parenthesised for negatives.
_LEGACY_CASH_MONEY_RE = re.compile(r"\(?-?[\d,]+\.\d{2}\)?")


def _extract_cash_balance(text: str) -> CashBalance:
    """Extract starting / ending USD cash balance from the period summary.

    Tries the modern ``Transactions - Summary`` row first, then falls back
    to the legacy ``Cash Transactions Summary`` table. Returns a zero/zero
    balance when neither is found.
    """
    modern = _extract_cash_balance_modern(text)
    if modern is not None:
        return modern
    legacy = _extract_cash_balance_legacy(text)
    if legacy is not None:
        return legacy
    return CashBalance(currency="USD", starting=Decimal("0"), ending=Decimal("0"))


def _extract_cash_balance_modern(text: str) -> CashBalance | None:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if _SCHWAB_CASH_HEADER_TOKEN not in line:
            continue
        if _SCHWAB_CASH_HEADER_TERMINATOR not in line:
            continue
        for follow in lines[idx + 1 : idx + 4]:
            stripped = follow.strip()
            if not stripped:
                continue
            numbers = _SCHWAB_CASH_MONEY_RE.findall(stripped)
            if len(numbers) >= 8:
                try:
                    starting = _parse_money(numbers[0])
                    ending = _parse_money(numbers[7])
                except (InvalidOperation, ValueError):
                    return None
                return CashBalance(currency="USD", starting=starting, ending=ending)
            break
    return None


def _extract_cash_balance_legacy(text: str) -> CashBalance | None:
    starting = _legacy_cash_row_value(text, label="Starting Cash")
    ending = _legacy_cash_row_value(text, label="Ending Cash")
    if starting is None and ending is None:
        return None
    return CashBalance(
        currency="USD",
        starting=starting or Decimal("0"),
        ending=ending or Decimal("0"),
    )


def _legacy_cash_row_value(text: str, *, label: str) -> Decimal | None:
    """Read the *This Period* (first money column) from a Cash Transactions row.

    Schwab's legacy ``Cash Transactions Summary`` row layout is one of::

        Starting Cash* $7.63 $7.71
        Deposits 52,303.29 52,303.29
        Deposits and other Cash Credits 20,000.00 207,000.00
        Withdrawals and other Debits (20,000.00) (26,500.00)

    The row label may be followed by an asterisk and a description suffix
    ("and other Cash Credits", "and other Debits"), then two money values
    optionally prefixed with ``$``.
    """
    # Use alternation rather than ``\(?...\)?`` to guarantee paren pairing.
    # The trailing ``\b`` we used previously matched in the middle of
    # ``(20,000.00)`` and dropped the closing paren, which left
    # ``_parse_money`` to flip the sign of a clearly-negative withdrawal.
    pattern = re.compile(
        rf"^{re.escape(label)}\*?(?:\s+and other [A-Za-z ]+?)?"
        rf"\s+\$?\s*(?P<value>\(-?[\d,]+\.\d{{2}}\)|-?[\d,]+\.\d{{2}})"
    )
    for line in text.splitlines():
        stripped = line.strip()
        match = pattern.match(stripped)
        if match is not None:
            try:
                return _parse_money(match.group("value"))
            except (InvalidOperation, ValueError):
                return None
    return None


def _extract_external_cash_flow_total(
    text: str, period_start: date, period_end: date
) -> Transaction | None:
    """Return a single net external-flow transaction for the statement period.

    External cash flow on Schwab statements comes through several distinct
    line types — ``Deposit MoneyLink``, ``Withdrawal MoneyLink``, journaled
    funds, IRA rollover contributions, conversions, recharacterizations,
    and (in IRA accounts) bank-sweep transfers. Enumerating every variant
    in the per-row parser is error-prone, so we instead read the
    statement's own ``Deposits`` / ``Withdrawals`` totals (modern format)
    or ``Cash Transactions Summary`` table (legacy format) and emit one
    synthetic external-flow transaction per period stamped at the period
    end date. This guarantees the audit-window external cash flow matches
    the broker's own books even when novel transfer action names appear.
    """
    deposits, withdrawals = _extract_cash_flow_totals(text)
    net = deposits + withdrawals
    if net == 0:
        return None
    return Transaction(
        trade_date=period_end,
        action="contrib" if net > 0 else "wd",
        symbol=None,
        kind=AssetKind.CASH,
        currency="USD",
        amount_local=net,
        amount_base=net,
        is_external_cash_flow=True,
        source_section="Cash Transactions Summary",
        source_description=f"Net external flow: deposits {deposits}, withdrawals {withdrawals}",
    )


def _extract_cash_flow_totals(text: str) -> tuple[Decimal, Decimal]:
    """Return ``(deposits, withdrawals)`` for the period.

    Deposits are returned as a positive number; withdrawals as a negative
    number. The two sources are mutually exclusive — modern statements
    use the single-row ``Transactions - Summary`` layout, legacy
    statements use the multi-row ``Cash Transactions Summary`` table.
    """
    modern = _extract_cash_flow_totals_modern(text)
    if modern is not None:
        return modern
    legacy = _extract_cash_flow_totals_legacy(text)
    if legacy is not None:
        return legacy
    return Decimal("0"), Decimal("0")


def _extract_cash_flow_totals_modern(text: str) -> tuple[Decimal, Decimal] | None:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if _SCHWAB_CASH_HEADER_TOKEN not in line or _SCHWAB_CASH_HEADER_TERMINATOR not in line:
            continue
        for follow in lines[idx + 1 : idx + 4]:
            stripped = follow.strip()
            if not stripped:
                continue
            numbers = _SCHWAB_CASH_MONEY_RE.findall(stripped)
            if len(numbers) < 8:
                continue
            try:
                deposits = _parse_money(numbers[1])
                withdrawals = _parse_money(numbers[2])
            except (InvalidOperation, ValueError):
                return None
            return deposits, withdrawals
    return None


def _extract_cash_flow_totals_legacy(text: str) -> tuple[Decimal, Decimal] | None:
    deposits = _legacy_cash_row_value(text, label="Deposits")
    withdrawals = _legacy_cash_row_value(text, label="Withdrawals")
    if deposits is None and withdrawals is None:
        return None
    return deposits or Decimal("0"), withdrawals or Decimal("0")


def _extract_stock_trading_totals(text: str) -> tuple[Decimal, Decimal]:
    """Return ``(purchases, sales)`` stock-trading notionals for the period.

    Both numbers are absolute magnitudes (always >= 0). The modern
    ``Transactions - Summary`` and legacy ``Cash Transactions
    Summary`` both publish these totals on every Schwab monthly
    brokerage statement; the parser otherwise does not emit
    individual stock buy / sell ``Transaction`` rows (stock realized
    PnL flows through ``per_symbol_pnl_base`` instead), so this
    aggregate is the only path through which a monthly turnover
    figure can pick up Schwab's stock trading.

    Returns ``(Decimal('0'), Decimal('0'))`` when neither layout is
    detected so callers can blindly add the values without a None
    check.
    """
    modern = _extract_stock_trading_totals_modern(text)
    if modern is not None:
        return modern
    legacy = _extract_stock_trading_totals_legacy(text)
    if legacy is not None:
        return legacy
    return Decimal("0"), Decimal("0")


def _extract_stock_trading_totals_modern(text: str) -> tuple[Decimal, Decimal] | None:
    """Extract Purchases / Sales-Redemptions from the modern 8-column row.

    The single data row under the
    ``BeginningCash + Deposits + Withdrawals + Purchases +
    Sales/Redemptions + Dividends/Interest + Expenses = EndingCash``
    header carries eight money amounts in this column order::

        [0] BeginningCash
        [1] Deposits
        [2] Withdrawals
        [3] Purchases               <- stock purchases
        [4] Sales/Redemptions       <- stock sales
        [5] Dividends/Interest
        [6] Expenses
        [7] EndingCash

    A previous version of this function had columns 3 and 5 swapped,
    which silently counted the entire Dividends/Interest column as
    stock-purchase notional and inflated the turnover analytic by an
    order of magnitude. Tests pin the right mapping.
    """
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if _SCHWAB_CASH_HEADER_TOKEN not in line or _SCHWAB_CASH_HEADER_TERMINATOR not in line:
            continue
        for follow in lines[idx + 1 : idx + 4]:
            stripped = follow.strip()
            if not stripped:
                continue
            numbers = _SCHWAB_CASH_MONEY_RE.findall(stripped)
            if len(numbers) < 8:
                continue
            try:
                purchased = _parse_money(numbers[3])
                sold = _parse_money(numbers[4])
            except (InvalidOperation, ValueError):
                return None
            return abs(purchased), abs(sold)
    return None


def _extract_stock_trading_totals_legacy(text: str) -> tuple[Decimal, Decimal] | None:
    """Extract Investments Purchased / Sold from the legacy multi-row table.

    The legacy ``Cash Transactions Summary`` writes one labelled row per
    bucket: ``Investments Sold $X $Y`` and ``Investments Purchased
    ($X) ($Y)`` (Purchased is shown parenthesised because it's an
    outflow). The labels are stable across the legacy era; we read the
    *This Period* column only.
    """
    sold = _legacy_cash_row_value(text, label="Investments Sold")
    purchased = _legacy_cash_row_value(text, label="Investments Purchased")
    if sold is None and purchased is None:
        return None
    return abs(purchased or Decimal("0")), abs(sold or Decimal("0"))


# Each Schwab transaction line on the Transaction Details page looks like:
#     MM/DD <Category> <Action> <Symbol> <Description> <Qty> <Price> <Charges> <Amount> [Realized,(ST|LT)]
# Different categories occupy different subsets of the columns, so we use a
# tolerant regex anchored on the rightmost numeric value (the amount) and let
# downstream code interpret the category. See the docstring on
# ``_extract_transactions`` for the supported categories.
_SCHWAB_DIVIDEND_RE = re.compile(
    r"^"
    r"(?P<date>\d{2}/\d{2})\s+"
    r"Dividend\s+\S+\s+"
    r"(?P<symbol>[A-Z][A-Z0-9.\-]{0,6})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount>\(?-?[\d,]+(?:\.\d+)?\)?)\s*$"
)
_SCHWAB_INTEREST_RE = re.compile(
    r"^"
    r"(?P<date>\d{2}/\d{2})\s+"
    r"Interest\s+\S+\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount>\(?-?[\d,]+(?:\.\d+)?\)?)\s*$"
)
_SCHWAB_EXPENSE_RE = re.compile(
    r"^"
    r"(?P<date>\d{2}/\d{2})\s+"
    r"Expense\s+\S+\s+"
    r"(?P<symbol>[A-Z][A-Z0-9.\-]{0,6})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount>\(?-?[\d,]+(?:\.\d+)?\)?)\s*$"
)
# Schwab option Sale / Purchase rows are detectable by the action token
# carrying the option expiration glued onto the underlying ticker
# (``BN04/17/2025``). The Sale action may also be prefixed with an explicit
# ``ShortSale`` word (open-to-sell short). The Amount column is already net
# of the Charges (commission + industry fees) — we therefore emit the
# transaction without an explicit cost_bucket to avoid double-counting the
# Charges into the cost-drag bucket.
_SCHWAB_OPTION_TXN_RE = re.compile(
    r"^"
    # The leading date is optional: same-day sibling rows below the dated
    # row inherit the date prefix and omit it on subsequent ``Sale ...`` /
    # ``Purchase ...`` lines (e.g. the 02/12 PDD short open is followed by
    # a ``Sale DAL03/28/2025 ...`` row on the next line with no date).
    r"(?:(?P<date>\d{2}/\d{2})\s+)?"
    r"(?P<category>Sale|Purchase)\s+"
    r"(?:ShortSale\s+)?"
    r"(?P<underlying>[A-Z]+(?:\.[A-Z]+)?)"
    r"(?P<expiry>\d{2}/\d{2}/\d{4})"
    r"\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<quantity>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<price>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<charges>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<amount>\(?-?[\d,]+(?:\.\d+)?\)?)"
    # Optional trailing realized G/L column. Both signs occur: a profitable
    # close prints ``330.02,(ST)`` (positive, no parens); a losing close
    # prints ``(6,881.63),(ST)`` (parenthesised). The realized value here
    # is informational only -- the per-trade realized PnL feeds
    # ``per_symbol_pnl_base`` via ``_extract_realized_pnl`` separately.
    r"(?:\s*,?\s*\(?-?[\d,]+(?:\.\d+)?\)?\s*,\s*\(?(?:ST|LT)\)?)?"
    r"\s*$",
    re.IGNORECASE,
)


def _extract_transactions(
    text: str, period_start: date, period_end: date
) -> tuple[Transaction, ...]:
    """Emit ``Transaction`` rows for income / cost / external-flow categories.

    Schwab's "Transaction Details" page mixes several categories on the same
    page (Sale, Purchase, Dividend, Interest, Expense, Deposit, Withdrawal,
    Other). For per-position contribution analytics we need the rows that
    move PnL outside of holdings snapshots and outside of realized-gain
    columns:

      * ``Dividend Qual.Dividend SYMBOL ... amount`` -> CASH_DIVIDEND income
      * ``Interest CreditInterest ... amount``      -> INTEREST_CREDIT income (cash sweep, no symbol)
      * ``Expense ADRPassThru SYMBOL ... (amount)`` -> OTHER_COST attached to the symbol
      * ``Deposit / Withdrawal ... amount``          -> external cash flow
      * ``Sale [ShortSale] TICKERDDMMYYYY ...``      -> OPTION transaction (premium received)
      * ``Purchase TICKERDDMMYYYY ...``              -> OPTION transaction (premium paid)

    Stock Sale / Purchase rows are intentionally skipped here: realized
    PnL from stock dispositions is captured separately by
    :func:`_extract_realized_pnl`, and stock purchases don't move PnL
    (they only move cash into cost basis). Per-trade stock rows would
    add noise without new information for PnL attribution. The
    statement-level ``Investments Purchased`` / ``Investments Sold``
    aggregates from the Cash Transactions Summary still need to feed
    the turnover analytic, which reads them off
    ``Statement.stock_purchases_base`` / ``stock_sales_base`` --
    populated outside this function by
    :func:`_extract_stock_trading_totals`.

    Option Sale / Purchase rows ARE emitted as ``Transaction(kind=OPTION)``
    so the option premium cash flows feed the Options bucket of the dollar
    P&L decomposition chart instead of leaking into the Price residual; the
    Δunrealized side of those positions is captured separately via
    ``_extract_option_holdings`` snapshots.
    """
    output: list[Transaction] = []
    # Track the date prefix of the most recent dated row so an option
    # sibling row (``Sale TICKERDATE ...`` with no leading date prefix)
    # below a dated row can borrow it. Schwab prints multi-trade days
    # this way and only the first row carries the date.
    last_dated_prefix: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        starts_with_date = line.startswith(_TXN_DATE_PREFIXES)
        if starts_with_date:
            last_dated_prefix = line[:5]
            if " Dividend " in line[:25]:
                txn = _parse_dividend(line, period_start, period_end)
            elif " Interest " in line[:25]:
                txn = _parse_interest(line, period_start, period_end)
            elif " Expense " in line[:25]:
                txn = _parse_expense(line, period_start, period_end)
            elif " Sale " in line[:25] or " Purchase " in line[:25]:
                txn = _parse_option_transaction(line, period_start, period_end)
            else:
                continue
        elif (line.startswith("Sale ") or line.startswith("Purchase ")) and last_dated_prefix:
            # Sibling option trade on the same date as the previous dated
            # row -- prepend the inherited date so the regex anchors fire.
            txn = _parse_option_transaction(
                f"{last_dated_prefix} {line}", period_start, period_end
            )
        else:
            continue
        if txn is not None:
            output.append(txn)
    # Append a single synthetic external-flow transaction per statement
    # based on the broker's own Cash Transactions Summary totals.
    flow_txn = _extract_external_cash_flow_total(text, period_start, period_end)
    if flow_txn is not None:
        output.append(flow_txn)
    return tuple(output)


def _parse_option_transaction(
    line: str, period_start: date, period_end: date
) -> Transaction | None:
    """Parse a Schwab Sale / Purchase row that represents an option trade.

    Stock Sale / Purchase rows are deliberately handled elsewhere and must
    not match here, which is enforced by requiring the action token to
    contain a glued option expiry (``BN04/17/2025``). The Amount column on
    Schwab option rows is already net of the Charges (commission + industry
    fees), so we keep ``amount_base`` exactly as printed and skip setting a
    ``cost_bucket`` — otherwise the Charges would be double-counted into
    cost drag on top of being already netted out of Amount.
    """
    match = _SCHWAB_OPTION_TXN_RE.match(line)
    if match is None:
        return None
    try:
        amount = _parse_signed_number(match.group("amount"))
    except InvalidOperation:
        return None
    underlying = match.group("underlying").strip().upper()
    date_token = match.group("date") or period_end.strftime("%m/%d")
    return Transaction(
        trade_date=_schwab_date(date_token, period_start, period_end),
        action="sell" if match.group("category").lower() == "sale" else "buy",
        symbol=f"_OPT_{underlying}",
        kind=AssetKind.OPTION,
        currency="USD",
        amount_local=amount,
        amount_base=amount,
        source_section="Transaction Details",
        source_description=match.group("description").strip(),
    )


def _parse_dividend(line: str, period_start: date, period_end: date) -> Transaction | None:
    match = _SCHWAB_DIVIDEND_RE.match(line)
    if match is None:
        return None
    try:
        amount = _parse_signed_number(match.group("amount"))
    except InvalidOperation:
        return None
    return Transaction(
        trade_date=_schwab_date(match.group("date"), period_start, period_end),
        action="div",
        symbol=match.group("symbol").strip(),
        kind=AssetKind.EQUITY,
        currency="USD",
        amount_local=amount,
        amount_base=amount,
        income_bucket=IncomeBucket.CASH_DIVIDEND,
        source_section="Transaction Details",
        source_description=match.group("description").strip(),
    )


def _parse_interest(line: str, period_start: date, period_end: date) -> Transaction | None:
    match = _SCHWAB_INTEREST_RE.match(line)
    if match is None:
        return None
    try:
        amount = _parse_signed_number(match.group("amount"))
    except InvalidOperation:
        return None
    return Transaction(
        trade_date=_schwab_date(match.group("date"), period_start, period_end),
        action="int",
        symbol=None,
        kind=AssetKind.CASH,
        currency="USD",
        amount_local=amount,
        amount_base=amount,
        income_bucket=IncomeBucket.INTEREST_CREDIT,
        source_section="Transaction Details",
        source_description=match.group("description").strip(),
    )


def _parse_expense(line: str, period_start: date, period_end: date) -> Transaction | None:
    match = _SCHWAB_EXPENSE_RE.match(line)
    if match is None:
        return None
    try:
        amount = _parse_signed_number(match.group("amount"))
    except InvalidOperation:
        return None
    return Transaction(
        trade_date=_schwab_date(match.group("date"), period_start, period_end),
        action="fee",
        symbol=match.group("symbol").strip(),
        kind=AssetKind.EQUITY,
        currency="USD",
        amount_local=amount,
        amount_base=amount,
        fees=abs(amount),
        cost_bucket=CostBucket.OTHER_COST,
        source_section="Transaction Details",
        source_description=match.group("description").strip(),
    )


def _schwab_date(mmdd: str, period_start: date, period_end: date) -> date:
    """Convert a ``MM/DD`` token to a real date inside the statement period."""
    month, day = (int(part) for part in mmdd.split("/"))
    # Monthly statements straddle a single calendar month, but Schwab also
    # displays cash-sweep interest dated in the prior month (e.g. an April
    # statement may show "03/29 Interest ..."). Pick the year that keeps the
    # date inside the statement period; fall back to period_end's year.
    for candidate_year in (period_end.year, period_start.year):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue
        if period_start <= candidate <= period_end:
            return candidate
    return date(period_end.year, month, day)


# Two regexes split by asset class so each can use the right trailing
# anchor:
#
#   * Stock sales (no option expiry glued to the ticker) ALWAYS print a
#     realized column as the last money field, but Schwab dropped the
#     ``,(ST|LT)`` tag from this row family sometime in late 2025 / early
#     2026 (the Feb 2026 RothIRA statement reads ``20,424.96,`` and
#     ``33,105.92`` for FTAI sales — no tag, with or without a trailing
#     comma). The stock regex therefore accepts either form, and uses a
#     negative lookahead to skip ``Sale ShortSale`` (option-open short
#     premium, no realized).
#   * Option close rows (Sale/Purchase ``<UNDERLYING><EXPIRY>``) cannot
#     be distinguished from option-open rows by trailing-field count
#     alone, so we still REQUIRE the ``,(ST|LT)`` tag on those to avoid
#     misreading premium as realized. If Schwab drops the tag on options
#     too in a future release, this regex needs the same loosening, but
#     with a per-contract field-count check.
_REALIZED_STOCK_RE = re.compile(
    r"^"
    r"(?:(?P<date>\d{2}/\d{2})\s+)?"
    r"(?:Sale|Purchase)\s+(?!ShortSale\b)"
    r"(?P<symbol>[A-Z]+(?:\.[A-Z]+)?)(?![A-Z0-9])"
    r"\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<quantity>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<price>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<charges>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<amount>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<realized>\(?-?[\d,]+(?:\.\d+)?\)?)"
    r"(?:\s*,\s*\(?(?P<tenor>ST|LT)\)?)?"
    r"\s*,?\s*$",
    re.IGNORECASE,
)
_REALIZED_OPTION_RE = re.compile(
    r"^"
    r"(?P<date>\d{2}/\d{2})\s+"
    r"(?:Sale|Purchase)\s+"
    r"(?P<symbol>[A-Z]+(?:\.[A-Z]+)?)(?![A-Z])"
    r"(?P<expiry>\d{2}/\d{2}/\d{4})"
    r".*?"
    r"(?P<realized>\(?-?[\d,]+(?:\.\d+)?\)?)"
    r"\s*,\s*\(?(?P<tenor>ST|LT)\)?\s*$",
    re.IGNORECASE,
)


def _option_realized_key(underlying: str) -> str:
    """Synthetic per-underlying key for Schwab option realized P&L.

    Prefixing with an underscore keeps the pseudo-symbol out of the Top
    Contributors / Top Detractors lists (``contribution._is_real_stock``
    filters those out), while still letting the option realized P&L appear
    in the per-position aggregate total used for the Price-bucket
    reconciliation footnote.
    """
    return f"_OPT_{underlying.upper()}"

# Continuation rows for the same Sale/Purchase appear directly below a dated
# Sale/Purchase line when the disposition spans multiple tax lots. Schwab
# splits the realized column across the dated short-term lot row and a
# wrapped long-term lot row that carries no leading date (the description
# wraps onto that line instead), e.g.::
#
#     02/24 Sale PDD PDDHOLDINGSINC F (2,300.0000) 125.0000 8.37 287,491.63 32,784.25,(ST)
#                UNSPONSOREDADR                                              41,943.06,(LT)
#
# Without recognising the second line, the long-term portion of every
# two-lot disposition is dropped on the floor.
_REALIZED_CONTINUATION_RE = re.compile(
    r"(?P<realized>\(?-?[\d,]+(?:\.\d+)?\)?)"
    r"\s*,\s*\(?(?P<tenor>ST|LT)\)?\s*$",
    re.IGNORECASE,
)


def _extract_realized_pnl(text: str) -> dict[str, Decimal]:
    """Sum per-symbol realized Gain/(Loss) from Schwab's Transaction Details.

    Schwab brokerage PDFs include a per-page table on the "Transaction Details"
    page with the columns ``Date | Category | Action | Symbol | Description |
    Quantity | Price | Charges | Amount | Realized Gain/(Loss)``. The realized
    column is only populated for closing trades of stocks where Schwab tracks
    cost basis (the row ends with ``X,(ST)`` or ``X,(LT)``); option closes and
    purchases have no realized column and are skipped here.

    Multi-lot dispositions split the realized column across the dated row and
    a wrapped continuation that has no leading date — we attach those to the
    most recent dated Sale/Purchase symbol so both tax-lot portions count.

    Returns a dict keyed by symbol with the per-statement realized PnL in USD.
    Multi-currency Schwab statements would feed FX reconstruction later, but
    the brokerage statements we audit are all USD so no FX is needed here.
    """
    output: dict[str, Decimal] = {}
    # Fold legacy "Gain or (Loss) on Investments Sold" entries in first so
    # any same-symbol modern realized rows simply accumulate on top.
    for symbol, gain in _extract_legacy_realized_pnl(text).items():
        output[symbol] = output.get(symbol, Decimal("0")) + gain
    last_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        starts_with_date = line.startswith(_TXN_DATE_PREFIXES)
        starts_with_sale_or_purchase = (
            "Sale" in line[:25] or "Purchase" in line[:25]
        ) and "ShortSale" not in line[:25]
        ends_with_tenor = line.rstrip().endswith(",(ST)") or line.rstrip().endswith(",(LT)")

        if starts_with_date:
            # A new dated row resets the "current trade" context. If it's a
            # Sale or Purchase with a realized column we capture both the
            # attribution key and the first tax-lot realized amount; any
            # other dated row (Dividend, Interest, Deposit, Withdrawal,
            # Expense, Other) clears the context so a stray realized line
            # later doesn't get attributed to a previous unrelated trade.
            if not starts_with_sale_or_purchase:
                last_key = None
                continue
            parsed = _match_realized_row(line)
            if parsed is None:
                last_key = None
                continue
            key, realized = parsed
            output[key] = output.get(key, Decimal("0")) + realized
            last_key = key
        elif starts_with_sale_or_purchase:
            # New-format sibling sale (same security, same day, second lot)
            # printed as ``Sale <TICKER> ...`` without a leading date.
            parsed = _match_realized_row(line)
            if parsed is None:
                continue
            key, realized = parsed
            output[key] = output.get(key, Decimal("0")) + realized
            last_key = key
        elif ends_with_tenor and last_key is not None:
            # Wrapped continuation of an old-format multi-lot disposition
            # (``,(LT)`` line below the dated ``,(ST)`` line).
            match = _REALIZED_CONTINUATION_RE.search(line)
            if match is None:
                continue
            try:
                realized = _parse_signed_number(match.group("realized"))
            except InvalidOperation:
                continue
            output[last_key] = output.get(last_key, Decimal("0")) + realized
    return output


def _match_realized_row(line: str) -> tuple[str, Decimal] | None:
    """Try stock then option regex; return (attribution_key, realized) or None.

    Stock sales (no expiry glued to the ticker) use ``_REALIZED_STOCK_RE``
    which accepts both pre-2026 ``,(ST|LT)`` and post-2026 untagged rows.
    Option closes (with the expiry glued, e.g. ``BN04/17/2025``) still
    require an explicit ``,(ST|LT)`` tag because the trailing-field count
    alone can't distinguish a closing trade from an opening trade.
    """
    option_match = _REALIZED_OPTION_RE.match(line)
    if option_match is not None:
        try:
            realized = _parse_signed_number(option_match.group("realized"))
        except InvalidOperation:
            return None
        underlying = option_match.group("symbol").upper()
        return _option_realized_key(underlying), realized
    stock_match = _REALIZED_STOCK_RE.match(line)
    if stock_match is not None:
        try:
            realized = _parse_signed_number(stock_match.group("realized"))
        except InvalidOperation:
            return None
        return stock_match.group("symbol").upper(), realized
    return None


def _extract_holdings(text: str, period_end: date) -> tuple[Holding, ...]:
    """Walk each known holdings section and return parsed ``Holding`` rows."""
    holdings: list[Holding] = []
    seen_symbols: set[str] = set()
    for header, kind, total_marker in _HOLDING_SECTIONS:
        section_lines = _section_lines(text, header=header, end_marker=total_marker)
        for line in section_lines:
            holding = _parse_holding_row(line, kind, period_end)
            if holding is not None:
                holdings.append(holding)
                seen_symbols.add(holding.symbol)
    # Pre-2024 Schwab statements use a different layout: "Investment Detail -
    # Equities" / "...Exchange Traded Funds" / "...Mutual Funds" instead of
    # "Positions - ...", with per-lot subrows and a separate "Cost Basis"
    # total line. Parse those as a fallback so the early-period audit window
    # has its holdings tracked too.
    for symbol, kind, qty, mv, cb in _extract_legacy_investment_detail(text):
        if symbol in seen_symbols:
            continue
        holdings.append(
            Holding(
                symbol=symbol,
                description=symbol,
                kind=kind,
                currency="USD",
                quantity=qty,
                market_value_local=mv,
                market_value_base=mv,
                cost_basis_local=cb,
                cost_basis_base=cb,
                as_of=period_end,
            )
        )
        seen_symbols.add(symbol)
    holdings.extend(_extract_option_holdings(text, period_end))
    return tuple(holdings)


# Pre-2024 Schwab statements record realized PnL in a stand-alone
# ``Gain or (Loss) on Investments Sold`` section rather than appending
# ``,(ST|LT)`` to Sale rows on the Transaction Details page. Each row looks
# like::
#
#   INVSC QQQ TRUST SRS 1 ETF: QQQ 0.0118 11/01/21 10/14/22 3.12 4.56 (1.44)
#   JD COM INC FSPONSORED ADR 1 10.0000 06/03/22 10/18/22 446.79 561.37 (114.58)
#   ADR REPS: JD
#
# The symbol lives in a ``: TICKER`` suffix at the end of the description on
# the row itself OR on the next "ADR REPS: ..." continuation line. Two
# trailing date tokens followed by three money fields uniquely identify a
# realized-PnL row. The "Total Gain or (Loss) on Investments Sold" summary
# line ends the section.
_LEGACY_REALIZED_END_MARKER = "Total Gain or (Loss) on Investments Sold"
_LEGACY_REALIZED_SECTION_HEADER = "Gain or (Loss) on Investments Sold"
_LEGACY_REALIZED_ROW_RE = re.compile(
    r"^"
    r"(?P<description>.+?)"
    r"\s+(?P<qty>[\d,]+(?:\.\d+)?)"
    r"\s+(?P<opened>\d{2}/\d{2}/\d{2})"
    r"\s+(?P<closed>\d{2}/\d{2}/\d{2})"
    r"\s+(?P<proceeds>[\d,]+(?:\.\d+)?)"
    r"\s+(?P<cb>[\d,]+(?:\.\d+)?)"
    r"\s+(?P<gain>\(?-?[\d,]+(?:\.\d+)?\)?)"
    r"\s*$"
)
_LEGACY_INLINE_SYMBOL_RE = re.compile(r":\s+(?P<symbol>[A-Z][A-Z0-9.]{0,6})\b")
_LEGACY_CONTINUATION_SYMBOL_RE = re.compile(r":\s+(?P<symbol>[A-Z][A-Z0-9.]{0,6})\b")


def _extract_legacy_realized_pnl(text: str) -> dict[str, Decimal]:
    """Sum per-symbol realized PnL from the legacy realized table.

    Modern statements (post ~2024-Q1) put realized PnL inline on each Sale
    row with a ``,(ST|LT)`` tag; legacy statements collect them into a
    standalone ``Gain or (Loss) on Investments Sold`` section. Both layouts
    coexist in the same audit window for our user's records, so we sum the
    two sources independently into ``per_symbol_pnl_base``.
    """
    section = _legacy_realized_section(text)
    if not section:
        return {}
    output: dict[str, Decimal] = {}
    pending_gain: Decimal | None = None
    pending_symbol: str | None = None
    for raw_line in section:
        line = raw_line.strip()
        if not line:
            continue
        match = _LEGACY_REALIZED_ROW_RE.match(line)
        if match is not None:
            # Commit the previous row if we haven't yet resolved its symbol.
            if pending_gain is not None and pending_symbol is not None:
                output[pending_symbol] = output.get(pending_symbol, Decimal("0")) + pending_gain
            try:
                pending_gain = _parse_signed_number(match.group("gain"))
            except InvalidOperation:
                pending_gain = None
                pending_symbol = None
                continue
            symbol_match = _LEGACY_INLINE_SYMBOL_RE.search(match.group("description"))
            pending_symbol = (
                symbol_match.group("symbol").upper() if symbol_match is not None else None
            )
            if pending_symbol is not None:
                output[pending_symbol] = output.get(pending_symbol, Decimal("0")) + pending_gain
                pending_gain = None
                pending_symbol = None
            continue
        # Continuation row (e.g. "ADR REPS: JD") that carries the symbol
        # the prior data row was missing.
        if pending_gain is not None and pending_symbol is None:
            cont = _LEGACY_CONTINUATION_SYMBOL_RE.search(line)
            if cont is not None:
                symbol = cont.group("symbol").upper()
                output[symbol] = output.get(symbol, Decimal("0")) + pending_gain
                pending_gain = None
                pending_symbol = None
    return output


def _legacy_realized_section(text: str) -> list[str]:
    """Return the lines inside the legacy ``Gain or (Loss) on Investments Sold`` section.

    A bespoke walk avoids relying on ``_section_lines`` -- the start header
    is a substring of the end marker, and the section is page-broken with
    ``(continued)`` sub-headers that the standard helper would not handle
    cleanly.
    """
    output: list[str] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not in_section:
            if line.startswith(_LEGACY_REALIZED_SECTION_HEADER):
                in_section = True
            continue
        if line.startswith(_LEGACY_REALIZED_END_MARKER):
            break
        output.append(line)
    return output


_LEGACY_HOLDING_SECTIONS: tuple[tuple[str, AssetKind, str], ...] = (
    ("Investment Detail - Equities", AssetKind.EQUITY, "Total Equities"),
    (
        "Investment Detail - Exchange Traded Funds",
        AssetKind.ETF,
        "Total Exchange Traded Funds",
    ),
    ("Investment Detail - Mutual Funds", AssetKind.MUTUAL_FUND, "Total Mutual Funds"),
)

# Legacy header row carries the position-level summary: description (often
# spanning multiple subsequent lines), total quantity, market price, total
# market value, % of account, unrealized gain, yield, and annual income.
# We anchor on the trailing 5 fields so the variable description is captured
# without ambiguity.
_LEGACY_HOLDING_HEADER_RE = re.compile(
    r"^"
    r"(?P<description>.+?)\s+"
    r"(?P<quantity>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<price>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<mv>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<pct>[<>]?\d+(?:\.\d+)?%)\s+"
    r"(?P<gain>\(?-?[\d,]+(?:\.\d+)?\)?|N/A)\s+"
    r"(?P<yield_pct>N/A|[\d.]+%)\s+"
    r"(?P<annual_income>N/A|\(?-?[\d,]+(?:\.\d+)?\)?)"
    r"\s*$"
)
# Some intermediate-format statements (seen most clearly on Schwab's
# Jun-Oct 2024 brokerage PDFs) put the position's cost basis on the
# SYMBOL line itself rather than on a preceding sub-description line:
#
#     CONSOL ENERGY INC (M) 400.0000 102.03000 40,812.00 7% 5,102.05 N/A N/A
#     SYMBOL: CEIX 35,709.95
#
# An optional trailing money group lets the same regex parse both this
# variant and the bare ``SYMBOL: BRKB`` form used elsewhere.
_LEGACY_SYMBOL_RE = re.compile(
    r"^SYMBOL:\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,6})\b"
    # Optional inline cost basis. Matches only when the SYMBOL line
    # has nothing after the cost basis money column -- the original-
    # legacy variant prints lot-detail tokens after the symbol
    # (``SYMBOL: BRKB 1.0000 301.6700 ...``) which must NOT be
    # captured as a cost basis.
    r"(?:\s+(?P<inline_cb>\(?-?[\d,]+\.\d{2}\)?)\s*$)?"
)
_LEGACY_COST_BASIS_RE = re.compile(r"^Cost Basis\s+(?P<cb>\(?-?[\d,]+(?:\.\d+)?\)?)\s*$")
# Schwab's *intermediate* Investment Detail layout (seen on monthly
# statements roughly Aug 2023 through Dec 2024) drops the explicit
# ``Cost Basis NNN`` subtotal row and instead glues the cost basis
# onto the first sub-description line under each header, e.g.::
#
#     BERKSHIRE HATHAWAY (M) 218.0000 360.20000 78,523.60 16% 14,830.72 N/A N/A
#     CLASS B 63,692.88
#     SYMBOL: BRKB
#
# The sub-description starts with letters (share class, ADR style, etc.).
# The regex anchors on ``^[A-Z]`` plus a trailing money column with two
# decimal places. Original-legacy lot-detail rows like
# ``CLASS B 1.0000 301.6700 301.67 02/24/22 (6.58)`` would otherwise
# match this shape and incorrectly overwrite the cost basis with the
# trailing realized G/L of a single lot; we exclude them by requiring
# the line to NOT contain a date in MM/DD/YY or MM/DD/YYYY format,
# which lot-detail rows always carry and intermediate-format sub-
# descriptions never do.
_DATE_IN_LINE_RE = re.compile(r"\b\d{2}/\d{2}/\d{2,4}\b")
_INTERMEDIATE_SUBDESC_CB_RE = re.compile(
    r"^[A-Z][^$\n]*?\s+"
    r"(?P<cb>\(?-?[\d,]+\.\d{2}\)?)"
    r"\s*$"
)


def _extract_legacy_investment_detail(
    text: str,
) -> list[tuple[str, AssetKind, Decimal, Decimal, Decimal]]:
    """Yield ``(symbol, kind, quantity, market_value, cost_basis)`` tuples.

    Schwab's brokerage statements have used three subtly different layouts
    for the per-position detail section across the audit window:

    * **Original legacy** (pre-Aug 2023). The position spans many lines:

          BERKSHIRE HATHAWAY (cid:224) 14.0000 295.09000 4,131.26 ...
          CLASS B 1.0000 301.6700 301.67 02/24/22 (6.58)
          SYMBOL: BRKB 1.0000 301.6700 301.67 02/24/22 (6.58)
          ...per-lot rows...
          Cost Basis 4,164.53

      The cost basis lands on a dedicated trailing ``Cost Basis NNN`` row.

    * **Intermediate** (Aug 2023 - Dec 2024). Schwab compressed the lot
      detail away and glued the cost basis onto the first
      sub-description line under each header:

          BERKSHIRE HATHAWAY (M) 218.0000 360.20000 78,523.60 ...
          CLASS B 63,692.88
          SYMBOL: BRKB

      There is no explicit ``Cost Basis`` row. The first letter-prefixed
      sub-description line carries the per-position cost basis.

    * **Modern** (Jan 2025 onwards). Single-row ``Positions - Equities``
      table, parsed by :func:`_extract_holdings` directly without going
      through this function.

    This function handles the first two; the modern path is dispatched
    in :func:`_extract_holdings`. We walk the section line by line and
    commit a position as soon as we have all three of ``(qty, mv, cb,
    symbol)`` from either layout's mix of lines.
    """
    out: list[tuple[str, AssetKind, Decimal, Decimal, Decimal]] = []
    for header, kind, total_marker in _LEGACY_HOLDING_SECTIONS:
        section_lines = _section_lines(text, header=header, end_marker=total_marker)
        if not section_lines:
            continue
        pending_qty: Decimal | None = None
        pending_mv: Decimal | None = None
        pending_cb: Decimal | None = None
        pending_symbol: str | None = None

        def _maybe_commit() -> None:
            nonlocal pending_qty, pending_mv, pending_cb, pending_symbol
            if (
                pending_qty is not None
                and pending_mv is not None
                and pending_cb is not None
                and pending_symbol is not None
            ):
                out.append(
                    (
                        pending_symbol,
                        kind,
                        pending_qty,
                        pending_mv,
                        pending_cb,
                    )
                )
                pending_qty = pending_mv = pending_cb = None
                pending_symbol = None

        for raw_line in section_lines:
            line = raw_line.strip()
            if not line:
                continue
            # 1. Header row: opens a new position, resets the pending state.
            header_match = _LEGACY_HOLDING_HEADER_RE.match(line)
            if header_match is not None:
                try:
                    pending_qty = _parse_signed_number(header_match.group("quantity"))
                    pending_mv = _parse_signed_number(header_match.group("mv"))
                except InvalidOperation:
                    pending_qty = pending_mv = None
                pending_cb = None
                pending_symbol = None
                continue
            # 2. Symbol row -- may also carry an inline cost basis on
            # the Jun-Oct 2024 variant (``SYMBOL: CEIX 35,709.95``).
            symbol_match = _LEGACY_SYMBOL_RE.match(line)
            if symbol_match is not None:
                pending_symbol = symbol_match.group("symbol").upper()
                inline_cb_raw = symbol_match.group("inline_cb")
                if pending_cb is None and inline_cb_raw is not None:
                    try:
                        pending_cb = _parse_signed_number(inline_cb_raw)
                    except InvalidOperation:
                        pending_cb = None
                _maybe_commit()
                continue
            # 3. Explicit ``Cost Basis NNN`` subtotal row (original legacy).
            cb_match = _LEGACY_COST_BASIS_RE.match(line)
            if cb_match is not None:
                try:
                    pending_cb = _parse_signed_number(cb_match.group("cb"))
                except InvalidOperation:
                    pending_cb = None
                _maybe_commit()
                continue
            # 4. Intermediate-format sub-description with glued cost basis.
            # Only consider when we don't already have a cost basis pending
            # AND the line doesn't contain a date (which would identify it
            # as an original-legacy lot-detail row whose trailing money is
            # the lot's realized G/L, not the position's cost basis).
            if (
                pending_qty is not None
                and pending_mv is not None
                and pending_cb is None
                and _DATE_IN_LINE_RE.search(line) is None
            ):
                sub_match = _INTERMEDIATE_SUBDESC_CB_RE.match(line)
                if sub_match is not None:
                    try:
                        pending_cb = _parse_signed_number(sub_match.group("cb"))
                    except InvalidOperation:
                        pending_cb = None
                    _maybe_commit()
                    continue
    return out


# Schwab's "Positions - Options" section uses a multi-line row layout where
# only the first line carries the financial columns; expiration / strike /
# CALL-PUT tag wrap onto follow-on lines we don't need. The financial line
# has: symbol description quantity price MV cost-basis gain/(loss).
# Capture parentheses inside the value groups so ``_parse_signed_number`` can
# flip the sign for shorts (e.g. ``(10.0000)`` quantity, ``(150.00)`` MV).
_OPTION_ROW_RE = re.compile(
    r"^"
    r"(?P<symbol>[A-Z][A-Z0-9.\-]{0,6})\s+"
    r"(?P<description>(?:PUT|CALL)[A-Z0-9 .,\-]*?),?\s+"
    r"(?P<quantity>\(?-?[\d,]+(?:\.\d+)?\)?)S?\s+"
    r"(?P<price>-?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<mv>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<cb>\(?-?[\d,]+(?:\.\d+)?\)?)\s+"
    r"(?P<gain>\(?-?[\d,]+(?:\.\d+)?\)?)"
    r"\s*$"
)


def _extract_option_holdings(text: str, period_end: date) -> list[Holding]:
    """Aggregate Schwab option positions into one synthetic Holding per underlying.

    Schwab brokerage statements publish a per-contract ``Positions - Options``
    table that records the short/long quantity, current market value, cost
    basis (premium received for shorts, premium paid for longs, both shown
    parenthesized when negative) and the unrealized gain on each contract.
    Without parsing this table the open option positions are invisible to
    per-position attribution, and the entire short-premium effect leaks into
    the Price residual.

    Contracts are aggregated per underlying ticker into a synthetic
    ``_OPT_<UNDERLYING>`` Holding so the contribution panel can compute
    Δ(market_value − cost_basis) period-over-period without the panel being
    flooded with per-contract pseudo-symbols. The leading underscore keeps
    the pseudo-symbol out of the Top Contributors / Top Detractors stock
    rankings (``contribution._is_real_stock`` filter) while still counting in
    the aggregate Price-bucket reconciliation total.

    The synthetic Holding's quantity carries the *contract count* sign (a
    short put on BN shows ``quantity=-10``) but it is only informational —
    contribution attribution reads MV and CB.
    """
    rows = _section_lines(text, header=_OPTIONS_SECTION_HEADER, end_marker=_OPTIONS_SECTION_TOTAL)
    if not rows:
        return []

    @dataclass
    class _OptionAgg:
        quantity: Decimal = Decimal("0")
        mv: Decimal = Decimal("0")
        cb: Decimal = Decimal("0")

    agg: dict[str, _OptionAgg] = {}
    for line in rows:
        match = _OPTION_ROW_RE.match(line)
        if match is None:
            continue
        try:
            quantity = _parse_signed_number(match.group("quantity"))
            mv = _parse_signed_number(match.group("mv"))
            cb = _parse_signed_number(match.group("cb"))
        except InvalidOperation:
            logger.debug("Could not parse Schwab option row numbers", line=line)
            continue
        # The "(N.NNNN)S" quantity is parenthesised when short. The "S" suffix
        # also signals short but we rely on the parentheses to set the sign.
        underlying = match.group("symbol").strip().upper()
        key = f"_OPT_{underlying}"
        bucket = agg.setdefault(key, _OptionAgg())
        bucket.quantity += quantity
        bucket.mv += mv
        bucket.cb += cb

    holdings: list[Holding] = []
    for key, bucket in agg.items():
        underlying = key.removeprefix("_OPT_")
        holdings.append(
            Holding(
                symbol=key,
                description=f"Schwab options on {underlying}",
                kind=AssetKind.OPTION,
                currency="USD",
                quantity=bucket.quantity,
                market_value_local=bucket.mv,
                market_value_base=bucket.mv,
                cost_basis_local=bucket.cb,
                cost_basis_base=bucket.cb,
                as_of=period_end,
            )
        )
    return holdings


def _section_lines(text: str, *, header: str, end_marker: str) -> list[str]:
    """Return non-empty lines between a section header and its Total marker.

    Schwab's holdings sections always end with a single-line summary such as
    ``TotalEquities $862,308.81 ...``. We scan the document once per section
    and slice each section's content lines.
    """
    output: list[str] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not in_section:
            if line.lower().startswith(header.lower()):
                in_section = True
            continue
        if line.startswith(end_marker):
            break
        if any(line.startswith(prefix) for prefix in _NON_HOLDING_PREFIXES):
            continue
        if _NAME_HEADER_RE.match(line):
            continue
        output.append(line)
    return output


def _parse_holding_row(line: str, kind: AssetKind, period_end: date) -> Holding | None:
    """Apply the holdings row regex; ignore lines that don't match cleanly."""
    match = _HOLDING_ROW_RE.match(line)
    if match is None:
        return None
    try:
        quantity = _parse_signed_number(match.group("quantity"))
        price = _parse_signed_number(match.group("price"))
        market_value = _parse_signed_number(match.group("market_value"))
        cost_basis = _parse_signed_number(match.group("cost_basis"))
    except InvalidOperation:
        logger.debug("Could not parse Schwab holding row numbers", line=line)
        return None
    description = match.group("description").strip()
    if description.endswith(",") and len(description) > 1:
        description = description[:-1]
    if not description:
        return None
    # The cost-basis figure on Schwab statements is in the same currency as the
    # market value (USD for the brokerage statements we audit). When the row
    # represents a short option the values are negative which Holding accepts
    # via Decimal serialization.
    del price  # currently retained on the regex only; kept for forward use
    return Holding(
        symbol=match.group("symbol").strip(),
        description=description,
        kind=kind,
        currency="USD",
        quantity=quantity,
        market_value_local=market_value,
        market_value_base=market_value,
        cost_basis_local=cost_basis,
        cost_basis_base=cost_basis,
        as_of=period_end,
    )


def _parse_signed_number(value: str) -> Decimal:
    """Parse a Schwab numeric field that may use parentheses for negatives."""
    stripped = value.replace(",", "").strip()
    negative = stripped.startswith("(") and stripped.endswith(")")
    stripped = stripped.strip("()")
    result = Decimal(stripped)
    return -result if negative else result


# ---------------------------------------------------------------------------
# 1099 Composite / Year-End Summary parsing
# ---------------------------------------------------------------------------
#
# Pre-2025 schwab_individual brokerage statements collapse stock realized
# PnL into a single ``Investments Sold $X`` aggregate row with no per-symbol
# detail, so the monthly-statement realized parser is blind to which ticker
# generated which gain or loss. The annual Form 1099 Composite is the only
# Schwab document that publishes per-lot realized data for the brokerage
# account; ingesting it closes the per-position attribution gap for the
# legacy era.
#
# Each disposition spans two lines in the layout text:
#
#     235 ALPHABET INC. CLASS S VARIOUS $ 20,627.83 $ 26,841.83 -- $ (6,214.00)$ 0.00
#     02079K305 / GOOGL 01/10/23 --
#     Security Subtotal $ 20,627.83 $ 26,841.83 -- $ (6,214.00) $ 0.00
#
# Stock dispositions carry the symbol on the second line in
# ``<CUSIP> / <SYMBOL>`` format; option dispositions instead emit
# ``<UNDERLYING> <expiry> <strike> <C|P> <date_sold>``. We track the most
# recent observed symbol while walking the section and attribute the
# realized amount on each ``Security Subtotal`` line to it. Options are
# routed to ``_OPT_<UNDERLYING>`` to match the monthly-parser convention.

_COMPOSITE_PERIOD_RE = re.compile(r"TAX YEAR\s+(?P<year>\d{4})")
_SUBTOTAL_RE = re.compile(
    r"^Security Subtotal\s+"
    r"\$\s*\(?-?[\d,]+\.\d{2}\)?\s+"
    r"\$\s*\(?-?[\d,]+\.\d{2}\)?\s+"
    r"(?:--|\$?\s*\(?-?[\d,]+\.\d{2}\)?)\s+"
    r"\$\s*(?P<realized>\(?-?[\d,]+\.\d{2}\)?)"
)
# A per-disposition row inside the 1099-B section. Shape (stock):
#
#   235 ALPHABET INC. CLASS S VARIOUS $ 20,627.83 $ 26,841.83 -- $ (6,214.00)$ 0.00
#
# and (option):
#
#   23S CALL PDD HOLDINGS INC $110 E X 11/27/24 $ 766.72 $ 0.00 -- $ 766.72$ 0.00
#
# We anchor on the trailing money columns (proceeds, cost basis, wash-sale
# adj, realized, accrued/withheld) so this regex matches both layouts.
_DISPOSITION_ROW_RE = re.compile(
    r"^\s*\d+\S?\s+.+?\s+"  # count, description (greedy non-newline)
    r"\$\s*(?P<proceeds>\(?-?[\d,]+\.\d{2}\)?)\s+"
    r"\$\s*(?P<cost>\(?-?[\d,]+\.\d{2}\)?)\s+"
    r"(?:--|\$\s*\(?-?[\d,]+\.\d{2}\)?)\s+"  # wash-sale adj
    r"\$\s*(?P<realized>\(?-?[\d,]+\.\d{2}\)?)"
    r"\s*\$?\s*\(?-?[\d,]*\.?\d{0,2}\)?\s*$"  # optional accrued / withheld
)
# The PII redactor masks 8-12-digit standalone numbers as ``[REDACTED]``,
# which catches CUSIPs (9 digits). We accept either the raw CUSIP or the
# redaction placeholder so the symbol on the same row is captured either
# way. We also capture the sold date that follows the symbol on the same
# line so per-disposition Transactions can carry the broker's trade_date.
_CUSIP_SYMBOL_RE = re.compile(
    r"^(?:\[REDACTED\]|[A-Z0-9]{8,9})\s*/\s*(?P<symbol>[A-Z][A-Z0-9.\-/]{0,8})\b"
    r"(?:\s+(?P<sold>\d{2}/\d{2}/\d{2,4}))?"
)
_OPTION_DETAIL_RE = re.compile(
    r"^(?P<underlying>[A-Z][A-Z0-9.\-]{0,6})\s+"
    r"\d{2}/\d{2}/\d{4}\s+"
    r"\d+(?:\.\d+)?\s+"
    r"(?:C|P)\b"
    r"(?:\s+(?P<sold>\d{2}/\d{2}/\d{2,4}))?"
)


def _parse_1099_composite(
    pdf_path: Path,
    raw_text: str,
    text: str,
    *,
    account_label: str | None,
    parser_version: int,
) -> Statement:
    """Parse a Schwab Form 1099 Composite / Year-End Summary PDF.

    The 1099-B section is column-aligned in the original PDF; the fast
    text extractor flattens columns into a single space-separated stream
    which is sufficient for our use because we anchor on the ``Security
    Subtotal`` line shape and ignore the per-disposition rows.

    NAV is left at 0/0 -- the broker doesn't publish a NAV on this
    document, and the monthly statements already provide it. The audit
    pipeline post-processes 1099-composite Statements to override the
    same-account monthly per_symbol_pnl_base for the covered year, so the
    1099-B is the authoritative source for per-symbol realized PnL
    whenever it's present.
    """
    layout_text = redact_text(extract_text_layout(pdf_path).text)
    year_match = _COMPOSITE_PERIOD_RE.search(text) or _COMPOSITE_PERIOD_RE.search(layout_text)
    if year_match is None:
        raise ParserError("Could not detect tax year on Schwab 1099 Composite")
    year = int(year_match.group("year"))
    per_symbol = _extract_1099_composite_realized(layout_text)
    transactions = tuple(
        _extract_1099_composite_dispositions(layout_text, default_year=year)
    )

    label = account_label or account_label_from_path(pdf_path)
    raw_hash = hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest()
    return Statement(
        account_label=label,
        broker="schwab",
        base_currency="USD",
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        frequency="A",
        beginning_value_base=Decimal("0"),
        ending_value_base=Decimal("0"),
        holdings=(),
        transactions=transactions,
        cash_balances=(),
        per_symbol_pnl_base=per_symbol,
        per_symbol_pnl_includes_unrealized=False,
        parser_version=parser_version,
        raw_text_hash=raw_hash,
        source_path=redact_text(str(pdf_path)),
    )


def _extract_1099_composite_realized(text: str) -> dict[str, Decimal]:
    """Walk the 1099-B section and sum realized PnL per symbol / option underlying."""
    output: dict[str, Decimal] = {}
    last_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # CUSIP / SYMBOL line for a stock disposition.
        cusip_match = _CUSIP_SYMBOL_RE.match(line)
        if cusip_match is not None:
            last_key = _normalize_1099_symbol(cusip_match.group("symbol"))
            continue
        # Underlying / expiry / strike / C-P line for an option.
        option_match = _OPTION_DETAIL_RE.match(line)
        if option_match is not None:
            underlying = option_match.group("underlying").upper()
            last_key = f"_OPT_{underlying}"
            continue
        subtotal_match = _SUBTOTAL_RE.match(line)
        if subtotal_match is None or last_key is None:
            continue
        try:
            realized = _parse_money(subtotal_match.group("realized"))
        except (InvalidOperation, ValueError):
            continue
        output[last_key] = output.get(last_key, Decimal("0")) + realized
        last_key = None
    return output


def _normalize_1099_symbol(raw: str) -> str:
    """Schwab 1099 forms write Class B shares as ``BRK/B``; the rest of the
    codebase uses ``BRK.B``."""
    return raw.replace("/", ".").upper()


def _extract_1099_composite_dispositions(
    text: str, *, default_year: int
) -> list[Transaction]:
    """Emit one ``Transaction`` per disposition row in the 1099-B section.

    Each disposition spans two lines in the layout text:

        235 ALPHABET INC. CLASS S VARIOUS $ 20,627.83 $ 26,841.83 -- $ (6,214.00)$ 0.00
        02079K305 / GOOGL 01/10/23 --

    The first line carries the disposition's proceeds, cost basis, and
    realized PnL. The second line carries the symbol (stock) or
    underlying contract details (option) plus the sold date.

    The Statement that wraps these Transactions still publishes the same
    per-symbol aggregate via ``per_symbol_pnl_base`` for backward
    compatibility, but downstream audit-window clipping rebuilds that
    aggregate from these per-disposition rows when only a subset of the
    tax year falls inside the requested audit window (e.g. Oct-Dec 2022
    out of a full 2022 1099 Composite).

    ``default_year`` is the 1099's tax year, used as a fallback when a
    disposition's sold date is missing (rare but seen on some malformed
    PDFs).
    """
    output: list[Transaction] = []
    pending: tuple[Decimal, Decimal] | None = None  # (realized, proceeds)
    lines = text.splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Security Subtotal"):
            pending = None
            continue
        disp_match = _DISPOSITION_ROW_RE.match(line)
        if disp_match is not None:
            try:
                realized = _parse_money(disp_match.group("realized"))
                proceeds = _parse_money(disp_match.group("proceeds"))
            except (InvalidOperation, ValueError):
                pending = None
                continue
            pending = (realized, proceeds)
            continue
        if pending is None:
            continue
        cusip_match = _CUSIP_SYMBOL_RE.match(line)
        if cusip_match is not None:
            symbol = _normalize_1099_symbol(cusip_match.group("symbol"))
            sold_raw = cusip_match.group("sold")
            sold = _parse_1099_date(sold_raw, default_year=default_year)
            realized, proceeds = pending
            output.append(
                Transaction(
                    trade_date=sold,
                    action="sell",
                    symbol=symbol,
                    kind=AssetKind.EQUITY,
                    currency="USD",
                    amount_local=proceeds,
                    amount_base=proceeds,
                    realized_pnl_base=realized,
                    source_section="1099-B",
                )
            )
            pending = None
            continue
        option_match = _OPTION_DETAIL_RE.match(line)
        if option_match is not None:
            underlying = option_match.group("underlying").upper()
            sold_raw = option_match.group("sold")
            sold = _parse_1099_date(sold_raw, default_year=default_year)
            realized, proceeds = pending
            output.append(
                Transaction(
                    trade_date=sold,
                    action="sell",
                    symbol=f"_OPT_{underlying}",
                    kind=AssetKind.OPTION,
                    currency="USD",
                    amount_local=proceeds,
                    amount_base=proceeds,
                    realized_pnl_base=realized,
                    source_section="1099-B",
                )
            )
            pending = None
            continue
    return output


def _parse_1099_date(raw: str | None, *, default_year: int) -> date:
    """Parse a Schwab 1099 ``MM/DD/YY`` (or ``MM/DD/YYYY``) sold-date.

    Schwab consistently emits two-digit years on 1099 disposition rows;
    the tax year is the document-level fallback for the rare cases where
    a disposition row has the sold-date column missing (we then assume
    Dec 31 of the tax year so the disposition still falls inside the
    1099's covered period for audit-window clipping).
    """
    if not raw:
        return date(default_year, 12, 31)
    parts = raw.split("/")
    month, day, year_raw = parts[0], parts[1], parts[2]
    if len(year_raw) == 2:
        # Schwab uses two-digit years; map 00..49 to 20YY and 50..99 to 19YY.
        year_int = int(year_raw)
        year = 2000 + year_int if year_int <= 49 else 1900 + year_int
    else:
        year = int(year_raw)
    return date(year, int(month), int(day))


register_parser(Broker.SCHWAB, StatementFormat.PDF, SchwabParser)
