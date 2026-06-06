# SPDX-License-Identifier: MIT
"""Yahoo Finance sector vocabulary -> GICS sector crosswalk.

Yahoo Finance's ``.info["sector"]`` field uses an 11-element taxonomy that
descends from Morningstar and pre-dates GICS standardization. To produce
attribution panels that align with the way institutional benchmarks classify
holdings, we map every Yahoo sector to its closest GICS sector.

The mapping is documented in the security-master research notes
(``research/security_master_data_sources.md``). Where Yahoo's taxonomy splits
or aggregates differently from GICS (e.g. Yahoo's ``Financial Services``
covers both GICS ``Financials`` and ``Real Estate`` issuers when the latter
operates as a financial services company), the crosswalk uses the most
common mapping observed in the industry.

This crosswalk does **not** require any paid GICS license: the GICS-shaped
labels emitted here are only used in our own attribution output and never
republished as authoritative GICS data.
"""

from __future__ import annotations

from typing import Final

YAHOO_TO_GICS: Final[dict[str, str]] = {
    "Basic Materials": "Materials",
    "Communication Services": "Communication Services",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Energy": "Energy",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Industrials": "Industrials",
    "Real Estate": "Real Estate",
    "Technology": "Information Technology",
    "Utilities": "Utilities",
}

GICS_SECTORS: Final[tuple[str, ...]] = (
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
)


def yahoo_to_gics(sector: str | None) -> str | None:
    """Map a Yahoo Finance sector string to its closest GICS sector.

    Returns ``None`` when ``sector`` is None, empty, or unrecognized.
    """
    if not sector:
        return None
    return YAHOO_TO_GICS.get(sector.strip())
