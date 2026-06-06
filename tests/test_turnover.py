# SPDX-License-Identifier: MIT
"""Unit tests for portfolio-turnover analytics.

The report's yearly summary reads from
:func:`monthly_two_way_turnover_series` -- per-month
``(buys + sells) / 2 / avg_NAV_month`` ratios. The yearly column on
each row sums the in-window months' ratios; the Total row averages
those yearly sums divided by years-in-window. Tests in this file pin
both the per-month math and the per-year aggregation.

The legacy SEC-min ``portfolio_turnover`` is also tested here because
it is kept on the public API for callers that explicitly want the Form
N-1A number.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_portfolio_auditor.analytics.pooled import PooledPeriod
from stock_portfolio_auditor.analytics.turnover import (
    monthly_two_way_turnover_series,
    portfolio_turnover,
    yearly_turnover_from_monthly,
)
from stock_portfolio_auditor.domain.models import AssetKind, Transaction
from tests.factories import make_statement


def _trade(
    *,
    action: str,
    amount_base: Decimal,
    trade_date: date,
    symbol: str = "AAPL",
    kind: AssetKind = AssetKind.EQUITY,
) -> Transaction:
    return Transaction(
        trade_date=trade_date,
        action=action,  # type: ignore[arg-type]
        symbol=symbol,
        kind=kind,
        currency="USD",
        amount_local=amount_base,
        amount_base=amount_base,
    )


def _period(period_end: date, beginning: float, ending: float) -> PooledPeriod:
    return PooledPeriod(
        period_end=period_end,
        beginning_value=beginning,
        ending_value=ending,
        external_cash_flow=0.0,
        period_return=0.0,
    )


# ---------------------------------------------------------------------------
# Monthly two-way turnover
# ---------------------------------------------------------------------------


def test_monthly_two_way_turnover_pure_rotation_matches_rotation_rate() -> None:
    """Sell $100 of A and buy $100 of B on a $1000 book = 10% rotation."""
    stmt = make_statement(
        account_label="A",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
    ).model_copy(
        update={
            "transactions": (
                _trade(action="sell", amount_base=Decimal("100"), trade_date=date(2024, 1, 10)),
                _trade(action="buy", amount_base=Decimal("-100"), trade_date=date(2024, 1, 20)),
            ),
        }
    )
    series = monthly_two_way_turnover_series(
        [stmt], [_period(date(2024, 1, 31), 1000.0, 1000.0)]
    )
    assert len(series) == 1
    # (100 + 100) / 2 / 1000 = 0.10
    assert series[0].ratio == pytest.approx(0.10)
    assert series[0].buys_base == 100.0
    assert series[0].sells_base == 100.0


def test_monthly_two_way_turnover_one_way_buying_captures_half_turnover() -> None:
    """A month with $200 of buys and zero sells reports 10% on $1000 NAV.

    The SEC formula would zero this out -- ``min(200, 0) = 0`` -- but
    for a personal portfolio that quarter the manager *did* deploy
    capital, so the two-way formula correctly captures it as
    ``(200 + 0) / 2 / 1000 = 10%``.
    """
    stmt = make_statement(
        account_label="A",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
    ).model_copy(
        update={
            "transactions": (
                _trade(action="buy", amount_base=Decimal("-200"), trade_date=date(2024, 1, 10)),
            ),
        }
    )
    series = monthly_two_way_turnover_series(
        [stmt], [_period(date(2024, 1, 31), 1000.0, 1000.0)]
    )
    assert series[0].ratio == pytest.approx(0.10)
    assert series[0].buys_base == 200.0
    assert series[0].sells_base == 0.0


def test_monthly_two_way_turnover_buckets_yearly_statement_by_trade_date() -> None:
    """A yearly statement's trades are bucketed into the right month.

    IBKR annual Activity Statements span Jan-Dec but each trade row
    has a date stamp. The monthly series must walk transactions by
    ``trade_date``, not by ``stmt.period_end``.
    """
    stmt = make_statement(
        account_label="A",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "transactions": (
                _trade(action="buy", amount_base=Decimal("-100"), trade_date=date(2024, 3, 15)),
                _trade(action="sell", amount_base=Decimal("100"), trade_date=date(2024, 3, 20)),
                _trade(action="buy", amount_base=Decimal("-50"), trade_date=date(2024, 6, 10)),
            ),
        }
    )
    periods = [
        _period(date(2024, 3, 31), 1000.0, 1000.0),
        _period(date(2024, 6, 30), 1000.0, 1000.0),
    ]
    series = monthly_two_way_turnover_series([stmt], periods)
    assert series[0].ratio == pytest.approx(0.10)  # March: 200/2/1000
    assert series[1].ratio == pytest.approx(0.025)  # June: 50/2/1000


def test_monthly_two_way_turnover_excludes_options_and_cash() -> None:
    """Option premium and cash sweeps must not register as portfolio trading."""
    stmt = make_statement(
        account_label="A",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
    ).model_copy(
        update={
            "transactions": (
                _trade(
                    action="buy",
                    amount_base=Decimal("-500"),
                    trade_date=date(2024, 1, 10),
                    kind=AssetKind.OPTION,
                    symbol="_OPT_AAPL",
                ),
                _trade(
                    action="sell",
                    amount_base=Decimal("500"),
                    trade_date=date(2024, 1, 20),
                    kind=AssetKind.CASH,
                ),
            ),
        }
    )
    series = monthly_two_way_turnover_series(
        [stmt], [_period(date(2024, 1, 31), 1000.0, 1000.0)]
    )
    assert series[0].ratio == 0.0
    assert series[0].buys_base == 0.0
    assert series[0].sells_base == 0.0


def test_monthly_two_way_turnover_reads_schwab_aggregate_when_no_per_trade_rows() -> None:
    """Schwab monthly statements publish aggregate Investments Purchased /
    Sold totals but don't enumerate per-trade stock rows. The turnover
    walker must read those aggregates so a quarter of pure buying
    (which would zero out under the SEC ``min(buys, sells)`` formula)
    still reports real activity.
    """
    stmt = make_statement(
        account_label="schwab",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
    ).model_copy(
        update={
            "stock_purchases_base": Decimal("400"),
            "stock_sales_base": Decimal("100"),
        }
    )
    series = monthly_two_way_turnover_series(
        [stmt], [_period(date(2024, 1, 31), 1000.0, 1000.0)]
    )
    assert series[0].buys_base == 400.0
    assert series[0].sells_base == 100.0
    # (400 + 100) / 2 / 1000 = 0.25
    assert series[0].ratio == pytest.approx(0.25)


def test_monthly_two_way_turnover_statement_aggregate_only_credited_to_exact_month() -> None:
    """A yearly statement with non-zero stock_purchases_base must not
    attribute its full-year aggregate to a single month -- the
    aggregate is only used when the statement covers exactly one
    calendar month.
    """
    stmt = make_statement(
        account_label="ibkr",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            # Deliberately set on a multi-month statement to verify
            # the period-exact guard.
            "stock_purchases_base": Decimal("5000"),
            "stock_sales_base": Decimal("5000"),
        }
    )
    series = monthly_two_way_turnover_series(
        [stmt], [_period(date(2024, 6, 30), 1000.0, 1000.0)]
    )
    # Aggregate must NOT contribute -- statement spans 12 months.
    assert series[0].ratio == 0.0


def test_monthly_two_way_turnover_handles_zero_nav() -> None:
    stmt = make_statement(
        account_label="A",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
    ).model_copy(
        update={
            "transactions": (
                _trade(action="buy", amount_base=Decimal("-100"), trade_date=date(2024, 1, 10)),
            ),
        }
    )
    series = monthly_two_way_turnover_series(
        [stmt], [_period(date(2024, 1, 31), 0.0, 0.0)]
    )
    assert series[0].ratio == 0.0


def test_monthly_two_way_turnover_weights_by_in_month_nav_not_yearly_avg() -> None:
    """A $20 trade in January on $100 NAV counts more than the same $20
    trade in December on $500 NAV.

    This is the conceptual reason for summing monthly ratios rather
    than aggregating yearly: rotation is relative to the NAV at the
    time the trade happens.
    """
    stmt_jan = make_statement(
        account_label="A",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        frequency="M",
    ).model_copy(
        update={
            "transactions": (
                _trade(action="buy", amount_base=Decimal("-20"), trade_date=date(2024, 1, 10)),
            ),
        }
    )
    stmt_dec = make_statement(
        account_label="A",
        start=date(2024, 12, 1),
        end=date(2024, 12, 31),
        frequency="M",
    ).model_copy(
        update={
            "transactions": (
                _trade(action="buy", amount_base=Decimal("-20"), trade_date=date(2024, 12, 10)),
            ),
        }
    )
    periods = [
        _period(date(2024, 1, 31), 100.0, 100.0),
        _period(date(2024, 12, 31), 500.0, 500.0),
    ]
    series = monthly_two_way_turnover_series([stmt_jan, stmt_dec], periods)
    # Jan ratio: (20 + 0) / 2 / 100 = 0.10
    # Dec ratio: (20 + 0) / 2 / 500 = 0.02
    assert series[0].ratio == pytest.approx(0.10)
    assert series[1].ratio == pytest.approx(0.02)
    # If we naively aggregated to yearly: (40 / 2) / avg(100, 500) = 20 / 300 = 0.067
    # That doesn't equal sum(monthly) = 0.12, demonstrating the difference.
    assert series[0].ratio + series[1].ratio == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# Yearly aggregation
# ---------------------------------------------------------------------------


def test_yearly_turnover_from_monthly_sums_in_window_months() -> None:
    """Summing twelve monthly ratios gives that year's rotation rate."""
    series = [
        _MonthlyTurnoverFactory(date(2024, 1, 31), 0.05),
        _MonthlyTurnoverFactory(date(2024, 6, 30), 0.10),
        _MonthlyTurnoverFactory(date(2024, 12, 31), 0.20),
        # Out of window: 2025 month is dropped.
        _MonthlyTurnoverFactory(date(2025, 3, 31), 1.00),
    ]
    yearly = yearly_turnover_from_monthly(
        series, start=date(2024, 1, 1), end=date(2024, 12, 31)
    )
    assert yearly == pytest.approx(0.35)


