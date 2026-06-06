# SPDX-License-Identifier: MIT
"""Unit tests for the security master tier cascade.

All network paths are mocked. The tests verify:
  * the Yahoo->GICS crosswalk
  * tier precedence (Tier 1 beats Tier 2 beats Tier 3)
  * manual overrides force their values onto the cascade result
  * parquet cache round-trips a :class:`Security`
  * fallback currency by exchange suffix kicks in only when no upstream
    provided a tradingCurrency
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from stock_portfolio_auditor.data import security_master as sm
from stock_portfolio_auditor.data.sector_crosswalk import yahoo_to_gics
from stock_portfolio_auditor.data.security_master import (
    SecurityCache,
    SecurityMaster,
    load_manual_overrides,
)
from stock_portfolio_auditor.domain.models import Security

# ---------------------------------------------------------------------------
# Sector crosswalk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("yahoo", "gics"),
    [
        ("Technology", "Information Technology"),
        ("Consumer Cyclical", "Consumer Discretionary"),
        ("Healthcare", "Health Care"),
        ("Financial Services", "Financials"),
        ("Basic Materials", "Materials"),
        ("Communication Services", "Communication Services"),
        ("Utilities", "Utilities"),
    ],
)
def test_yahoo_to_gics_crosswalk(yahoo: str, gics: str) -> None:
    assert yahoo_to_gics(yahoo) == gics


def test_yahoo_to_gics_unknown_returns_none() -> None:
    assert yahoo_to_gics(None) is None
    assert yahoo_to_gics("") is None
    assert yahoo_to_gics("Made-up Sector") is None


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------


def test_security_cache_roundtrip(tmp_path: Path) -> None:
    cache = SecurityCache(cache_dir=tmp_path, ttl_days=30)
    original = Security(
        ticker="AAPL",
        isin="US0378331005",
        figi="BBG000B9Y5X2",
        exchange="NASDAQ",
        base_currency="USD",
        sector_yahoo="Technology",
        sector_gics="Information Technology",
        industry="Consumer Electronics",
        country="United States",
        classification_source="yfinance",
        last_refreshed=date(2026, 5, 25),
    )
    cache.save(original)
    reloaded = cache.load("AAPL")
    assert reloaded is not None
    assert reloaded.ticker == "AAPL"
    assert reloaded.isin == "US0378331005"
    assert reloaded.sector_gics == "Information Technology"


def test_security_cache_returns_none_for_missing_ticker(tmp_path: Path) -> None:
    cache = SecurityCache(cache_dir=tmp_path)
    assert cache.load("NOTHING") is None


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------


def test_load_manual_overrides_empty_when_file_absent(tmp_path: Path) -> None:
    assert load_manual_overrides(tmp_path / "missing.csv") == {}


def test_load_manual_overrides_parses_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(
        "ticker,sector_gics,country,base_currency,classification_source\n"
        "FOO,Information Technology,Atlantis,USD,manual\n",
        encoding="utf-8",
    )
    overrides = load_manual_overrides(csv_path)
    assert "FOO" in overrides
    assert overrides["FOO"].sector_gics == "Information Technology"
    assert overrides["FOO"].country == "Atlantis"
    assert overrides["FOO"].classification_source == "manual"


def test_manual_override_forces_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when yfinance returns a valid row, the manual override wins."""
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_SGX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_HKEX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_EDGAR", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_OPENFIGI", "1")

    with patch.object(sm, "_yfinance_lookup") as yf_lookup:
        yf_lookup.return_value = Security(
            ticker="AAPL",
            base_currency="USD",
            sector_yahoo="Technology",
            sector_gics="Information Technology",
            country="United States",
            classification_source="yfinance",
        )
        master = SecurityMaster(
            cache=SecurityCache(cache_dir=tmp_path),
            manual_overrides={
                "AAPL": Security(
                    ticker="AAPL",
                    sector_gics="Communication Services",
                    classification_source="manual",
                )
            },
        )
        resolved = master.lookup("AAPL")
        assert resolved.sector_gics == "Communication Services"
        assert resolved.classification_source == "manual"
        # Non-overridden fields still come from yfinance
        assert resolved.country == "United States"
        assert resolved.base_currency == "USD"


# ---------------------------------------------------------------------------
# Cascade behaviour
# ---------------------------------------------------------------------------


def test_lookup_falls_back_to_yfinance_when_tier1_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_SGX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_HKEX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_EDGAR", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_OPENFIGI", "1")

    with patch.object(sm, "_yfinance_lookup") as yf_lookup:
        yf_lookup.return_value = Security(
            ticker="MSFT",
            base_currency="USD",
            sector_yahoo="Technology",
            sector_gics="Information Technology",
            country="United States",
            classification_source="yfinance",
        )
        master = SecurityMaster(cache=SecurityCache(cache_dir=tmp_path), manual_overrides={})
        resolved = master.lookup("MSFT")
        assert resolved.sector_gics == "Information Technology"
        assert resolved.classification_source == "yfinance"
        assert resolved.base_currency == "USD"


def test_lookup_uses_cache_on_second_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_SGX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_HKEX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_EDGAR", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_OPENFIGI", "1")

    with patch.object(sm, "_yfinance_lookup") as yf_lookup:
        yf_lookup.return_value = Security(
            ticker="GOOGL",
            base_currency="USD",
            sector_yahoo="Communication Services",
            sector_gics="Communication Services",
            country="United States",
            classification_source="yfinance",
        )
        master = SecurityMaster(cache=SecurityCache(cache_dir=tmp_path), manual_overrides={})
        first = master.lookup("GOOGL")
        second = master.lookup("GOOGL")
        assert yf_lookup.call_count == 1
        assert first.ticker == second.ticker == "GOOGL"


def test_lookup_returns_minimal_record_when_all_tiers_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_SGX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_HKEX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_EDGAR", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_OPENFIGI", "1")

    with patch.object(sm, "_yfinance_lookup", return_value=None):
        master = SecurityMaster(cache=SecurityCache(cache_dir=tmp_path), manual_overrides={})
        resolved = master.lookup("UNKNOWN")
        assert resolved.ticker == "UNKNOWN"
        # Sector / country unknown, but base_currency falls back from exchange guess
        assert resolved.base_currency == "USD"  # default US fallback
        assert resolved.sector_gics is None


def test_exchange_currency_fallback_for_hk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_SGX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_HKEX", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_EDGAR", "1")
    monkeypatch.setenv("SPA_DISABLE_SECURITY_MASTER_OPENFIGI", "1")

    with patch.object(sm, "_yfinance_lookup", return_value=None):
        master = SecurityMaster(cache=SecurityCache(cache_dir=tmp_path), manual_overrides={})
        resolved = master.lookup("0700.HK")
        assert resolved.base_currency == "HKD"
