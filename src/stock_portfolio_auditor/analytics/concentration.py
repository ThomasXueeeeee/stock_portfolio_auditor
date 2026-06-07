# SPDX-License-Identifier: MIT
"""Monthly portfolio-concentration analytics for the equity book.

This module answers "how concentrated was the stock portfolio on each
month-end inside the audit window?" using **only broker-parsed USD
values** -- no external market-data fetches. The contract is that
``fx_convert_statements`` runs upstream of this module and writes a
correctly FX-translated ``market_value_base`` (in the audit's base
currency, normally USD) onto every :class:`Holding`. Concentration
reads that value verbatim. If FX translation failed for any holding,
the audit pipeline aborts in :mod:`fx_reconstruction` rather than
silently letting HKD-nominal weights leak into the metric here.

Per (account, month-end) pair we need two things:

* **Quantity held per ticker**. For accounts whose broker publishes a
  monthly snapshot (Schwab brokerage, IRA, Roth IRA), the snapshot's
  ``Open Positions`` block is authoritative and is read directly. For
  accounts whose broker publishes only an annual snapshot but provides
  a per-trade ``Trades`` section covering the year (IBKR), quantities
  at any mid-year month-end are *replayed* from the nearest snapshot
  by reversing or applying in-window trades -- see
  :mod:`stock_portfolio_auditor.analytics.holdings_replay`. Without
  replay an annual-snapshot account would show every mid-year month
  with the same Dec 31 quantities, which is wrong any time the user
  rotated the book during the year.

* **USD value per held ticker**. For months where the account has a
  snapshot, the snapshot's ``market_value_base`` is used verbatim.
  For *replayed* months (no snapshot at that month-end), the position
  is valued at the nearest snapshot's *unit USD price* --
  ``snapshot.market_value_base / snapshot.quantity`` for the same
  symbol -- multiplied by the replayed quantity. This intentionally
  side-steps the yfinance close-price fetch a previous version
  attempted: yfinance rate-limits HK tickers regularly, silently
  dropping rate-limited positions out of the concentration metric and
  collapsing the effective-N for the affected months. Using the
  nearest snapshot's broker-published USD price introduces a small
  pricing error between snapshots (the position is held at the
  anchor's price, not the actual month-end's), but the *relative
  weights* (which are what concentration cares about) stay roughly
  consistent and the metric never degrades to "10 positions visible,
  4 of them silently missing".

Concentration is computed on **stocks and ETFs only**: the option leg
of a covered-call strategy is reported separately in the per-
underlying option-income table and including it here would double-
count the underlying's weight against the option contract's notional.
Cash, fixed income, FX pseudo-symbols (``_FX_<CCY>``) and option
pseudo-symbols (``_OPT_<UNDERLYING>``) are likewise excluded.

The metric set is the standard institutional one:

* **Herfindahl-Hirschman Index (HHI)** -- sum of squared weights, in
  [0, 1]. A perfectly equal-weighted 10-name book has HHI = 0.10; a
  one-name book has HHI = 1.0.
* **Effective number of positions** -- ``1 / HHI``. Reads as "the
  book trades as if it were N equal-weighted positions".
* **Top-1 / top-5 / top-10 share** -- cumulative weight of the
  largest 1 / 5 / 10 positions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from stock_portfolio_auditor.analytics.holdings_replay import (
    ReplayResult,
    replay_account_quantities,
)
from stock_portfolio_auditor.domain.models import AssetKind, Holding, Statement

# Holding kinds that count as equity exposure for concentration. Cash,
# fixed income, FX-pseudo and OPTION rows are excluded.
_STOCK_KINDS: frozenset[AssetKind] = frozenset({AssetKind.EQUITY, AssetKind.ETF})


@dataclass(frozen=True)
class ConcentrationSnapshot:
    """Concentration metrics at a single month-end."""

    as_of: date
    portfolio_value_base: float
    n_positions: int
    hhi: float
    effective_n: float
    top1_share: float
    top5_share: float
    top10_share: float


@dataclass(frozen=True)
class YearlyConcentration:
    """Aggregate of monthly concentration snapshots over a year (or Total)."""

    label: str
    months_observed: int
    avg_hhi: float
    avg_effective_n: float
    avg_top1_share: float
    avg_top5_share: float
    avg_top10_share: float
    avg_n_positions: float


def stock_holdings_at(statements: list[Statement], as_of: date) -> list[Holding]:
    """Return the per-symbol stock holdings at ``as_of`` from explicit snapshots only.

    For each account, the holdings from that account's *latest* snapshot
    whose ``period_end <= as_of`` are taken. This is kept on the public
    API for callers that don't need the replay engine (and for tests
    that pin pre-replay behaviour); the report pipeline uses
    :func:`monthly_concentration_series` which combines snapshots with
    trade-replay so an annual-snapshot account contributes correct mid-
    year quantities.
    """
    by_account: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        if stmt.holdings:
            by_account[stmt.account_label].append(stmt)

    pooled: list[Holding] = []
    for account_stmts in by_account.values():
        eligible = [s for s in account_stmts if s.period_end <= as_of]
        if not eligible:
            continue
        latest = max(eligible, key=lambda s: s.period_end)
        for holding in latest.holdings:
            if holding.kind not in _STOCK_KINDS:
                continue
            if holding.symbol.startswith("_OPT_") or holding.symbol.startswith("_FX_"):
                continue
            if holding.market_value_base is None:
                continue
            pooled.append(holding)
    return pooled


def concentration_metrics(holdings: list[Holding], *, as_of: date) -> ConcentrationSnapshot | None:
    """Compute HHI / effective N / top-N share from a list of holdings.

    Non-stock kinds (OPTION, CASH, FX, fixed income) and pseudo-symbol
    rows (``_OPT_*`` / ``_FX_*``) are filtered out here as a defense in
    depth even when the caller forgot to pre-filter via
    :func:`stock_holdings_at`. Holdings with ``market_value_base <= 0``
    (broker-reported short legs or rounding artefacts) are also dropped
    so the long-only stock book is what gets measured. Returns ``None``
    when the long stock book has zero value (no equity exposure that
    month-end).
    """
    by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for h in holdings:
        if h.kind not in _STOCK_KINDS:
            continue
        if h.symbol.startswith("_OPT_") or h.symbol.startswith("_FX_"):
            continue
        if h.market_value_base is None or h.market_value_base <= 0:
            continue
        by_symbol[h.symbol] += h.market_value_base
    total = sum(by_symbol.values())
    if total <= 0:
        return None
    weights = sorted(
        (float(value) / float(total) for value in by_symbol.values()),
        reverse=True,
    )
    hhi = sum(w * w for w in weights)
    effective_n = (1.0 / hhi) if hhi > 0 else 0.0
    return ConcentrationSnapshot(
        as_of=as_of,
        portfolio_value_base=float(total),
        n_positions=len(weights),
        hhi=hhi,
        effective_n=effective_n,
        top1_share=sum(weights[:1]),
        top5_share=sum(weights[:5]),
        top10_share=sum(weights[:10]),
    )


def monthly_concentration_series(
    statements: list[Statement],
    *,
    start: date,
    end: date,
) -> list[ConcentrationSnapshot]:
    """Concentration at each month-end inside ``[start, end]``.

    For each month-end the function:

    1. Walks every distinct account in ``statements``.
    2. If the account has a snapshot at exactly that month-end, uses
       the snapshot's ``market_value_base`` directly (already
       FX-translated to USD upstream by ``fx_convert_statements``).
    3. Otherwise replays the account's quantities at the month-end via
       :func:`replay_account_quantities` and values each held symbol at
       the *nearest snapshot's unit USD price* (``snapshot
       .market_value_base / snapshot.quantity``). No external market
       data is fetched -- yfinance rate-limits caused 30-50% of HK
       positions to silently drop out of the metric and collapse the
       effective-N for the affected months, so the broker-published
       USD price is the safer source.

    Months where the resulting pooled stock book has zero USD value
    are skipped (no equity exposure to measure).
    """
    if start > end:
        return []

    month_ends = _month_ends(start, end)
    if not month_ends:
        return []

    by_account = _statements_by_account(statements)

    snapshots: list[ConcentrationSnapshot] = []
    for month_end in month_ends:
        per_symbol_value: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for _account, account_stmts in by_account.items():
            exact = _find_exact_snapshot(account_stmts, month_end)
            if exact is not None:
                _accumulate_from_snapshot(exact, per_symbol_value)
                continue
            replayed = replay_account_quantities(account_stmts, month_end)
            _accumulate_from_replay_via_nearest_snapshot(
                replayed=replayed,
                account_stmts=account_stmts,
                month_end=month_end,
                out=per_symbol_value,
            )
        snap = _build_snapshot(per_symbol_value, as_of=month_end)
        if snap is None:
            continue
        snapshots.append(snap)
    return snapshots


def _statements_by_account(
    statements: list[Statement],
) -> dict[str, list[Statement]]:
    out: dict[str, list[Statement]] = defaultdict(list)
    for stmt in statements:
        out[stmt.account_label].append(stmt)
    return out


def _find_exact_snapshot(account_stmts: list[Statement], target: date) -> Statement | None:
    """Return the statement whose ``period_end == target`` and that
    actually publishes ``holdings``; ``None`` otherwise."""
    for stmt in account_stmts:
        if stmt.period_end == target and stmt.holdings:
            return stmt
    return None


def _accumulate_from_snapshot(stmt: Statement, out: dict[str, Decimal]) -> None:
    """Sum the snapshot's stock-only ``market_value_base`` into ``out``."""
    for holding in stmt.holdings:
        if holding.kind not in _STOCK_KINDS:
            continue
        if holding.symbol.startswith("_OPT_") or holding.symbol.startswith("_FX_"):
            continue
        if holding.market_value_base is None or holding.market_value_base <= 0:
            continue
        out[holding.symbol] += holding.market_value_base


