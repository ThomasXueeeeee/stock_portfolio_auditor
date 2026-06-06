# SPDX-License-Identifier: MIT
"""Core domain models shared by parsers, analytics, and reporting."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

MONEY_QUANT = Decimal("0.0001")


def quantize_decimal(value: Decimal | int | float | str | None) -> Decimal | None:
    """Convert numeric input to audit-friendly Decimal precision."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


class SPARecord(BaseModel):
    """Base model with stable JSON serialization for Decimal values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_serializer("*", when_used="json")
    def serialize_decimal(self, value: object) -> object:
        """Serialize Decimal as string to avoid floating-point loss."""
        if isinstance(value, Decimal):
            return str(value)
        return value


class AssetKind(StrEnum):
    """Normalized asset buckets used across brokers."""

    EQUITY = "equity"
    OPTION = "option"
    ETF = "etf"
    MUTUAL_FUND = "mf"
    CASH = "cash"
    FIXED_INCOME = "fi"
    FX = "fx"
    OTHER = "other"


class CostBucket(StrEnum):
    """Negative drag buckets shown in the Cost & Tax Analysis report section."""

    STOCK_COMMISSION = "stock_commission"
    OPTION_COMMISSION = "option_commission"
    REGULATORY_FEE = "regulatory_fee"
    EXCHANGE_FEE = "exchange_fee"
    FX_CONVERSION_FEE = "fx_conversion_fee"
    MARGIN_INTEREST = "margin_interest"
    SHORT_BORROW_FEE = "short_borrow_fee"
    ACCOUNT_FEE = "account_fee"
    WITHHOLDING_TAX = "withholding_tax"
    OTHER_COST = "other_cost"


class IncomeBucket(StrEnum):
    """Positive income buckets separated from costs."""

    CASH_DIVIDEND = "cash_dividend"
    INTEREST_CREDIT = "interest_credit"
    LENDING_INCOME = "lending_income"
    OTHER_INCOME = "other_income"


class OptionType(StrEnum):
    """Option contract side."""

    CALL = "call"
    PUT = "put"


class OptionMeta(SPARecord):
    """Normalized option contract metadata."""

    underlying: str
    expiration: date
    strike: Decimal
    option_type: OptionType

    _quantize_strike = field_validator("strike", mode="before")(quantize_decimal)


class Holding(SPARecord):
    """A position at a statement date.

    ``symbol`` is the broker-native identifier (Schwab/Fidelity ticker, IBKR
    symbol). ``isin`` / ``figi`` are populated when the source statement
    carries them (e.g. IBKR CSV) and are used to reconcile the same security
    across brokers. ``classification_source`` records which security-master
    tier (sgx / hkex / edgar / yfinance / openfigi / manual) supplied the
    sector and country tags on the joined :class:`Security` row.
    """

    symbol: str
    description: str = ""
    kind: AssetKind
    currency: str = Field(min_length=3, max_length=3)
    quantity: Decimal
    market_value_local: Decimal
    market_value_base: Decimal
    cost_basis_local: Decimal | None = None
    cost_basis_base: Decimal | None = None
    as_of: date
    isin: str | None = None
    figi: str | None = None
    exchange: str | None = None

    _quantize_quantity = field_validator("quantity", mode="before")(quantize_decimal)
    _quantize_market_value_local = field_validator("market_value_local", mode="before")(
        quantize_decimal
    )
    _quantize_market_value_base = field_validator("market_value_base", mode="before")(
        quantize_decimal
    )
    _quantize_cost_basis_local = field_validator("cost_basis_local", mode="before")(
        quantize_decimal
    )
    _quantize_cost_basis_base = field_validator("cost_basis_base", mode="before")(quantize_decimal)

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        """Normalize ISO-style currency codes."""
        return value.upper()


class Lot(SPARecord):
    """A tax lot inside a holding.

    Populated when the broker statement carries lot detail (IBKR yes, Schwab
    partially, Fidelity blanks for retirement accounts, Robinhood no). Wash-sale
    rules are not modeled here; the lot is only retained for FIFO/HIFO cost-basis
    tracking and short/long-term realized-gain bucketing.
    """

    symbol: str
    acquired_date: date
    shares: Decimal
    cost_basis_local: Decimal
    currency: str = Field(min_length=3, max_length=3)

    _quantize_shares = field_validator("shares", mode="before")(quantize_decimal)
    _quantize_cost_basis_local = field_validator("cost_basis_local", mode="before")(
        quantize_decimal
    )

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        """Normalize ISO-style currency codes."""
        return value.upper()


class Security(SPARecord):
    """Security-master row used for attribution.

    Joined to :class:`Holding` rows by ``figi`` (preferred) or ``isin``, falling
    back to ``ticker`` for US listings where the security master may not carry
    an ISIN. ``classification_source`` records the highest-tier source that
    supplied the sector/country tags (sgx / hkex / edgar / yfinance / openfigi /
    manual). The Yahoo sector vocabulary differs from GICS, so we keep both
    ``sector_yahoo`` and ``sector_gics`` — the GICS field is derived via a
    crosswalk shipped in :mod:`stock_portfolio_auditor.data.security_master`.
    """

    ticker: str
    isin: str | None = None
    figi: str | None = None
    exchange: str | None = None
    base_currency: str | None = Field(default=None, min_length=3, max_length=3)
    sector_yahoo: str | None = None
    sector_gics: str | None = None
    industry: str | None = None
    country: str | None = None
    country_of_risk: str | None = None
    classification_source: str | None = None
    last_refreshed: date | None = None

    @field_validator("base_currency")
    @classmethod
    def uppercase_base_currency(cls, value: str | None) -> str | None:
        """Normalize ISO-style currency codes."""
        return value.upper() if value else value


TransactionAction = Literal[
    "buy",
    "sell",
    "div",
    "int",
    "fee",
    "tax",
    "contrib",
    "wd",
    "assign",
    "expire",
    "journal",
    "reinvest",
    "fx_conv",
    "margin_int",
    "lend_int",
    "split",
]


class Transaction(SPARecord):
    """A normalized transaction row from a broker statement.

    ``realized_pnl_base`` carries the per-disposition realized PnL in base
    currency when the source row publishes one (Schwab 1099 Composite
    disposition rows, IBKR Trades rows with a Realized P/L column). It is
    deliberately a per-row field rather than a per-statement aggregate
    because the audit-window filter needs to rebuild the per-symbol
    realized totals on partial-overlap statements by summing only the
    in-window dispositions -- the full-period aggregate published on a
    1099 or YTD CSV cannot be split temporally any other way.
    """

    trade_date: date
    settle_date: date | None = None
    action: TransactionAction
    symbol: str | None = None
    kind: AssetKind
    currency: str = Field(min_length=3, max_length=3)
    quantity: Decimal = Decimal("0")
    price: Decimal | None = None
    amount_local: Decimal = Decimal("0")
    amount_base: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    realized_pnl_base: Decimal | None = None
    cost_bucket: CostBucket | None = None
    income_bucket: IncomeBucket | None = None
    is_external_cash_flow: bool = False
    option_meta: OptionMeta | None = None
    source_section: str | None = None
    source_description: str | None = None

    _quantize_quantity = field_validator("quantity", mode="before")(quantize_decimal)
    _quantize_price = field_validator("price", mode="before")(quantize_decimal)
    _quantize_amount_local = field_validator("amount_local", mode="before")(quantize_decimal)
    _quantize_amount_base = field_validator("amount_base", mode="before")(quantize_decimal)
    _quantize_commission = field_validator("commission", mode="before")(quantize_decimal)
    _quantize_fees = field_validator("fees", mode="before")(quantize_decimal)
    _quantize_realized_pnl_base = field_validator("realized_pnl_base", mode="before")(
        quantize_decimal
    )

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        """Normalize ISO-style currency codes."""
        return value.upper()


class CashBalance(SPARecord):
    """Per-currency cash balance for a statement period."""

    currency: str = Field(min_length=3, max_length=3)
    starting: Decimal
    ending: Decimal
    fx_translation_pnl: Decimal | None = None

    _quantize_starting = field_validator("starting", mode="before")(quantize_decimal)
    _quantize_ending = field_validator("ending", mode="before")(quantize_decimal)
    _quantize_fx_translation_pnl = field_validator("fx_translation_pnl", mode="before")(
        quantize_decimal
    )

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        """Normalize ISO-style currency codes."""
        return value.upper()


class Statement(SPARecord):
    """Normalized broker statement model."""

    account_label: str
    broker: Literal["schwab", "ibkr", "fidelity", "robinhood"]
    base_currency: str = Field(min_length=3, max_length=3)
    period_start: date
    period_end: date
    frequency: Literal["M", "Q", "A"]
    beginning_value_base: Decimal
    ending_value_base: Decimal
    holdings: tuple[Holding, ...] = ()
    transactions: tuple[Transaction, ...] = ()
    cash_balances: tuple[CashBalance, ...] = ()
    # Per-symbol PnL contribution over the statement period, denominated in
    # ``base_currency``. Two sources publish this:
    #
    #   * IBKR's ``Mark-to-Market Performance Summary`` — total stock PnL
    #     (realized + unrealized + commissions). For this source, set
    #     ``per_symbol_pnl_includes_unrealized=True`` so analytics doesn't
    #     double-count by adding holdings-derived Δunrealized on top.
    #   * Schwab's ``Transaction Details`` ``Realized Gain/(Loss)`` column —
    #     realized only (the position's unrealized change still comes from
    #     diffing holdings snapshots). Keep the flag False here.
    per_symbol_pnl_base: dict[str, Decimal] = {}
    per_symbol_pnl_includes_unrealized: bool = False
    # Period-aggregate stock buy / sell notional in base currency. This
    # is set by parsers that publish a *trading-volume summary* on the
    # statement but do not emit per-trade ``Transaction`` rows for
    # stock buys/sells -- specifically Schwab monthly statements,
    # which publish ``Investments Sold`` / ``Investments Purchased``
    # totals in the Cash Transactions Summary. The turnover analytic
    # adds these to the per-trade buy/sell totals it sums out of
    # ``transactions`` so a monthly two-way turnover figure can be
    # computed even on a broker statement that doesn't enumerate
    # individual stock trades.
    #
    # Both numbers are absolute magnitudes (always >= 0). For brokers
    # whose parser already emits per-trade stock buy/sell rows
    # (IBKR), these stay at zero so we don't double-count.
    stock_purchases_base: Decimal = Decimal("0")
    stock_sales_base: Decimal = Decimal("0")
    parser_version: int
    raw_text_hash: str
    source_path: str | None = None

    _quantize_beginning_value_base = field_validator("beginning_value_base", mode="before")(
        quantize_decimal
    )
    _quantize_ending_value_base = field_validator("ending_value_base", mode="before")(
        quantize_decimal
    )
    _quantize_stock_purchases_base = field_validator("stock_purchases_base", mode="before")(
        quantize_decimal
    )
    _quantize_stock_sales_base = field_validator("stock_sales_base", mode="before")(
        quantize_decimal
    )

    @field_validator("base_currency")
    @classmethod
    def uppercase_base_currency(cls, value: str) -> str:
        """Normalize ISO-style currency codes."""
        return value.upper()

    @field_validator("period_end")
    @classmethod
    def period_end_not_before_start(cls, value: date, info: object) -> date:
        """Validate period ordering when both dates are available."""
        data = getattr(info, "data", {})
        start = data.get("period_start")
        if isinstance(start, date) and value < start:
            raise ValueError("period_end cannot be before period_start")
        return value
