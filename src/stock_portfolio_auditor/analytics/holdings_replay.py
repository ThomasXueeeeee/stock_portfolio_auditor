# SPDX-License-Identifier: MIT
"""Per-account holdings replay -- reconstruct quantity held at any date.

Brokers publish portfolio snapshots at different frequencies:

* **Schwab brokerage / IRA** -- one snapshot per monthly statement.
* **IBKR Activity Statement** -- one snapshot at ``period_end`` plus a
  per-trade ``Trades`` section covering the whole period. A yearly
  IBKR statement therefore gives one explicit snapshot (Dec 31) plus
  ~hundreds of trades spread across the year.

For analytics that need a month-by-month portfolio state -- monthly
concentration, monthly drift, factor exposure series -- a snapshot at
each month-end is required even when the broker only publishes one
snapshot every twelve months. This module reconstructs the missing
snapshots by *replaying trades* against the broker's nearest explicit
snapshot:

* If the target ``as_of`` is **after** the closest known snapshot,
  apply each in-window trade forward.
* If ``as_of`` is **before** the closest known snapshot (the common
  IBKR case -- we have a Dec 31 snapshot and want, say, Jul 31),
  reverse each in-window trade backward.
* If a snapshot already exists at ``as_of``, use it directly.

The output is a ``dict[symbol, quantity]``; pricing those quantities at
month-end market closes is done in the concentration analytic, not
here. Replay is per-account because trades on one account never affect
positions in another.

The replay correctly handles only **stock-side** ``buy`` / ``sell``
trades. Options are reported separately by the option-income table.
Splits, spin-offs, and other corporate-action quantity adjustments are
NOT applied because broker statements don't enumerate them as
transactions; for tickers that split inside the audit window the
replayed quantity will be wrong for months between the split and the
next explicit snapshot. The forward replay self-corrects at the next
snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.domain.models import AssetKind, Statement

_TRADING_KINDS: frozenset[AssetKind] = frozenset({AssetKind.EQUITY, AssetKind.ETF})


@dataclass(frozen=True)
class ReplayResult:
    """Reconstructed quantities at a target date for one account.

    ``quantities`` maps ticker -> quantity held (always positive; zero-
    quantity positions are dropped). ``currency_by_symbol`` records the
    listing currency the broker reported for each symbol so the
    pricing-and-FX step downstream can pull the right local market
    close and translate it to the audit base currency. ``anchor_date``
    is the snapshot ``period_end`` the replay walked from (useful for
    surfacing "we used the Dec 31, 2023 anchor for Jul 2023" in logs).
    """

    as_of: date
    quantities: dict[str, Decimal]
    currency_by_symbol: dict[str, str]
    anchor_date: date | None


def replay_account_quantities(
    statements_for_account: list[Statement],
    as_of: date,
) -> ReplayResult:
    """Replay trades against the nearest snapshot to derive quantities at ``as_of``.

    ``statements_for_account`` must already be filtered to a single
    account (otherwise cross-account trades would mutate a portfolio
    they don't belong to). Pass the full set of statements for that
    account; this function picks the right anchor itself.
    """
    snapshots = sorted(
        [s for s in statements_for_account if s.holdings],
        key=lambda s: s.period_end,
    )
    if not snapshots:
        return ReplayResult(as_of=as_of, quantities={}, currency_by_symbol={}, anchor_date=None)

    # Anchor selection: prefer the closest snapshot >= as_of so we can
    # reverse a small number of recent trades, falling back to the
    # latest snapshot before as_of when no forward snapshot exists.
    forward = [s for s in snapshots if s.period_end >= as_of]
    anchor = forward[0] if forward else snapshots[-1]

    quantities: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    currencies: dict[str, str] = {}
    for h in anchor.holdings:
        if h.kind not in _TRADING_KINDS:
            continue
        if h.symbol.startswith("_OPT_") or h.symbol.startswith("_FX_"):
            continue
        quantities[h.symbol] = h.quantity
        currencies[h.symbol] = h.currency

    if anchor.period_end == as_of:
        return ReplayResult(
            as_of=as_of,
            quantities={s: q for s, q in quantities.items() if q > 0},
            currency_by_symbol=currencies,
            anchor_date=anchor.period_end,
        )

    # Trades pool: every trading-kind buy/sell across all statements for
    # this account. The IBKR parser emits one per fill on the ``Trades``
    # section with a *signed* ``quantity`` (positive on buy, negative on
    # sell) -- we apply that sign directly.
    all_trades = [
        txn
        for stmt in statements_for_account
        for txn in stmt.transactions
        if txn.kind in _TRADING_KINDS
        and txn.action in ("buy", "sell")
        and txn.symbol is not None
        and not txn.symbol.startswith("_OPT_")
        and not txn.symbol.startswith("_FX_")
    ]

    if anchor.period_end > as_of:
        # Walk backward: reverse trades that happened after as_of and on
        # or before the anchor. The trade's signed quantity is added
        # with a flipped sign to undo it.
        for txn in all_trades:
            symbol = txn.symbol
            if symbol is None:
                continue
            if as_of < txn.trade_date <= anchor.period_end:
                quantities[symbol] = quantities.get(symbol, Decimal("0")) - txn.quantity
                currencies.setdefault(symbol, txn.currency)
    else:
        # Walk forward: apply trades that happened after the anchor and
        # on or before as_of.
        for txn in all_trades:
            symbol = txn.symbol
            if symbol is None:
                continue
            if anchor.period_end < txn.trade_date <= as_of:
                quantities[symbol] = quantities.get(symbol, Decimal("0")) + txn.quantity
                currencies.setdefault(symbol, txn.currency)

    return ReplayResult(
        as_of=as_of,
        quantities={s: q for s, q in quantities.items() if q > 0},
        currency_by_symbol=currencies,
        anchor_date=anchor.period_end,
    )


def replay_all_accounts(
    statements: list[Statement],
    as_of: date,
) -> dict[str, ReplayResult]:
    """Replay every distinct account in ``statements`` and return a per-account map."""
    by_account: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        by_account[stmt.account_label].append(stmt)
    return {
        account: replay_account_quantities(stmts, as_of) for account, stmts in by_account.items()
    }