def _accumulate_from_replay_via_nearest_snapshot(
    *,
    replayed: ReplayResult,
    account_stmts: list[Statement],
    month_end: date,
    out: dict[str, Decimal],
) -> None:
    """Value replayed quantities at the nearest snapshot's unit USD price.

    "Nearest" means the snapshot whose ``period_end`` is closest to the
    target month-end in either direction (preferring the closer one, with
    a tie-break to the future snapshot when distances are equal -- the
    future snapshot's prices are usually more representative of the
    current portfolio than a much older past snapshot's). Each replayed
    symbol's USD value is ``replayed_qty *
    snapshot.market_value_base[symbol] / snapshot.quantity[symbol]``. The
    snapshot's ``market_value_base`` is already FX-translated to USD by
    :func:`fx_convert_statements`, so this multiplication yields a
    correct USD value without any further data fetch.

    Replayed symbols that don't appear in the nearest snapshot are
    skipped silently -- they were opened and closed entirely between
    snapshots, which is rare for a fundamental book and doesn't
    materially move the concentration metric.
    """
    if not replayed.quantities:
        return

    snapshots_with_holdings = [s for s in account_stmts if s.holdings]
    if not snapshots_with_holdings:
        return

    anchor = _nearest_snapshot(snapshots_with_holdings, month_end)
    if anchor is None:
        return

    unit_value_base: dict[str, Decimal] = {}
    for h in anchor.holdings:
        if h.kind not in _STOCK_KINDS:
            continue
        if h.symbol.startswith("_OPT_") or h.symbol.startswith("_FX_"):
            continue
        if h.market_value_base is None or h.market_value_base <= 0:
            continue
        if h.quantity <= 0:
            continue
        unit_value_base[h.symbol] = h.market_value_base / h.quantity

    for symbol, qty in replayed.quantities.items():
        if qty <= 0:
            continue
        if symbol.startswith("_OPT_") or symbol.startswith("_FX_"):
            continue
        unit = unit_value_base.get(symbol)
        if unit is None:
            continue
        out[symbol] += qty * unit


