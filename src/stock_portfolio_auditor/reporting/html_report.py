# SPDX-License-Identifier: MIT
"""HTML report renderer."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from stock_portfolio_auditor.reporting.narrative import executive_summary
from stock_portfolio_auditor.reporting.report_schema import ReportSchema


def render_html(schema: ReportSchema) -> str:
    """Render a ReportSchema to HTML."""
    enriched = schema.model_copy(
        update={"narrative": schema.narrative or executive_summary(schema)}
    )
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.j2")
    return template.render(schema=enriched)


def write_html(schema: ReportSchema, path: str | Path) -> Path:
    """Write rendered HTML report to disk."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(schema), encoding="utf-8")
    return output
