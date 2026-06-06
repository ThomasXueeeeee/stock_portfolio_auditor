# SPDX-License-Identifier: MIT
"""FX attribution helpers."""

from __future__ import annotations

from dataclasses import dataclass

from stock_portfolio_auditor.domain.models import Statement


@dataclass(frozen=True, slots=True)
class CurrencyExposure:
    """Currency exposure and FX contribution summary."""

    currency: str
    average_exposure_base: float
    fx_pnl_base: float
    contribution_bps: float


def cash_fx_attribution(
    statements: list[Statement], *, average_nav: float
) -> tuple[CurrencyExposure, ...]:
    """Aggregate broker-reported FX translation PnL on cash balances by currency."""
    totals: dict[str, float] = {}
    exposures: dict[str, float] = {}
    for statement in statements:
        for balance in statement.cash_balances:
            if balance.currency == statement.base_currency:
                continue
            exposures[balance.currency] = (
                exposures.get(balance.currency, 0.0)
                + (float(balance.starting) + float(balance.ending)) / 2.0
            )
            if balance.fx_translation_pnl is not None:
                totals[balance.currency] = totals.get(balance.currency, 0.0) + float(
                    balance.fx_translation_pnl
                )

    return tuple(
        CurrencyExposure(
            currency=currency,
            average_exposure_base=exposures.get(currency, 0.0),
            fx_pnl_base=amount,
            contribution_bps=0.0 if average_nav == 0 else amount / average_nav * 10_000,
        )
        for currency, amount in sorted(totals.items())
    )


def allocate_cross_term_to_fx(local_return: float, fx_return: float) -> float:
    """Return FX contribution including local-return x FX-return cross term."""
    base_return = (1.0 + local_return) * (1.0 + fx_return) - 1.0
    return base_return - local_return