def _nearest_snapshot(snapshots: list[Statement], target: date) -> Statement | None:
    """Pick the snapshot whose ``period_end`` is closest to ``target``.

    Ties favour the future snapshot because the user-visible
    concentration time series typically extends into a month whose
    final snapshot has not yet been published; the in-progress
    portfolio is more accurately approximated by next month's snapshot
    than by the previous one. Returns ``None`` when ``snapshots`` is
    empty.
    """
    if not snapshots:
        return None
    forward = sorted(
        (s for s in snapshots if s.period_end >= target),
        key=lambda s: s.period_end,
    )
    past = sorted(
        (s for s in snapshots if s.period_end < target),
        key=lambda s: s.period_end,
        reverse=True,
    )
    if forward and past:
        fwd_gap = (forward[0].period_end - target).days
        past_gap = (target - past[0].period_end).days
        return forward[0] if fwd_gap <= past_gap else past[0]
    return forward[0] if forward else past[0]


def _build_snapshot(
    per_symbol_value: dict[str, Decimal], *, as_of: date
) -> ConcentrationSnapshot | None:
    """Compute HHI / effective-N / top-N from per-symbol USD values."""
    positive = {s: v for s, v in per_symbol_value.items() if v > 0}
    total = sum(positive.values())
    if total <= 0:
        return None
    weights = sorted((float(v) / float(total) for v in positive.values()), reverse=True)
    hhi = sum(w * w for w in weights)
    effective_n = (1.0 / hhi) if hhi > 0 else 0.0
    return ConcentrationSnapshot(
        as_of=as_of,
        portfolio_value_base=float(total),
        n_positions=len(weights),
        hhi=hhi,
        effective_n=effective_n,
        top1_share=sum(weights[:1]),
        top5_share=sum(weights[:5]),
        top10_share=sum(weights[:10]),
    )


