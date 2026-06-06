# SPDX-License-Identifier: MIT
"""Historical close-price helper for month-end mark-to-market.

The concentration analytic (and any future analytic that needs a per-
month portfolio state) reconstructs quantity held at each month-end via
:func:`stock_portfolio_auditor.analytics.holdings_replay.replay_account_quantities`
and then needs to price those quantities at the *month-end market
close*. This module wraps the existing :class:`PriceProvider` chain
(yfinance / Stooq / Frankfurter, with a parquet cache) so we can fetch
one daily close series per ticker for the whole audit window and look
up month-ends locally.

A separate module exists because :mod:`fx_reconstruction` is hard-coded
to FX-only (it does pair-level series fetching keyed by
``(currency, base)``); reusing it for stock close fetching would have
required tangling the FX-specific normalisation in. The interface here
mirrors :func:`fx_reconstruction._rate_at` for consistency.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

import pandas as pd
from loguru import logger

from stock_portfolio_auditor.pricing.providers.base import PriceProvider
from stock_portfolio_auditor.pricing.providers.factory import build_price_provider


def fetch_close_series(
    symbols: Iterable[str],
    *,
    start: date,
    end: date,
    provider: PriceProvider | None = None,
) -> dict[str, pd.Series]:
    """Return ``{ticker: close_series}`` for the symbols over ``[start, end]``.

    One network round-trip per ticker (modulo cache hits). Tickers
    whose download fails are omitted from the returned dict so callers
    can detect the gap by ``ticker not in series``. Caller-side ticker
    normalisation (Schwab ``BRKB`` -> Yahoo ``BRK-B``, etc.) is done by
    :func:`yahoo_ticker`.
    """
    provider = provider or build_price_provider()
    out: dict[str, pd.Series] = {}
    # Pad the window slightly so a month-end on a weekend or US holiday
    # can fall back to the previous business-day close.
    fetch_start = start - timedelta(days=14)
    fetch_end = end + timedelta(days=2)
    for symbol in dict.fromkeys(symbols):  # de-dup, preserve order
        yticker = yahoo_ticker(symbol)
        try:
            series = provider.get_close(yticker, fetch_start, fetch_end)
        except Exception as exc:  # noqa: BLE001 - network is allowed to fail
            logger.warning(
                "close series fetch failed",
                symbol=symbol,
                yahoo_ticker=yticker,
                error=str(exc),
            )
            continue
        if series is None or series.empty:
            continue
        out[symbol] = series.sort_index()
    return out


def close_at(series: pd.Series | None, target: date) -> float | None:
    """Last close at or before ``target``; ``None`` when the series is empty.

    Mirrors ``fx_reconstruction._rate_at`` so callers reading both
    pricing and FX series can treat them identically.
    """
    if series is None or series.empty:
        return None
    target_ts = pd.Timestamp(target)
    eligible = series[series.index <= target_ts]
    if eligible.empty:
        # Target precedes the entire series. Fall back to the first
        # available close so we don't drop the position entirely; the
        # caller knows the audit window starts before the series.
        return float(series.iloc[0])
    return float(eligible.iloc[-1])


def yahoo_ticker(symbol: str) -> str:
    """Translate a broker ticker to its Yahoo-Finance equivalent.

    The conventions actually observed in the corpus are limited; this
    function therefore handles a small set explicitly rather than
    trying to be exhaustive:

    * Class-share tickers written as ``BRK.B`` or ``BRK/B`` -> ``BRK-B``
      (Yahoo's separator for share classes is a hyphen).
    * 4-letter Schwab tickers ending in ``B`` for a class-B share
      (``BRKB``) are not auto-rewritten because that would mis-handle
      regular tickers like ``GOOGL``. The corpus's actual Class-B
      holdings come through as ``BRK.B`` from Schwab's modern
      statement format, which is handled by the first rule.
    * HK tickers (``3347.HK``) and US tickers without share classes
      pass through unchanged -- Yahoo accepts both verbatim.
    """
    upper = symbol.upper()
    # Replace ``.`` or ``/`` share-class separators with Yahoo's ``-``,
    # but keep ``.HK`` / ``.SS`` / ``.SZ`` / ``.L`` exchange suffixes intact.
    if "/" in upper:
        return upper.replace("/", "-")
    if "." in upper and not _is_exchange_suffix(upper):
        head, _, tail = upper.rpartition(".")
        return f"{head}-{tail}"
    # Schwab condenses Class B tickers without separator (``BRKB``).
    # Without a manifest we can't tell ``BRKB`` (a class-B share) from
    # a real 4-letter ticker, so we leave it; downstream price fetch
    # will fail and the symbol will simply be missing from the
    # concentration weights with a warning logged.
    return upper


def _is_exchange_suffix(symbol: str) -> bool:
    """True when the trailing ``.XX`` token denotes an exchange listing.

    Yahoo's exchange-suffix conventions are documented at
    https://help.yahoo.com/kb/finance-for-web/SLN2310.html ; we list
    the ones likely to appear in this corpus and the broader US/HK
    fundamental universe.
    """
    if "." not in symbol:
        return False
    suffix = symbol.rpartition(".")[2]
    return suffix in {
        "HK",  # Hong Kong (HKEX)
        "SS",  # Shanghai
        "SZ",  # Shenzhen
        "L",  # London
        "T",  # Tokyo
        "TO",  # Toronto
        "V",  # TSX Venture
        "PA",  # Paris (Euronext)
        "AS",  # Amsterdam
        "DE",  # Xetra
        "MI",  # Milan
        "MC",  # Madrid
        "SW",  # Switzerland
        "AX",  # Australia
        "NZ",  # New Zealand
        "SG",  # Singapore (SGX)
        "KS",  # Korea
        "TW",  # Taiwan
        "BO",  # Bombay
        "NS",  # NSE India
        "SA",  # São Paulo
        "MX",  # Mexico
        "BR",  # Brussels
    }
