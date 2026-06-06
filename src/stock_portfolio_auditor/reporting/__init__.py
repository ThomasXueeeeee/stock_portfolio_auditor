"""Reporting schema and rendering helpers.

The report is delivered as a single interactive HTML file. If you need a
PDF, open the HTML in your browser and use ``File -> Print -> Save as PDF``;
the browser preserves Plotly charts as vectors and handles pagination far
better than a server-side HTML-to-PDF converter would.
"""

from __future__ import annotations

from stock_portfolio_auditor.reporting.build_pipeline import BuildStage, iter_build_stages
from stock_portfolio_auditor.reporting.build_report import build_report_schema
from stock_portfolio_auditor.reporting.html_report import render_html, write_html
from stock_portfolio_auditor.reporting.narrative import executive_summary
from stock_portfolio_auditor.reporting.report_schema import (
    FIELD_LABELS,
    ChartBlock,
    ContributionRow,
    KpiStrip,
    ReportSchema,
    YearlySummaryRow,
)

__all__ = [
    "FIELD_LABELS",
    "BuildStage",
    "ChartBlock",
    "ContributionRow",
    "KpiStrip",
    "ReportSchema",
    "YearlySummaryRow",
    "build_report_schema",
    "executive_summary",
    "iter_build_stages",
    "render_html",
    "write_html",
]
