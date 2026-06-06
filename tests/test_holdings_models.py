# SPDX-License-Identifier: MIT
"""Sanity checks for Holding/Lot/Security domain models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.domain.models import AssetKind, Holding, Lot, Security


def test_holding_carries_identifier_fields() -> None:
    holding = Holding(
        symbol="0700",
        kind=AssetKind.EQUITY,
        currency="HKD",
        quantity=Decimal("100"),
        market_value_local=Decimal("65000"),
        market_value_base=Decimal("8300"),
        as_of=date(2026, 4, 30),
        isin="KYG875721634",
        figi="BBG000BFXVQ5",
        exchange="HKEX",
    )
    assert holding.isin == "KYG875721634"
    assert holding.figi == "BBG000BFXVQ5"
    assert holding.exchange == "HKEX"


def test_holding_identifier_fields_default_to_none() -> None:
    holding = Holding(
        symbol="AAPL",
        kind=AssetKind.EQUITY,
        currency="usd",
        quantity=Decimal("10"),
        market_value_local=Decimal("2000"),
        market_value_base=Decimal("2000"),
        as_of=date(2026, 4, 30),
    )
    assert holding.isin is None
    assert holding.figi is None
    assert holding.exchange is None
    assert holding.currency == "USD"


def test_security_normalizes_currency() -> None:
    security = Security(
        ticker="T14.SI",
        isin="SG2A41995944",
        figi="BBG000BHP9Z3",
        exchange="SGX",
        base_currency="usd",
        sector_yahoo="Basic Materials",
        sector_gics="Materials",
        industry="Steel",
        country="Singapore",
        classification_source="yfinance",
        last_refreshed=date(2026, 5, 25),
    )
    assert security.base_currency == "USD"
    assert security.sector_gics == "Materials"


def test_security_allows_missing_base_currency() -> None:
    security = Security(ticker="ZZZ", classification_source="manual")
    assert security.base_currency is None
    assert security.sector_yahoo is None


def test_lot_rejects_invalid_currency_length() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError type varies
        Lot(
            symbol="AAPL",
            acquired_date=date(2023, 6, 1),
            shares=Decimal("10"),
            cost_basis_local=Decimal("1500"),
            currency="DOLLARS",
        )


def test_lot_quantizes_and_uppercases() -> None:
    lot = Lot(
        symbol="0700",
        acquired_date=date(2024, 3, 1),
        shares="500",
        cost_basis_local="320000",
        currency="hkd",
    )
    assert lot.shares == Decimal("500.0000")
    assert lot.cost_basis_local == Decimal("320000.0000")
    assert lot.currency == "HKD"