def test_yearly_turnover_from_monthly_partial_year_returns_partial_months_only() -> None:
    """A 4-month partial year sums 4 monthly ratios (not extrapolated)."""
    series = [
        _MonthlyTurnoverFactory(date(2026, 1, 31), 0.05),
        _MonthlyTurnoverFactory(date(2026, 2, 28), 0.08),
        _MonthlyTurnoverFactory(date(2026, 3, 31), 0.10),
        _MonthlyTurnoverFactory(date(2026, 4, 30), 0.07),
        _MonthlyTurnoverFactory(date(2026, 5, 31), 1.00),  # Out of window
    ]
    yearly = yearly_turnover_from_monthly(
        series, start=date(2026, 1, 1), end=date(2026, 4, 30)
    )
    assert yearly == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Legacy SEC formula (kept on the public API)
# ---------------------------------------------------------------------------


def test_portfolio_turnover_is_min_of_buys_and_sells_over_avg_nav() -> None:
    """SEC convention: ``min(buys, sells) / avg_nav`` strips out new-money deployment."""
    stmt = make_statement(
        account_label="A", start=date(2025, 1, 1), end=date(2025, 12, 31), frequency="A"
    ).model_copy(
        update={
            "transactions": (
                _trade(action="buy", amount_base=Decimal("-300"), trade_date=date(2025, 3, 1)),
                _trade(action="sell", amount_base=Decimal("100"), trade_date=date(2025, 6, 1)),
            ),
        }
    )
    stats = portfolio_turnover(
        [stmt],
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        average_nav=1000.0,
    )
    # min(300, 100) / 1000 = 10%
    assert stats.purchases_base == 300.0
    assert stats.sales_base == 100.0
    assert stats.ratio == pytest.approx(0.10)


# Local factory for MonthlyTurnover so tests can build series rows
# directly without spinning up statements / pooled periods.
def _MonthlyTurnoverFactory(period_end: date, ratio: float):  # noqa: N802
    from stock_portfolio_auditor.analytics.turnover import MonthlyTurnover

    return MonthlyTurnover(
        period_end=period_end,
        buys_base=0.0,
        sells_base=0.0,
        average_nav=1.0,
        ratio=ratio,
    )
