# SPDX-License-Identifier: MIT
"""Post-parse FX translation of non-base-currency holdings to base currency.

The IBKR Activity Statement CSV reports each Open Position in its local
trading currency (HKD for HK stocks, JPY for TSE stocks, ...) and **does
not** publish a per-position USD value. Our parser previously copied the
local value into ``market_value_base`` which is incorrect for non-USD
holdings; a Hong Kong stock worth HK$913 would have been recorded as if it
were US$913 and would dominate top-contributor / detractor lists or
attribution panels.

This module fixes that as a post-parse step:

    1. Collect every (non-base currency, statement period_end) pair that
       appears in any holding.
    2. Fetch a daily FX series for each currency pair via the active
       :class:`PriceProvider` (yfinance default, Stooq fallback, parquet
       cached). Network failures degrade gracefully — the original (broken)
       values are left in place with a warning.
    3. Replace ``market_value_base`` and ``cost_basis_local`` on every
       non-base-currency holding with the FX-translated USD amount, using
       the most recent rate at or before that statement's ``period_end``.

The pricing FX provider already handles caching to ``data_cache/prices``,
so re-runs only hit the network when a new (currency, date) is encountered.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
from loguru import logger

from stock_portfolio_auditor.domain.errors import MissingFxRateError
from stock_portfolio_auditor.domain.models import Holding, Statement, Transaction
from stock_portfolio_auditor.pricing.providers.base import PriceProvider
from stock_portfolio_auditor.pricing.providers.factory import build_price_provider

# Maximum tolerable gap (in days) between a required FX-rate date and
# the nearest cached observation. FX series carry daily closes on
# trading days; a 7-day window lets us bridge a weekend, a national
# holiday, or a missing day in the data without forcing a hard failure
# every time the broker stamps a transaction on a Friday-after-close.
_MAX_FX_RATE_GAP_DAYS = 7

# Cost basis quantization matches the rest of the pydantic model.
_DECIMAL_QUANT = Decimal("0.0001")


def fx_convert_statements(
    statements: list[Statement],
    *,
    provider: PriceProvider | None = None,
    strict: bool = True,
) -> list[Statement]:
    """Return a new list of statements with FX-translated base-currency values.

    This is the *single FX chokepoint* for the report pipeline. Every
    downstream analytic (turnover, concentration, contribution, dollar
    PnL decomposition, pooled NAV / TWR / MWR) reads ``*_base`` fields
    and trusts them to be in the audit base currency (normally USD).
    No analytic re-fetches FX on its own.

    Cost basis is translated using the (account, symbol) pair's earliest
    snapshot rate rather than each statement's period-end rate. Translating
    cost basis at the period-end rate moves it in lockstep with market value
    and therefore hides the FX gain on cost — for a long-held HKD or JPY
    position with material currency drift, (rate_today − rate_acquired) ×
    cost_local of FX-driven P&L would otherwise be invisible to the
    per-position attribution. We can't see real per-lot acquisition dates
    from IBKR's Open Positions section, so we approximate "acquisition" as
    the earliest snapshot date on which the (account, symbol) pair appears
    inside the audit window — which is exact for positions opened during
    the window and a tight upper bound for positions held since before it.

    When ``strict=True`` (the default), the function raises
    :class:`MissingFxRateError` if any required ``(currency, base, date)``
    tuple can't be resolved within ``_MAX_FX_RATE_GAP_DAYS`` days of a
    cached observation. The downstream analytics (turnover,
    concentration, dollar-PnL decomposition) all sum
    ``|amount_base|`` over rows in the audit window; silently leaving
    a HK$50,000 trade with ``amount_base = 50000`` (HKD nominal,
    treated as USD by the analytic) inflates the metric by ~8x, so
    failing loudly is the correct behaviour. Pass ``strict=False``
    when running in a context where partial FX coverage is acceptable
    (e.g. the parsed-CSV write step, which only needs translation as
    a courtesy for inspection).
    """
    if not statements:
        return statements
    needed = _needed_fx_pairs(statements)
    if not needed:
        return statements
    provider = provider or build_price_provider()
    rates = _fetch_rates(provider, needed)
    if strict:
        gaps = _missing_fx_coverage(needed, rates)
        if gaps:
            raise MissingFxRateError(
                _format_fx_gap_message(gaps),
                context={"missing_pairs": _gaps_to_context(gaps)},
            )
    earliest_dates = _earliest_appearance_per_pair(statements)
    return [_translate_one(stmt, rates, earliest_dates) for stmt in statements]


def _missing_fx_coverage(
    needed: dict[tuple[str, str], list[date]],
    rates: dict[tuple[str, str], pd.Series],
) -> dict[tuple[str, str], list[date]]:
    """Return ``(pair -> [dates without an FX rate within tolerance])``.

    A date is considered covered when the cached series has an
    observation on or within ``_MAX_FX_RATE_GAP_DAYS`` days before the
    target -- enough to bridge weekends and exchange holidays. The
    returned dict only contains pairs that have at least one
    uncovered date so callers can ``if gaps:`` cheaply.
    """
    gaps: dict[tuple[str, str], list[date]] = defaultdict(list)
    for pair, dates in needed.items():
        series = rates.get(pair)
        for target in sorted(set(dates)):
            if series is None or series.empty:
                gaps[pair].append(target)
                continue
            target_ts = pd.Timestamp(target)
            eligible = series[series.index <= target_ts]
            if eligible.empty:
                gaps[pair].append(target)
                continue
            latest = eligible.index.max()
            if (target_ts - latest).days > _MAX_FX_RATE_GAP_DAYS:
                gaps[pair].append(target)
    return dict(gaps)


def _format_fx_gap_message(gaps: dict[tuple[str, str], list[date]]) -> str:
    """Render an actionable retry message for a CLI / log line."""
    lines = ["Missing FX rates for one or more (currency, base, date) tuples:"]
    for (currency, base), dates in sorted(gaps.items()):
        sample = ", ".join(d.isoformat() for d in dates[:5])
        more = "" if len(dates) <= 5 else f" (+{len(dates) - 5} more)"
        lines.append(f"  {currency}->{base}: {sample}{more}")
    lines.extend(
        [
            "",
            "Likely cause: the active PriceProvider (yfinance default,",
            "with Stooq / Frankfurter fallbacks) rate-limited the request.",
            "",
            "Resolution: wait 10-20 minutes for the rate limit to clear",
            "and re-run the same report command. Cached FX rates from",
            "earlier successful runs persist under data_cache/, so the",
            "retry only fetches the dates listed above.",
            "",
            "Bypass: set SPA_PRICE_PROVIDER=offline before the run to",
            "skip the FX layer (this leaves non-base-currency holdings",
            "and transactions at their local-currency magnitudes, which",
            "will inflate every USD-denominated analytic by the missing",
            "FX factor -- DO NOT use this for a real audit).",
        ]
    )
    return "\n".join(lines)


def _gaps_to_context(
    gaps: dict[tuple[str, str], list[date]],
) -> list[tuple[str, str, str]]:
    """Flatten gaps to ``(currency, base, date)`` triples for structured logs."""
    return [
        (currency, base, target.isoformat())
        for (currency, base), dates in sorted(gaps.items())
        for target in sorted(set(dates))
    ]


def _earliest_appearance_per_pair(
    statements: list[Statement],
) -> dict[tuple[str, str], date]:
    """Map ``(account_label, symbol)`` -> earliest statement period_end seen."""
    out: dict[tuple[str, str], date] = {}
    for stmt in sorted(statements, key=lambda s: s.period_end):
        for holding in stmt.holdings:
            if holding.currency == stmt.base_currency:
                continue
            key = (stmt.account_label, holding.symbol)
            if key not in out:
                out[key] = stmt.period_end
    return out


def _needed_fx_pairs(statements: list[Statement]) -> dict[tuple[str, str], list[date]]:
    """Return the FX pairs that still need translation.

    A holding / transaction row is considered already translated when
    ``amount_base`` (or ``market_value_base`` for holdings) differs
    from its local counterpart -- the parser's raw output writes them
    equal so subsequent runs are idempotent. Both holdings and
    transactions are walked: turnover, dollar-PnL decomposition and
    any other per-transaction analytic reads ``amount_base`` and would
    otherwise see a HK$50,000 trade as US$50,000 (an ~8x overstatement
    on the user's HK book).
    """
    out: dict[tuple[str, str], list[date]] = defaultdict(list)
    for stmt in statements:
        for holding in stmt.holdings:
            if holding.currency == stmt.base_currency:
                continue
            if holding.market_value_local != holding.market_value_base:
                continue  # already translated by a previous pass
            out[(holding.currency, stmt.base_currency)].append(stmt.period_end)
        for txn in stmt.transactions:
            if txn.currency == stmt.base_currency:
                continue
            if txn.amount_local != txn.amount_base:
                continue  # already translated
            # ``trade_date`` is the right rate-lookup key; the FX pair
            # is the trade's currency vs the statement's base currency.
            out[(txn.currency, stmt.base_currency)].append(txn.trade_date)
    return out


def _fetch_rates(
    provider: PriceProvider,
    needed: dict[tuple[str, str], list[date]],
) -> dict[tuple[str, str], pd.Series]:
    rates: dict[tuple[str, str], pd.Series] = {}
    for (currency, base), dates in needed.items():
        start = min(dates) - timedelta(days=14)
        end = max(dates) + timedelta(days=2)
        try:
            series = provider.get_fx(currency, base, start, end)
        except Exception as exc:  # noqa: BLE001 - network is allowed to fail
            logger.warning(
                "FX series fetch failed",
                currency=currency,
                base=base,
                error=str(exc),
            )
            continue
        if series is None or series.empty:
            continue
        rates[(currency, base)] = series.sort_index()
    return rates


def _translate_one(
    stmt: Statement,
    rates: dict[tuple[str, str], pd.Series],
    earliest_dates: dict[tuple[str, str], date],
) -> Statement:
    if not stmt.holdings and not stmt.transactions:
        return stmt
    new_holdings, holdings_mutated = _translate_holdings(stmt, rates, earliest_dates)
    new_transactions, txn_mutated = _translate_transactions(stmt, rates)
    if not holdings_mutated and not txn_mutated:
        return stmt
    update: dict[str, object] = {}
    if holdings_mutated:
        update["holdings"] = tuple(new_holdings)
    if txn_mutated:
        update["transactions"] = tuple(new_transactions)
    return stmt.model_copy(update=update)


def _translate_holdings(
    stmt: Statement,
    rates: dict[tuple[str, str], pd.Series],
    earliest_dates: dict[tuple[str, str], date],
) -> tuple[list[Holding], bool]:
    new_holdings: list[Holding] = []
    mutated = False
    for holding in stmt.holdings:
        if holding.currency == stmt.base_currency:
            new_holdings.append(holding)
            continue
        if holding.market_value_local != holding.market_value_base:
            new_holdings.append(holding)
            continue
        pair_series = rates.get((holding.currency, stmt.base_currency))
        mv_rate = _rate_at(pair_series, stmt.period_end)
        if mv_rate is None:
            new_holdings.append(holding)
            continue
        acquisition_date = earliest_dates.get(
            (stmt.account_label, holding.symbol), stmt.period_end
        )
        cb_rate = _rate_at(pair_series, acquisition_date) or mv_rate
        mutated = True
        new_holdings.append(_apply_rates(holding, mv_rate=mv_rate, cb_rate=cb_rate))
    return new_holdings, mutated


def _translate_transactions(
    stmt: Statement,
    rates: dict[tuple[str, str], pd.Series],
) -> tuple[list[Transaction], bool]:
    """Multiply each non-base-currency transaction's amount by the FX rate
    at its ``trade_date``.

    Touches ``amount_base``, ``commission`` and ``fees``. ``amount_local``
    is left untouched so we can detect re-runs (idempotent guard).
    ``realized_pnl_base`` is also FX-translated when present because
    Schwab 1099-B disposition rows and IBKR Trades realized columns are
    both denominated in the trade's currency (USD on the user's
    statements today, so the guard is a no-op for them).
    """
    new_transactions: list[Transaction] = []
    mutated = False
    for txn in stmt.transactions:
        if txn.currency == stmt.base_currency:
            new_transactions.append(txn)
            continue
        if txn.amount_local != txn.amount_base:
            # Already translated; preserve idempotency.
            new_transactions.append(txn)
            continue
        pair_series = rates.get((txn.currency, stmt.base_currency))
        rate = _rate_at(pair_series, txn.trade_date)
        if rate is None:
            new_transactions.append(txn)
            continue
        mutated = True
        rate_dec = Decimal(str(rate))
        update: dict[str, Decimal | None] = {
            "amount_base": (txn.amount_local * rate_dec).quantize(_DECIMAL_QUANT),
        }
        if txn.commission != 0:
            update["commission"] = (txn.commission * rate_dec).quantize(_DECIMAL_QUANT)
        if txn.fees != 0:
            update["fees"] = (txn.fees * rate_dec).quantize(_DECIMAL_QUANT)
        if txn.realized_pnl_base is not None:
            update["realized_pnl_base"] = (
                txn.realized_pnl_base * rate_dec
            ).quantize(_DECIMAL_QUANT)
        new_transactions.append(txn.model_copy(update=update))
    return new_transactions, mutated


def _apply_rates(holding: Holding, *, mv_rate: float, cb_rate: float) -> Holding:
    mv_dec = Decimal(str(mv_rate))
    cb_dec = Decimal(str(cb_rate))
    update: dict[str, Decimal] = {
        "market_value_base": (holding.market_value_local * mv_dec).quantize(_DECIMAL_QUANT),
    }
    if holding.cost_basis_local is not None:
        update["cost_basis_base"] = (holding.cost_basis_local * cb_dec).quantize(_DECIMAL_QUANT)
    return holding.model_copy(update=update)


def _rate_at(series: pd.Series | None, target: date) -> float | None:
    if series is None or series.empty:
        return None
    target_ts = pd.Timestamp(target)
    eligible = series[series.index <= target_ts]
    if eligible.empty:
        return float(series.iloc[0])
    return float(eligible.iloc[-1])
