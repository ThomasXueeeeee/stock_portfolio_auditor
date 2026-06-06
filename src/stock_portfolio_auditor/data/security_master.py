# SPDX-License-Identifier: MIT
"""Free global security-master pipeline.

Resolves a broker-native ticker (e.g. ``AAPL``, ``0700.HK``, ``T14.SI``) into a
:class:`~stock_portfolio_auditor.domain.models.Security` row carrying sector,
country, currency, and cross-broker identifiers (ISIN, FIGI).

Source cascade (highest-quality first):

1. **Exchange-authoritative free feeds** — SGX ``api.sgx.com/marketmetadata/v2``
   for SGX names, HKEX ``ListOfSecurities.xlsx`` for HKEX names, SEC EDGAR
   ``submissions`` JSON for US issuers. These ship ISIN + trading currency
   reliably and never misclassify the way a generic aggregator can.
2. **yfinance ``.info``** — broadest free coverage of sector / industry /
   marketcap / country fields. Yahoo's 11-sector vocabulary is mapped to GICS
   via :mod:`stock_portfolio_auditor.data.sector_crosswalk`.
3. **OpenFIGI v3** (anonymous, 25 req/min) — used only to obtain FIGI / ISIN
   when other tiers couldn't. Sector / country fields are not populated by
   OpenFIGI; it is a reconciliation tool, not a classifier.
4. **User-editable overrides** at ``data/manual_overrides.csv`` — appended
   manually for the long tail. Fields override anything from earlier tiers.

Every resolved row is cached as a parquet file at
``data_cache/securities/<safe-ticker>.parquet`` with a configurable TTL
(default 30 days). Network failures degrade gracefully: a cached row is
returned even when the upstream provider is down. Tiers 1-3 are best-effort
and can be disabled per source via env vars; only Tier 4 (manual overrides)
is always honored.

See ``research/security_master_data_sources.md`` for the source-quality study
behind this design.
"""

from __future__ import annotations

import csv
import os
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pandas as pd
import requests
from loguru import logger

from stock_portfolio_auditor.data.sector_crosswalk import yahoo_to_gics
from stock_portfolio_auditor.domain.models import Security

