from __future__ import annotations

from stock_portfolio_auditor.pricing.currency_resolver import CurrencyResolver
from stock_portfolio_auditor.pricing.ticker_resolver import TickerResolver


def test_ticker_resolver_maps_hk_numeric_symbol() -> None:
    result = TickerResolver().resolve("1093", currency="HKD")
    assert result.ticker == "1093.HK"
    assert result.reason == "hk_numeric"


def test_ticker_resolver_maps_class_share() -> None:
    result = TickerResolver().resolve("BRK.B")
    assert result.ticker == "BRK-B"


def test_ticker_resolver_uses_override() -> None:
    result = TickerResolver({"ABC": "XYZ"}).resolve("ABC")
    assert result.ticker == "XYZ"
    assert result.reason == "manual_override"


def test_currency_resolver_prefers_broker_currency() -> None:
    assert CurrencyResolver().resolve("1093.HK", broker_currency="HKD") == "HKD"


def test_currency_resolver_uses_suffix_rules() -> None:
    assert CurrencyResolver().resolve("7203.T") == "JPY"
    assert CurrencyResolver().resolve("MC.PA") == "EUR"
