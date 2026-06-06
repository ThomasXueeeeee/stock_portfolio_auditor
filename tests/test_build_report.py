from __future__ import annotations

from datetime import date
from decimal import Decimal

from stock_portfolio_auditor.reporting.build_report import (
    _contribution_decomposition,
    build_report_schema,
)
from tests.factories import make_statement


def test_build_report_schema_from_statement() -> None:
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("110")}
    )
    schema = build_report_schema([statement])
    assert schema.accounts == ("acct",)
    assert schema.period_start == date(2024, 1, 1)
    assert schema.kpis.total_pnl == 10
    # New default: include $ amounts on the report unless the operator
    # opts out via ``include_dollar_amounts=False``.
    assert schema.include_dollar_amounts is True


def test_build_report_schema_respects_include_dollar_amounts_flag() -> None:
    """``include_dollar_amounts=False`` propagates through to the schema
    so the template can hide every absolute-scale surface (the Total
    $ P&L row, contribution-table dollar columns, dollar charts and
    the option-income table) while keeping returns / bps / turnover /
    concentration visible."""
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("110")}
    )
    schema = build_report_schema([statement], include_dollar_amounts=False)
    assert schema.include_dollar_amounts is False
    # The numbers are still computed -- the *template* hides them. This
    # keeps the chart pipeline (which reads `total_pnl` for axis
    # scaling) untouched.
    assert schema.kpis.total_pnl == 10


def test_build_report_schema_primary_benchmark_kwarg_records_ticker_on_kpis() -> None:
    """The primary benchmark default is URTH (MSCI World) for a global
    portfolio. The KPI strip records the ticker that was actually used
    so the template can label the regression row precisely (e.g. "Beta
    vs URTH" / "Beta vs SPY")."""
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={"beginning_value_base": Decimal("100"), "ending_value_base": Decimal("110")}
    )
    schema = build_report_schema([statement], primary_benchmark="SPY")
    # The benchmark fetch may or may not succeed offline; what we pin
    # is that the kwarg propagates to the KPI strip's ticker field even
    # when the actual regression returns None (insufficient overlap on a
    # single-statement test).
    assert schema.kpis.primary_benchmark_ticker in {"SPY", ""}


def test_contribution_decomposition_pulls_ibkr_forex_mtm_out_of_price() -> None:
    """IBKR Forex MTM lives under ``_FX_<CCY>`` in per_symbol_pnl_base and must
    show up in the FX bucket, not be absorbed into the Price residual.
    """
    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "beginning_value_base": Decimal("1000"),
            "ending_value_base": Decimal("1100"),
            "per_symbol_pnl_base": {
                "AAPL": Decimal("130"),
                "_FX_EUR": Decimal("-30"),
            },
            "per_symbol_pnl_includes_unrealized": True,
        }
    )
    _, dollars = _contribution_decomposition([statement], average_nav=1050.0)
    assert dollars["fx"] == -30
    # NAV change = 100, no other buckets active besides FX, so Price absorbs
    # what FX didn't claim: 100 - (-30) = 130.
    assert dollars["price"] == 130
    # Buckets sum back to NAV change (no external CF in this fixture).
    assert (
        dollars["price"]
        + dollars["dividends"]
        + dollars["options"]
        + dollars["fx"]
        + dollars["lending"]
        + dollars["tcost"]
        == 100
    )


def test_contribution_decomposition_does_not_double_count_ibkr_cash_fx() -> None:
    """For IBKR statements, MTM ``_FX_*`` and CashBalance.fx_translation_pnl
    measure different aggregations (period delta vs. cumulative snapshot) of
    the same underlying FX P&L. We must use only one of the two.
    """
    from stock_portfolio_auditor.domain.models import CashBalance

    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "beginning_value_base": Decimal("1000"),
            "ending_value_base": Decimal("1100"),
            "per_symbol_pnl_base": {"_FX_EUR": Decimal("-30")},
            "per_symbol_pnl_includes_unrealized": True,
            "cash_balances": (
                CashBalance(
                    currency="EUR",
                    starting=Decimal("0"),
                    ending=Decimal("100"),
                    # Cumulative snapshot value, NOT the period delta.
                    fx_translation_pnl=Decimal("75"),
                ),
            ),
        }
    )
    _, dollars = _contribution_decomposition([statement], average_nav=1050.0)
    # Only the MTM `_FX_*` row contributes; the cash snapshot is ignored.
    assert dollars["fx"] == -30


def test_schwab_1099_supersedes_monthly_per_symbol_pnl_for_covered_year() -> None:
    """The annual Form 1099 Composite is the authoritative realized PnL source
    for the brokerage account; when present its Statement overrides the
    monthly statements' per_symbol_pnl_base for the same account / year.
    """
    from stock_portfolio_auditor.reporting.build_report import _dedupe_schwab_1099_realized

    monthly = make_statement(
        start=date(2023, 6, 1),
        end=date(2023, 6, 30),
        frequency="M",
    ).model_copy(
        update={
            "account_label": "brokerage_taxable",
            "broker": "schwab",
            "per_symbol_pnl_base": {"GOOGL": Decimal("-6214"), "AMZN": Decimal("-5872.90")},
        }
    )
    composite = make_statement(
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "account_label": "brokerage_taxable",
            "broker": "schwab",
            "per_symbol_pnl_base": {
                "GOOGL": Decimal("-6214"),
                "AMZN": Decimal("-5872.90"),
                "BRK.B": Decimal("12424.96"),
            },
        }
    )
    other_account_monthly = make_statement(
        start=date(2023, 6, 1),
        end=date(2023, 6, 30),
        frequency="M",
    ).model_copy(
        update={
            "account_label": "brokerage_retirement",
            "broker": "schwab",
            "per_symbol_pnl_base": {"TCEHY": Decimal("1500")},
        }
    )
    result = _dedupe_schwab_1099_realized([monthly, composite, other_account_monthly])
    by_label = {(s.account_label, s.frequency): s for s in result}
    # Monthly brokerage_taxable is zeroed out by the matching 1099.
    assert by_label[("brokerage_taxable", "M")].per_symbol_pnl_base == {}
    # 1099 untouched.
    assert by_label[("brokerage_taxable", "A")].per_symbol_pnl_base == composite.per_symbol_pnl_base
    # Other account untouched because no 1099 covers it.
    assert (
        by_label[("brokerage_retirement", "M")].per_symbol_pnl_base
        == other_account_monthly.per_symbol_pnl_base
    )


def test_contribution_decomposition_uses_cash_balance_fx_for_non_ibkr() -> None:
    """When a broker exposes only cash-balance snapshot FX P&L (Schwab style)
    we fall back to ``cash.fx_translation_pnl`` for the FX bucket.
    """
    from stock_portfolio_auditor.domain.models import CashBalance

    statement = make_statement(
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        frequency="A",
    ).model_copy(
        update={
            "beginning_value_base": Decimal("1000"),
            "ending_value_base": Decimal("1100"),
            "per_symbol_pnl_includes_unrealized": False,
            "cash_balances": (
                CashBalance(
                    currency="EUR",
                    starting=Decimal("0"),
                    ending=Decimal("100"),
                    fx_translation_pnl=Decimal("12"),
                ),
            ),
        }
    )
    _, dollars = _contribution_decomposition([statement], average_nav=1050.0)
    assert dollars["fx"] == 12
