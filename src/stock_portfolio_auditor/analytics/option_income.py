# SPDX-License-Identifier: MIT
"""Per-underlying option income aggregation.

Reports option writing P&L as **transaction cash flow per underlying ticker**
rather than 1099 realized G/L. The distinction matters because Schwab's
1099 reports an *assigned* short put / call as if it were bought-to-close
at intrinsic value -- a single trade can show, say, -$6,881 of "realized
loss" on the option leg when in fact no cash was paid on that leg (the
cash flow happened on the assigned stock side, which now carries a $60
strike cost basis).

The cash-flow approach matches how an option writer actually thinks about
income:

  * Sale ShortSale / option-open premium received -> +amount
  * BTC purchase -> -amount (close cost)
  * Expired worthless -> no transaction emitted, premium stays as profit
  * Assignment -> no transaction emitted on the option leg, stock side
    carries the cash event at strike

Summing all ``Transaction(kind=OPTION)`` rows per underlying yields the
true net premium income the user earned from writing options on that
underlying.

The underlying ticker is extracted from each transaction's symbol:

  * Schwab option transactions emit ``symbol = "_OPT_<UNDERLYING>"``
    via the parser, so the underlying is the suffix after ``_OPT_``.
  * IBKR option Trades emit the full option contract identifier
    (e.g. ``JXN 21MAR25 80 P``), so the underlying is the first
    whitespace-separated token.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from stock_portfolio_auditor.domain.models import AssetKind, Statement


@dataclass(frozen=True, slots=True)
class OptionIncomeRow:
    """Per-underlying option income / cost aggregate."""

    underlying: str
    premium_received: float
    premium_paid: float
    net: float


def _option_underlying(symbol: str | None) -> str | None:
    """Extract the underlying ticker from an option Transaction's ``symbol``."""
    if not symbol:
        return None
    if symbol.startswith("_OPT_"):
        return symbol.removeprefix("_OPT_").upper() or None
    # IBKR-style ``JXN 21MAR25 80 P``: take the first whitespace-separated token.
    head = symbol.split(" ", 1)[0].strip().upper()
    return head or None


def option_income_by_underlying(statements: list[Statement]) -> list[OptionIncomeRow]:
    """Aggregate per-underlying option transaction cash flow.

    Positive ``amount_base`` rows (premium received from short-sale opens
    and long-position closes) accumulate into ``premium_received``;
    negative rows (close costs and long-position opens) accumulate into
    ``premium_paid``; ``net`` is the sum.

    Result is sorted by ``net`` descending so the biggest contributors
    appear first.
    """
    received: dict[str, float] = defaultdict(float)
    paid: dict[str, float] = defaultdict(float)
    for stmt in statements:
        for txn in stmt.transactions:
            if txn.kind is not AssetKind.OPTION:
                continue
            underlying = _option_underlying(txn.symbol)
            if underlying is None:
                continue
            amount = float(txn.amount_base)
            if amount >= 0:
                received[underlying] += amount
            else:
                paid[underlying] += amount
    underlyings = set(received) | set(paid)
    rows = [
        OptionIncomeRow(
            underlying=underlying,
            premium_received=received.get(underlying, 0.0),
            premium_paid=paid.get(underlying, 0.0),
            net=received.get(underlying, 0.0) + paid.get(underlying, 0.0),
        )
        for underlying in underlyings
    ]
    rows.sort(key=lambda row: row.net, reverse=True)
    return rows


def total_option_income(rows: list[OptionIncomeRow]) -> float:
    """Sum the per-underlying net option income."""
    return sum(row.net for row in rows)
