# SPDX-License-Identifier: MIT
"""Audit-window filter for parsed statements.

The audit window is the ``(start_date, end_date)`` interval the operator
supplies on the CLI. The contract is: the report must reflect only data
the broker attributes to dates inside the window. The previous version
of this filter rejected any statement that overflowed either boundary,
which threw away useful data on documents like an annual 1099 Composite
(rejected because Jan-Sep extends before the audit start) or a YTD IBKR
CSV (rejected because the last few weeks extend past the audit end).

The corrected approach is to *clip* partial-overlap statements instead
of dropping them:

* Transactions are filtered to ``trade_date`` inside the window.
* Holdings and cash-balance snapshots (which are taken at
  ``period_end``) are kept only when ``period_end`` itself falls in the
  window -- a holdings snapshot dated outside the window cannot be the
  audit's "final" snapshot.
* ``per_symbol_pnl_base`` is the trickiest field because it is published
  as a single full-period aggregate on annual / YTD documents.

  - When the parser also emitted per-disposition ``Transaction`` rows
    that carry a ``realized_pnl_base`` value (Schwab 1099 Composite,
    IBKR Trades), the aggregate is rebuilt by summing those rows whose
    ``trade_date`` falls in the audit window. This gives an exact
    in-window realized PnL for the 1099 case.
  - When no per-disposition data is available (or the statement's
    ``per_symbol_pnl_includes_unrealized=True`` -- IBKR's
    Mark-to-Market Performance Summary mixes realized and unrealized
    in a way that can't be split off per trade), the aggregate is kept
    as-is and a note is logged explaining the overshoot. The
    alternative -- dropping the unrealized portion entirely -- would
    leak more error than a small period overshoot.

The function returns ``(in_window_statements, excluded)`` where
``excluded`` is a list of ``(statement, reason)`` tuples for statements
that fell fully outside the window or were clipped with a notable
adjustment that the operator should see in the run log.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.domain.models import Statement, Transaction


def filter_statements_to_window(
    statements: list[Statement],
    *,
    start_date: date,
    end_date: date,
) -> tuple[list[Statement], list[tuple[Statement, str]]]:
    """Return statements clipped to the audit window.

    Returns ``(included, excluded)`` where ``included`` may contain
    *modified* Statement instances (partial-overlap statements with their
    transactions / holdings / per-symbol PnL trimmed to the window), and
    ``excluded`` contains ``(statement, reason)`` tuples both for
    statements fully outside the window and for partial-overlap
    statements whose adjustment is worth surfacing in the run log (the
    same Statement is also in ``included`` in the latter case).
    """
    if start_date > end_date:
        raise ValueError(f"Audit start {start_date} is after end {end_date}; supply a sane window.")
    included: list[Statement] = []
    excluded: list[tuple[Statement, str]] = []
    for stmt in statements:
        result = _filter_statement_to_window(stmt, start_date=start_date, end_date=end_date)
        if result is None:
            excluded.append(
                (
                    stmt,
                    f"period {stmt.period_start}..{stmt.period_end} is fully "
                    f"outside audit window {start_date}..{end_date}",
                )
            )
            continue
        clipped, note = result
        included.append(clipped)
        if note is not None:
            excluded.append((stmt, note))
    return included, excluded


def _filter_statement_to_window(
    stmt: Statement,
    *,
    start_date: date,
    end_date: date,
) -> tuple[Statement, str | None] | None:
    """Clip a single Statement to the audit window.

    Returns ``None`` when the statement is fully outside the window. On
    success returns ``(clipped_statement, note_or_None)`` where ``note``
    is set when the clipping introduces an approximation worth logging
    (typically a full-period ``per_symbol_pnl_base`` kept on a partial
    overlap).
    """
    if stmt.period_end < start_date or stmt.period_start > end_date:
        return None
    fully_in = stmt.period_start >= start_date and stmt.period_end <= end_date
    if fully_in:
        return stmt, None

    in_window_transactions = tuple(
        t for t in stmt.transactions if start_date <= t.trade_date <= end_date
    )
    snapshot_in_window = start_date <= stmt.period_end <= end_date
    new_holdings = stmt.holdings if snapshot_in_window else ()
    new_cash_balances = stmt.cash_balances if snapshot_in_window else ()

    new_per_symbol, rebuilt = _clip_per_symbol_pnl(stmt, in_window_transactions)

    clipped = stmt.model_copy(
        update={
            "transactions": in_window_transactions,
            "holdings": new_holdings,
            "cash_balances": new_cash_balances,
            "per_symbol_pnl_base": new_per_symbol,
        }
    )
    note = _build_clip_note(
        stmt,
        start_date=start_date,
        end_date=end_date,
        rebuilt=rebuilt,
        dropped_snapshot=not snapshot_in_window and bool(stmt.holdings),
    )
    return clipped, note


def _clip_per_symbol_pnl(
    stmt: Statement,
    in_window_transactions: tuple[Transaction, ...],
) -> tuple[dict[str, Decimal], bool]:
    """Rebuild ``per_symbol_pnl_base`` from in-window dispositions when possible.

    Returns ``(per_symbol, rebuilt_flag)``. ``rebuilt_flag`` is True when
    the aggregate was reconstructed from per-disposition rows (exact
    in-window number) and False when the full-period aggregate is kept
    as-is (approximation).
    """
    if stmt.per_symbol_pnl_includes_unrealized:
        # IBKR Mark-to-Market Performance Summary: aggregate mixes
        # realized + unrealized + commissions. Per-trade Realized P/L
        # only captures the realized slice, so rebuilding from
        # in-window trades would drop the unrealized contribution.
        # Keep the full-period aggregate (overshoots by the out-of-window
        # tail, but no worse than dropping the statement entirely).
        return dict(stmt.per_symbol_pnl_base), False

    has_disposition_data = any(t.realized_pnl_base is not None for t in stmt.transactions)
    if not has_disposition_data:
        return dict(stmt.per_symbol_pnl_base), False

    rebuilt: dict[str, Decimal] = {}
    for txn in in_window_transactions:
        if txn.realized_pnl_base is None or txn.symbol is None:
            continue
        rebuilt[txn.symbol] = rebuilt.get(txn.symbol, Decimal("0")) + txn.realized_pnl_base
    return rebuilt, True


def _build_clip_note(
    stmt: Statement,
    *,
    start_date: date,
    end_date: date,
    rebuilt: bool,
    dropped_snapshot: bool,
) -> str | None:
    """Compose a human-readable note describing how the statement was clipped."""
    parts: list[str] = []
    if stmt.period_start < start_date:
        parts.append(f"start {stmt.period_start} before audit start {start_date}")
    if stmt.period_end > end_date:
        parts.append(f"end {stmt.period_end} after audit end {end_date}")
    if not parts:
        return None
    overlap_desc = "; ".join(parts)
    if rebuilt:
        return (
            f"clipped to {start_date}..{end_date} ({overlap_desc}); "
            f"per_symbol_pnl_base rebuilt from in-window dispositions"
        )
    handling = (
        "per_symbol_pnl_base kept as full-period aggregate (no per-disposition "
        "data available -- accept slight over-attribution or re-download with "
        "tighter dates)"
    )
    if dropped_snapshot:
        handling += "; period_end snapshot dropped (outside audit window)"
    return f"clipped to {start_date}..{end_date} ({overlap_desc}); {handling}"
