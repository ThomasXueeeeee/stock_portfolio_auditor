# SPDX-License-Identifier: MIT
"""Shared helpers for IBKR Activity Statement parsers."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from stock_portfolio_auditor.domain.models import AssetKind, CostBucket, IncomeBucket
from stock_portfolio_auditor.ingestion.pii import account_label_from_path

IBKR_ACCOUNT_RE = re.compile(r"\bU\d{7,8}\b", re.IGNORECASE)


def account_label(path: str | Path, override: str | None = None) -> str:
    """Return the safe account label for an IBKR statement."""
    return override or account_label_from_path(path)


def parse_decimal(value: str | int | float | Decimal | None) -> Decimal:
    """Parse an IBKR numeric field into Decimal."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text or text == "--":
        return Decimal("0")
    text = text.replace(",", "")
    is_negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        result = Decimal(text)
    except InvalidOperation:
        return Decimal("0")
    return -result if is_negative else result


def normalize_symbol(symbol: str) -> str:
    """Normalize obvious IBKR symbol variants without leaking account data."""
    value = symbol.strip()
    if value.isdigit():
        return f"{value}.HK"
    return value


def asset_kind_from_ibkr(category: str) -> AssetKind:
    """Map IBKR asset category strings to normalized asset kinds."""
    lowered = category.strip().lower()
    if "option" in lowered:
        return AssetKind.OPTION
    if "stock" in lowered:
        return AssetKind.EQUITY
    if "fund" in lowered:
        return AssetKind.ETF
    if "forex" in lowered:
        return AssetKind.FX
    if "bond" in lowered or "bill" in lowered:
        return AssetKind.FIXED_INCOME
    return AssetKind.OTHER


def cost_bucket_for_trade(kind: AssetKind) -> CostBucket:
    """Choose commission bucket from asset kind."""
    if kind is AssetKind.OPTION:
        return CostBucket.OPTION_COMMISSION
    return CostBucket.STOCK_COMMISSION


def income_bucket_for_interest(description: str) -> IncomeBucket:
    """Tag IBKR interest rows."""
    if re.search(r"(?i)\b(SYEP|IBKR Managed Securities)\b", description):
        return IncomeBucket.LENDING_INCOME
    return IncomeBucket.INTEREST_CREDIT
