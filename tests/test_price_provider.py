from __future__ import annotations

from datetime import date

import pandas as pd

from stock_portfolio_auditor.pricing.providers.base import PriceProvider
from stock_portfolio_auditor.pricing.providers.cached import CachedProvider
from stock_portfolio_auditor.pricing.providers.chain import FallbackProvider


class DummyProvider(PriceProvider):
    name = "dummy"

    def __init__(self) -> None:
        self.calls = 0

    def get_close(self, ticker: str, start: date, end: date) -> pd.Series:
        self.calls += 1
        return pd.Series(
            [1.0, 2.0],
            index=pd.to_datetime([start, end]),
            name=ticker,
        )

    def get_splits(self, ticker: str, start: date, end: date) -> pd.Series:
        return pd.Series(dtype="float64")

    def get_fx(self, base: str, quote: str, start: date, end: date) -> pd.Series:
        return pd.Series(
            [1.0, 1.1],
            index=pd.to_datetime([start, end]),
            name=f"{base}{quote}",
        )


def test_cached_provider_reuses_existing_series(tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = DummyProvider()
    cached = CachedProvider(provider, cache_dir=tmp_path)

    start = date(2024, 1, 1)
    end = date(2024, 1, 2)
    first = cached.get_close("AAPL", start, end)
    second = cached.get_close("AAPL", start, end)

    assert provider.calls == 1
    assert first.equals(second)


def test_fallback_provider_uses_first_non_empty_provider() -> None:
    provider = DummyProvider()
    fallback = FallbackProvider((provider,))
    series = fallback.get_close("AAPL", date(2024, 1, 1), date(2024, 1, 2))
    assert len(series) == 2