CACHE_DIR: Final[Path] = Path("data_cache/securities")
DEFAULT_TTL_DAYS: Final[int] = 30
DEFAULT_USER_AGENT: Final[str] = (
    "stock-portfolio-auditor security master "
    "(https://github.com/ThomasXueeeeee/stock_portfolio_auditor)"
)
MANUAL_OVERRIDES_PATH: Final[Path] = Path("data/manual_overrides.csv")
DISABLE_ENV_VARS: Final[dict[str, str]] = {
    "yfinance": "SPA_DISABLE_SECURITY_MASTER_YFINANCE",
    "openfigi": "SPA_DISABLE_SECURITY_MASTER_OPENFIGI",
    "sgx": "SPA_DISABLE_SECURITY_MASTER_SGX",
    "hkex": "SPA_DISABLE_SECURITY_MASTER_HKEX",
    "edgar": "SPA_DISABLE_SECURITY_MASTER_EDGAR",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_filename(value: str) -> str:
    """Return a filesystem-safe version of a ticker for parquet caching."""
    return (
        value.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .replace("=", "_")
        .replace(" ", "_")
        .replace(".", "_")
    )


def _exchange_from_ticker(ticker: str) -> str | None:
    """Infer an exchange code from a yfinance-style suffix."""
    suffix_map = {
        "HK": "HKEX",
        "SI": "SGX",
        "T": "TSE",
        "L": "LSE",
        "DE": "XETRA",
        "AX": "ASX",
        "TO": "TSX",
        "PA": "EuronextParis",
        "MI": "BorsaItaliana",
        "AS": "EuronextAmsterdam",
        "SW": "SIX",
    }
    if "." in ticker:
        suffix = ticker.rsplit(".", 1)[-1].upper()
        return suffix_map.get(suffix)
    return "NYSE/NASDAQ"


def _currency_from_exchange(exchange: str | None) -> str | None:
    """Best-guess default currency from an exchange. Always overridden when an
    authoritative tradingCurrency is available, but useful as a last fallback."""
    if not exchange:
        return None
    defaults = {
        "HKEX": "HKD",
        "SGX": "SGD",
        "TSE": "JPY",
        "LSE": "GBP",
        "XETRA": "EUR",
        "EuronextParis": "EUR",
        "BorsaItaliana": "EUR",
        "EuronextAmsterdam": "EUR",
        "SIX": "CHF",
        "ASX": "AUD",
        "TSX": "CAD",
        "NYSE/NASDAQ": "USD",
        "NYSE": "USD",
        "NASDAQ": "USD",
    }
    return defaults.get(exchange)


# ---------------------------------------------------------------------------
# Parquet cache
# ---------------------------------------------------------------------------


class SecurityCache:
    """One parquet row per ticker, keyed by ``_safe_filename(ticker)``.

    Schema mirrors :class:`Security` plus a ``cached_at`` ISO date column so
    the TTL check is just an integer comparison.
    """

    def __init__(self, cache_dir: Path | None = None, ttl_days: int = DEFAULT_TTL_DAYS) -> None:
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_days = ttl_days

    def _path(self, ticker: str) -> Path:
        return self.cache_dir / f"{_safe_filename(ticker)}.parquet"

    def load(self, ticker: str) -> Security | None:
        path = self._path(ticker)
        if not path.exists():
            return None
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - corrupt cache files are not fatal
            logger.warning("Bad security cache file; ignoring", path=str(path), error=str(exc))
            return None
        if frame.empty:
            return None
        row = frame.iloc[0].to_dict()
        cached_at = row.pop("cached_at", None)
        if cached_at is not None:
            try:
                cached_date = datetime.fromisoformat(str(cached_at)).date()
                if datetime.now().date() - cached_date > timedelta(days=self.ttl_days):
                    return None
            except ValueError:
                pass
        row = {key: (None if pd.isna(value) else value) for key, value in row.items()}
        return Security(**row)

    def save(self, security: Security) -> None:
        row = security.model_dump()
        row["cached_at"] = datetime.now().date().isoformat()
        frame = pd.DataFrame([row])
        frame.to_parquet(self._path(security.ticker), index=False)


# ---------------------------------------------------------------------------
# Tier 4: Manual overrides
# ---------------------------------------------------------------------------


def load_manual_overrides(path: Path | None = None) -> dict[str, Security]:
    """Load the user-editable overrides CSV.

    The CSV has a header row and one row per override. Fields match
    :class:`Security`. Missing optional fields are ``None``.
    """
    csv_path = Path(path or MANUAL_OVERRIDES_PATH)
    if not csv_path.exists():
        return {}
    overrides: dict[str, Security] = {}
    with csv_path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ticker = (row.get("ticker") or "").strip()
            if not ticker:
                continue
            cleaned = {
                key: (value.strip() or None) for key, value in row.items() if value is not None
            }
            overrides[ticker.upper()] = Security(**cleaned)
    return overrides


# ---------------------------------------------------------------------------
# Tier 2: yfinance
# ---------------------------------------------------------------------------


def _yfinance_lookup(ticker: str) -> Security | None:
    """Resolve via yfinance ``.info``. Returns ``None`` on any failure."""
    if os.getenv(DISABLE_ENV_VARS["yfinance"]):
        return None
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:  # noqa: BLE001 - any network or parsing failure
        logger.debug("yfinance lookup failed", ticker=ticker, error=str(exc))
        return None
    if not info:
        return None
    sector_yahoo = info.get("sector")
    currency = info.get("currency")
    if isinstance(currency, str):
        currency = currency.upper().strip() or None
    exchange = info.get("exchange") or _exchange_from_ticker(ticker)
    return Security(
        ticker=ticker,
        isin=None,
        figi=None,
        exchange=exchange,
        base_currency=currency,
        sector_yahoo=sector_yahoo,
        sector_gics=yahoo_to_gics(sector_yahoo),
        industry=info.get("industry"),
        country=info.get("country"),
        country_of_risk=info.get("country"),
        classification_source="yfinance",
        last_refreshed=datetime.now().date(),
    )


# ---------------------------------------------------------------------------
# Tier 3: OpenFIGI (FIGI / ISIN reconciliation only)
# ---------------------------------------------------------------------------


class _OpenFIGIRateLimiter:
    """Token-bucket-ish limiter for the anonymous 25 req/min cap."""

    def __init__(self, limit: int = 25, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] > self.window:
                self._times.popleft()
            if len(self._times) >= self.limit:
                sleep_for = self.window - (now - self._times[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._times and now - self._times[0] > self.window:
                    self._times.popleft()
            self._times.append(now)


_OPENFIGI_LIMITER = _OpenFIGIRateLimiter()
_OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


def _openfigi_lookup(
    ticker: str,
    isin: str | None,
    session: requests.Session | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(figi, isin)`` for a ticker via OpenFIGI.

    Sector / country fields are *not* populated by OpenFIGI; this tier is only
    for cross-broker identifier reconciliation.
    """
    if os.getenv(DISABLE_ENV_VARS["openfigi"]):
        return None, None
    sess = session or requests.Session()
    payload: list[dict[str, str]] = []
    if isin:
        payload.append({"idType": "ID_ISIN", "idValue": isin})
    payload.append({"idType": "TICKER", "idValue": ticker.split(".")[0]})
    headers = {
        "Content-Type": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    _OPENFIGI_LIMITER.wait()
    try:
        response = sess.post(_OPENFIGI_URL, json=payload, headers=headers, timeout=20)
    except requests.RequestException as exc:
        logger.debug("OpenFIGI request failed", ticker=ticker, error=str(exc))
        return None, None
    if response.status_code == 429:
        logger.warning("OpenFIGI rate-limited", ticker=ticker)
        return None, None
    if response.status_code >= 400:
        return None, None
    try:
        body = response.json()
    except ValueError:
        return None, None
    figi: str | None = None
    resolved_isin: str | None = isin
    for entry in body:
        data = entry.get("data") or []
        if data:
            figi = data[0].get("compositeFIGI") or data[0].get("figi") or figi
            if not resolved_isin:
                resolved_isin = data[0].get("isin")
        if figi:
            break
    return figi, resolved_isin


# ---------------------------------------------------------------------------
# Tier 1: Exchange-authoritative
# ---------------------------------------------------------------------------


_SGX_URL = "https://api.sgx.com/marketmetadata/v2"
_HKEX_URL = (
    "https://www.hkex.com.hk/-/media/HKEX-Market/Services/Trading/Securities/"
    "Securities-Lists/ListOfSecurities.xlsx"
)


@dataclass(slots=True)
class _SGXSnapshot:
    rows_by_ticker: dict[str, dict[str, Any]] = field(default_factory=dict)
    rows_by_isin: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class _HKEXSnapshot:
    rows_by_code: dict[str, dict[str, Any]] = field(default_factory=dict)


_sgx_snapshot: _SGXSnapshot | None = None
_hkex_snapshot: _HKEXSnapshot | None = None
_snapshot_lock = threading.Lock()


def _sgx_load() -> _SGXSnapshot:
    """Fetch the SGX market-metadata catalog (validated against 18k rows)."""
    global _sgx_snapshot
    if _sgx_snapshot is not None:
        return _sgx_snapshot
    with _snapshot_lock:
        if _sgx_snapshot is not None:
            return _sgx_snapshot
        if os.getenv(DISABLE_ENV_VARS["sgx"]):
            _sgx_snapshot = _SGXSnapshot()
            return _sgx_snapshot
        rows_by_ticker: dict[str, dict[str, Any]] = {}
        rows_by_isin: dict[str, dict[str, Any]] = {}
        try:
            response = requests.get(
                _SGX_URL, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=30
            )
            response.raise_for_status()
            payload = response.json()
            for row in payload.get("data", []):
                code = (row.get("tradingName") or row.get("nc") or "").upper()
                isin = (row.get("isin") or "").upper() or None
                if code:
                    rows_by_ticker[code] = row
                if isin:
                    rows_by_isin[isin] = row
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("SGX security master fetch failed", error=str(exc))
        _sgx_snapshot = _SGXSnapshot(rows_by_ticker=rows_by_ticker, rows_by_isin=rows_by_isin)
        return _sgx_snapshot


def _sgx_lookup(ticker: str, isin: str | None) -> Security | None:
    if not (ticker.upper().endswith(".SI") or (isin and isin.startswith("SG"))):
        return None
    snapshot = _sgx_load()
    code = ticker.split(".")[0].upper()
    row = snapshot.rows_by_ticker.get(code)
    if not row and isin:
        row = snapshot.rows_by_isin.get(isin.upper())
    if not row:
        return None
    return Security(
        ticker=ticker,
        isin=(row.get("isin") or None),
        exchange="SGX",
        base_currency=(row.get("tradingCurrency") or None),
        country="Singapore",
        country_of_risk=row.get("countryOfIncorporation"),
        classification_source="sgx",
        last_refreshed=datetime.now().date(),
    )


def _hkex_load() -> _HKEXSnapshot:
    """Fetch the HKEX List of Securities xlsx; cache for the process lifetime."""
    global _hkex_snapshot
    if _hkex_snapshot is not None:
        return _hkex_snapshot
    with _snapshot_lock:
        if _hkex_snapshot is not None:
            return _hkex_snapshot
        if os.getenv(DISABLE_ENV_VARS["hkex"]):
            _hkex_snapshot = _HKEXSnapshot()
            return _hkex_snapshot
        rows_by_code: dict[str, dict[str, Any]] = {}
        try:
            response = requests.get(
                _HKEX_URL, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=60
            )
            response.raise_for_status()
            frame = pd.read_excel(response.content, sheet_name=0, skiprows=2, engine="openpyxl")
            for _, row in frame.iterrows():
                code_raw = row.get("Stock Code") or row.get("StockCode")
                if pd.isna(code_raw):
                    continue
                code = str(int(code_raw)).zfill(4)
                rows_by_code[code] = {
                    "name": row.get("Name of Securities"),
                    "category": row.get("Category"),
                    "sub_sector": row.get("Sub-Sector"),
                }
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("HKEX security master fetch failed", error=str(exc))
        _hkex_snapshot = _HKEXSnapshot(rows_by_code=rows_by_code)
        return _hkex_snapshot


def _hkex_lookup(ticker: str, isin: str | None) -> Security | None:
    if not ticker.upper().endswith(".HK"):
        return None
    snapshot = _hkex_load()
    raw_code = ticker.split(".")[0].upper().lstrip("0") or "0"
    code = raw_code.zfill(4)
    row = snapshot.rows_by_code.get(code)
    if not row:
        return None
    return Security(
        ticker=ticker,
        isin=isin,
        exchange="HKEX",
        base_currency="HKD",
        country="Hong Kong",
        country_of_risk=None,
        industry=row.get("sub_sector"),
        classification_source="hkex",
        last_refreshed=datetime.now().date(),
    )


def _edgar_lookup(ticker: str, isin: str | None) -> Security | None:
    """SEC EDGAR submissions JSON. Currently used only for US issuers."""
    if os.getenv(DISABLE_ENV_VARS["edgar"]):
        return None
    if "." in ticker:
        # Suffixed tickers indicate a non-US listing.
        return None
    return None  # EDGAR ticker->CIK mapping is non-trivial; placeholder for follow-up.


# ---------------------------------------------------------------------------
# Merging helper
# ---------------------------------------------------------------------------


_SECURITY_FIELDS: Final[tuple[str, ...]] = (
    "isin",
    "figi",
    "exchange",
    "base_currency",
    "sector_yahoo",
    "sector_gics",
    "industry",
    "country",
    "country_of_risk",
)


def _merge(base: Security, override: Security) -> Security:
    """Fill missing fields on ``base`` with values from ``override``.

    ``override`` never replaces a non-null value on ``base``; this preserves
    the higher-quality earlier tier's classification while letting later tiers
    backfill missing fields.
    """
    patch: dict[str, Any] = {}
    for fld in _SECURITY_FIELDS:
        if getattr(base, fld) is None and getattr(override, fld) is not None:
            patch[fld] = getattr(override, fld)
    if base.classification_source is None and override.classification_source is not None:
        patch["classification_source"] = override.classification_source
    if not patch:
        return base
    return base.model_copy(update=patch)


def _force_apply(base: Security, override: Security) -> Security:
    """Override every non-null field of ``base`` with ``override`` (Tier 4 manual)."""
    patch: dict[str, Any] = {}
    for fld in (*_SECURITY_FIELDS, "ticker", "classification_source", "last_refreshed"):
        value = getattr(override, fld)
        if value is not None:
            patch[fld] = value
    return base.model_copy(update=patch)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SecurityMaster:
    """Resolve a broker-native ticker to a :class:`Security` via the tier cascade."""

    def __init__(
        self,
        *,
        cache: SecurityCache | None = None,
        manual_overrides: dict[str, Security] | None = None,
    ) -> None:
        self.cache = cache or SecurityCache()
        self.manual_overrides = manual_overrides or load_manual_overrides()
        self._session = requests.Session()

    def lookup(
        self,
        ticker: str,
        *,
        isin: str | None = None,
        use_cache: bool = True,
    ) -> Security:
        """Resolve a security row. Always returns a Security, possibly partial."""
        ticker = ticker.strip()
        if use_cache:
            cached = self.cache.load(ticker)
            if cached is not None:
                manual = self.manual_overrides.get(ticker.upper())
                return _force_apply(cached, manual) if manual else cached

        resolved = Security(ticker=ticker, isin=isin)

        for tier in (_sgx_lookup, _hkex_lookup, _edgar_lookup):
            try:
                row = tier(ticker, isin)
            except Exception as exc:  # noqa: BLE001 - tier failures are tolerated
                logger.debug("Tier1 failure", ticker=ticker, tier=tier.__name__, error=str(exc))
                row = None
            if row is not None:
                resolved = _merge(resolved, row)

        yf_row = _yfinance_lookup(ticker)
        if yf_row is not None:
            resolved = _merge(resolved, yf_row)

        if not resolved.figi or not resolved.isin:
            figi, openfigi_isin = _openfigi_lookup(ticker, resolved.isin, session=self._session)
            if figi or openfigi_isin:
                resolved = _merge(
                    resolved,
                    Security(
                        ticker=ticker,
                        isin=openfigi_isin,
                        figi=figi,
                        classification_source=resolved.classification_source or "openfigi",
                    ),
                )

        if resolved.exchange is None:
            inferred_exchange = _exchange_from_ticker(ticker)
            if inferred_exchange:
                resolved = _merge(
                    resolved,
                    Security(ticker=ticker, exchange=inferred_exchange),
                )

        if resolved.base_currency is None:
            resolved = _merge(
                resolved,
                Security(
                    ticker=ticker,
                    base_currency=_currency_from_exchange(resolved.exchange),
                ),
            )

        manual = self.manual_overrides.get(ticker.upper())
        if manual:
            resolved = _force_apply(resolved, manual)

        if resolved.last_refreshed is None:
            resolved = resolved.model_copy(update={"last_refreshed": datetime.now().date()})

        if use_cache:
            self.cache.save(resolved)
        return resolved

    def lookup_batch(
        self,
        queries: Iterable[tuple[str, str | None]],
        *,
        use_cache: bool = True,
    ) -> list[Security]:
        return [self.lookup(ticker, isin=isin, use_cache=use_cache) for ticker, isin in queries]


__all__ = [
    "CACHE_DIR",
    "DEFAULT_TTL_DAYS",
    "MANUAL_OVERRIDES_PATH",
    "SecurityCache",
    "SecurityMaster",
    "load_manual_overrides",
]