def yearly_concentration_aggregates(
    snapshots: list[ConcentrationSnapshot],
    *,
    audit_start: date,
    audit_end: date,
    period_labeler: Callable[[int, date, date], str] | None = None,
) -> list[YearlyConcentration]:
    """Per-year averages plus a ``Total`` row across the audit window.

    The yearly row averages the monthly snapshot metrics that fall in
    that calendar year, clipped to the audit window so a partial year
    (e.g. ``2026 (Jan-Apr)``) is averaged only over the in-window
    months. ``period_labeler(year, audit_start, audit_end)`` is used for
    the label when supplied so the report's smart yearly labels (e.g.
    ``2026 (Jan-Apr)``) match the rest of the table; otherwise the
    label is the bare year integer.
    """
    if not snapshots:
        return []
    by_year: dict[int, list[ConcentrationSnapshot]] = defaultdict(list)
    for snap in snapshots:
        by_year[snap.as_of.year].append(snap)

    rows: list[YearlyConcentration] = []
    for year in sorted(by_year):
        year_snaps = by_year[year]
        if period_labeler is not None:
            label = period_labeler(year, audit_start, audit_end)
        else:
            label = str(year)
        rows.append(_aggregate(label, year_snaps))
    rows.append(_aggregate("Total", snapshots))
    return rows


def _aggregate(label: str, snaps: list[ConcentrationSnapshot]) -> YearlyConcentration:
    n = len(snaps)
    if n == 0:
        return YearlyConcentration(
            label=label,
            months_observed=0,
            avg_hhi=0.0,
            avg_effective_n=0.0,
            avg_top1_share=0.0,
            avg_top5_share=0.0,
            avg_top10_share=0.0,
            avg_n_positions=0.0,
        )
    return YearlyConcentration(
        label=label,
        months_observed=n,
        avg_hhi=sum(s.hhi for s in snaps) / n,
        avg_effective_n=sum(s.effective_n for s in snaps) / n,
        avg_top1_share=sum(s.top1_share for s in snaps) / n,
        avg_top5_share=sum(s.top5_share for s in snaps) / n,
        avg_top10_share=sum(s.top10_share for s in snaps) / n,
        avg_n_positions=sum(s.n_positions for s in snaps) / n,
    )


def _month_ends(start: date, end: date) -> list[date]:
    """Calendar month-ends from the first month-end >= ``start`` through
    the last month-end <= ``end``."""
    if start > end:
        return []
    # Find the first month-end on or after ``start``.
    year, month = start.year, start.month
    me = _last_day_of_month(year, month)
    if me < start:
        year, month = _add_month(year, month)
        me = _last_day_of_month(year, month)
    out: list[date] = []
    while me <= end:
        out.append(me)
        year, month = _add_month(year, month)
        me = _last_day_of_month(year, month)
    return out


def _add_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _last_day_of_month(year: int, month: int) -> date:
    next_year, next_month = _add_month(year, month)
    return date(next_year, next_month, 1) - timedelta(days=1)
