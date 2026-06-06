# SPDX-License-Identifier: MIT
"""Per-position contribution analytics.

The Schwab and IBKR statements we audit carry a snapshot of holdings at each
period_end with both ``market_value_base`` and ``cost_basis_local`` columns
populated. We use those two columns to compute a per-position dollar PnL by
comparing consecutive snapshots within an account:

    position_pnl = sum_t  (mv_t - cb_t) - (mv_{t-1} - cb_{t-1})

This is the change in *unrealized* gain over time. For positions held without
intra-period sales it equals the position's actual return contribution; for
positions where shares were sold mid-period the realized PnL is folded into
the cost-basis change, so the measure remains a reasonable proxy but is not
exact. The :func:`position_contributions` docstring spells out the caveats.

The output is consumed by the "Top Contributors / Top Detractors" panel in
the HTML report (modeled on the ARK / T. Rowe Price / Capital Group fund
letter format).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from stock_portfolio_auditor.domain.models import Holding, Statement


@dataclass(frozen=True, slots=True)
class PositionContribution:
    """One row in the per-position contribution table."""

    symbol: str
    description: str
    accounts: tuple[str, ...]
    dollar_pnl: float
    contribution_pct: float  # dollar_pnl / starting pooled portfolio value
    average_market_value: float
    ending_market_value: float
    first_seen: date
    last_seen: date


def position_contributions(
    statements: list[Statement],
    *,
    total_dollar_pnl: float | None = None,
    audit_end: date | None = None,
) -> list[PositionContribution]:
    """Return one :class:`PositionContribution` per ticker held over the period.

    Implementation notes:
        * Each (account, symbol) pair is walked through that account's
          consecutive statements. The dollar-PnL increment per period is
          either the broker-published per-symbol PnL (IBKR MTM, Schwab
          realized) or the change in unrealized gain
          ``Δ(market_value_base - cost_basis_base)`` from holdings snapshots.
          **This is the "Price PnL" of the position** — realized + unrealized
          price movement and commissions only. Dividends, lending income,
          interest, ADR fees and withholding tax are *not* attributed here;
          they appear in their own buckets of the dollar-P&L decomposition
          chart so the per-position total reconciles to the chart's Price
          bar rather than double-counting income sources.
        * When the same ticker appears in multiple accounts the contributions
          are summed across accounts.
        * **Ending market value** is computed as ``total_quantity ×
          audit_end_price`` — that is, sum the shares held across all accounts
          at each one's most recent snapshot, then multiply by the most recent
          observed unit price for that symbol that falls **on or before**
          ``audit_end``. This avoids the mixed-price artifact from summing
          ``mv`` across snapshots dated differently and respects the audit
          window boundary (a position priced at IBKR's May-22 snapshot is
          not used when the audit ends April 30 — Schwab's April-30 price for
          the same symbol is used instead).
        * ``contribution_pct = dollar_pnl / total_dollar_pnl``. The caller
          supplies ``total_dollar_pnl`` (typically the audit window's total
          dollar P&L from the KPI strip). When omitted we use the sum of all
          positions' dollar_pnl as the denominator. This matches the
          "share of P&L" convention used in fund letters (ARK, Capital Group,
          T. Rowe Price): the percentages across rows sum to ~100% of the
          attributed total.
        * Inputs must already be FX-translated to the base currency
          (see :mod:`stock_portfolio_auditor.ingestion.fx_reconstruction`).
    """
    if not statements:
        return []

    by_account: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        by_account[stmt.account_label].append(stmt)
    for label in by_account:
        by_account[label].sort(key=lambda stmt: stmt.period_end)

    # Position contribution is **price-only**: per-stock contribution to the
    # ``Price`` bar in the dollar-P&L decomposition chart. Income flows
    # (dividends, broker interest, lending income, ADR fees, withholding tax)
    # are intentionally **excluded** so the per-position total reconciles to
    # the Price bucket rather than double-counting income sources that are
    # already shown separately in the chart.

    # All accumulators are keyed by (account, symbol) so that the same ticker in
    # different accounts is tracked separately for MV/observation purposes.
    dollar_pnl_per_pair: dict[tuple[str, str], float] = defaultdict(float)
    last_seen_per_pair: dict[tuple[str, str], date] = {}
    first_seen_per_pair: dict[tuple[str, str], date] = {}
    last_mv_per_pair: dict[tuple[str, str], float] = {}
    last_quantity_per_pair: dict[tuple[str, str], float] = {}
    avg_obs_per_pair: dict[tuple[str, str], list[float]] = defaultdict(list)
    description_per_pair: dict[tuple[str, str], str] = {}
    # Track the most recent observed price per symbol so the consolidated
    # ending MV uses a single consistent price across accounts.
    latest_price_per_symbol: dict[str, tuple[date, float]] = {}

    for account, stmts in by_account.items():
        prior: dict[str, Holding] = {}
        for stmt in stmts:
            current = {h.symbol: h for h in stmt.holdings}
            published = stmt.per_symbol_pnl_base or {}

            # Step 1: take whatever PnL the statement publishes per symbol.
            # For IBKR (per_symbol_pnl_includes_unrealized=True) this is the
            # full stock PnL — realized + unrealized + commissions. For
            # Schwab the same field carries only the realized portion of
            # closing trades; the unrealized change comes from holdings.
            for symbol, pnl_decimal in published.items():
                key = (account, symbol)
                dollar_pnl_per_pair[key] += float(pnl_decimal)
                first_seen_per_pair.setdefault(key, stmt.period_end)
                last_seen_per_pair[key] = stmt.period_end

            # Step 2: when the published numbers don't already cover unrealized
            # change, add Δ(mv − cb) per symbol. We iterate the union of
            # current and prior holdings so positions that vanished mid-period
            # still get their prior unrealized subtracted.
            if not stmt.per_symbol_pnl_includes_unrealized:
                for symbol in set(current) | set(prior):
                    key = (account, symbol)
                    end = _unrealized(current[symbol]) if symbol in current else 0.0
                    start = _unrealized(prior[symbol]) if symbol in prior else 0.0
                    dollar_pnl_per_pair[key] += end - start

            # Step 3: track current-snapshot fields for the report row even
            # for symbols whose PnL was published directly. Also remember the
            # most recent observed price per symbol that falls on or before
            # ``audit_end`` so we can re-price every account's quantity at the
            # audit boundary price.
            for symbol, holding in current.items():
                key = (account, symbol)
                last_seen_per_pair[key] = stmt.period_end
                first_seen_per_pair.setdefault(key, stmt.period_end)
                last_mv_per_pair[key] = float(holding.market_value_base)
                # Only count the quantity into the audit-end snapshot if the
                # snapshot falls on or before the audit boundary. Otherwise
                # we'd report post-audit positions in the "ending" rows.
                if audit_end is None or stmt.period_end <= audit_end:
                    last_quantity_per_pair[key] = float(holding.quantity)
                avg_obs_per_pair[key].append(float(holding.market_value_base))
                if holding.description and key not in description_per_pair:
                    description_per_pair[key] = holding.description
                qty = float(holding.quantity)
                if qty != 0 and (audit_end is None or stmt.period_end <= audit_end):
                    unit_price = float(holding.market_value_base) / qty
                    existing = latest_price_per_symbol.get(symbol)
                    if existing is None or stmt.period_end > existing[0]:
                        latest_price_per_symbol[symbol] = (stmt.period_end, unit_price)

            # Note: income / cost transactions (dividends, interest, lending,
            # ADR fees, withholding tax) are intentionally NOT folded into
            # per-position attribution. They show up as their own bars in the
            # dollar-P&L decomposition chart and are reported under
            # ``Dividends`` / ``Lending`` / ``Cost drag`` in the yearly
            # summary, so attributing them per stock here would double-count.

            prior = current

    # Now aggregate per-symbol across accounts.
    pnl_by_symbol: dict[str, float] = defaultdict(float)
    total_quantity_by_symbol: dict[str, float] = defaultdict(float)
    avg_obs_by_symbol: dict[str, list[float]] = defaultdict(list)
    description_by_symbol: dict[str, str] = {}
    accounts_by_symbol: dict[str, set[str]] = defaultdict(set)
    first_seen_by_symbol: dict[str, date] = {}
    last_seen_by_symbol: dict[str, date] = {}

    for (account, symbol), pnl in dollar_pnl_per_pair.items():
        pnl_by_symbol[symbol] += pnl
        total_quantity_by_symbol[symbol] += last_quantity_per_pair.get((account, symbol), 0.0)
        avg_obs_by_symbol[symbol].extend(avg_obs_per_pair[(account, symbol)])
        accounts_by_symbol[symbol].add(account)
        if symbol not in description_by_symbol:
            description_by_symbol[symbol] = description_per_pair.get((account, symbol), symbol)
        first_dt = first_seen_per_pair[(account, symbol)]
        last_dt = last_seen_per_pair[(account, symbol)]
        if symbol not in first_seen_by_symbol or first_dt < first_seen_by_symbol[symbol]:
            first_seen_by_symbol[symbol] = first_dt
        if symbol not in last_seen_by_symbol or last_dt > last_seen_by_symbol[symbol]:
            last_seen_by_symbol[symbol] = last_dt

    if total_dollar_pnl is None or total_dollar_pnl == 0:
        denominator = sum(p for p in pnl_by_symbol.values()) or 1.0
    else:
        denominator = total_dollar_pnl

    rows: list[PositionContribution] = []
    for symbol, pnl in pnl_by_symbol.items():
        observations = avg_obs_by_symbol[symbol]
        average_mv = sum(observations) / len(observations) if observations else 0.0
        # Re-price total quantity at the most recently observed unit price.
        latest_price = latest_price_per_symbol.get(symbol)
        if latest_price is not None:
            ending_mv = total_quantity_by_symbol[symbol] * latest_price[1]
        else:
            ending_mv = 0.0
        rows.append(
            PositionContribution(
                symbol=symbol,
                description=description_by_symbol.get(symbol, symbol),
                accounts=tuple(sorted(accounts_by_symbol[symbol])),
                dollar_pnl=pnl,
                contribution_pct=pnl / denominator if denominator else 0.0,
                average_market_value=average_mv,
                ending_market_value=ending_mv,
                first_seen=first_seen_by_symbol[symbol],
                last_seen=last_seen_by_symbol[symbol],
            )
        )
    rows.sort(key=lambda row: row.dollar_pnl, reverse=True)
    return rows


def _is_real_stock(row: PositionContribution) -> bool:
    """Filter pseudo-symbols (``_INCOME`` etc.) out of contributor rankings.

    The Top Contributors / Top Detractors lists should only show real stocks,
    not the synthetic ``_INCOME`` bucket that aggregates broker sweep interest
    and other cash-only transactions.
    """
    return not row.symbol.startswith("_")


def top_contributors(
    contributions: list[PositionContribution], *, n: int = 5
) -> list[PositionContribution]:
    """Return the top ``n`` positive contributors (descending dollar_pnl)."""
    return [row for row in contributions if row.dollar_pnl > 0 and _is_real_stock(row)][:n]


def top_detractors(
    contributions: list[PositionContribution], *, n: int = 5
) -> list[PositionContribution]:
    """Return the top ``n`` detractors (most-negative dollar_pnl, low->high)."""
    detractors = [row for row in contributions if row.dollar_pnl < 0 and _is_real_stock(row)]
    detractors.sort(key=lambda row: row.dollar_pnl)
    return detractors[:n]


def total_attributed_pnl(contributions: list[PositionContribution]) -> float:
    """Sum of dollar_pnl across **real-stock** positions only.

    Pseudo-symbols (``_OPT_<UNDERLYING>``, ``_FX_<CCY>``, ``_INCOME``) are
    excluded so the headline "Total attributed P&L" lines up with the
    stocks-only Top Contributors / Detractors tables. Options and FX have
    their own report sections sourced from transaction cash flow, which
    handles assignments and currency-translation accounting more cleanly
    than mixing them into stock attribution.
    """
    return sum(row.dollar_pnl for row in contributions if _is_real_stock(row))


def stock_contributions(
    contributions: list[PositionContribution],
) -> list[PositionContribution]:
    """Return only the real-stock subset of a contribution list."""
    return [row for row in contributions if _is_real_stock(row)]


def _unrealized(holding: Holding) -> float:
    """Return ``market_value_base - cost_basis_base`` in the base currency.

    The FX reconstruction step always populates ``cost_basis_base`` so the
    two values are guaranteed to be in the same currency. If both are missing
    or the cost basis isn't available, the position contributes zero (we don't
    pretend to know its PnL).
    """
    mv = float(holding.market_value_base)
    if holding.cost_basis_base is not None:
        cb = float(holding.cost_basis_base)
    elif holding.cost_basis_local is not None and holding.currency == "USD":
        cb = float(holding.cost_basis_local)
    else:
        cb = 0.0
    return mv - cb
