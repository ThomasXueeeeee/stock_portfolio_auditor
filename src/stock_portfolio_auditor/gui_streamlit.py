# SPDX-License-Identifier: MIT
"""Streamlit GUI for local portfolio audit workflows."""

from __future__ import annotations

import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import streamlit as st

from stock_portfolio_auditor.parsers.base import parse_statement
from stock_portfolio_auditor.parsers.detect import detect_broker
from stock_portfolio_auditor.reporting import KpiStrip, ReportSchema, YearlySummaryRow, write_html
from stock_portfolio_auditor.reporting.build_pipeline import iter_build_stages

SUPPORTED_EXTENSIONS = {".pdf", ".csv", ".html", ".htm"}
DEFAULT_RECORDS = Path(os.environ.get("SPA_RECORDS_DIR", "./records"))
DEFAULT_REPORTS = Path(os.environ.get("SPA_REPORTS_DIR", "./reports"))


def main() -> None:
    """Render the Streamlit application."""
    st.set_page_config(page_title="Stock Portfolio Auditor", layout="wide")
    st.sidebar.title("stock_portfolio_auditor")
    page = st.sidebar.radio(
        "Navigation",
        ["1. Library", "2. Pre-flight", "3. Generate Report", "4. Report Viewer", "5. Settings"],
    )

    if page.startswith("1."):
        library_page()
    elif page.startswith("2."):
        preflight_page()
    elif page.startswith("3."):
        generate_page()
    elif page.startswith("4."):
        report_viewer_page()
    else:
        settings_page()


def library_page() -> None:
    """Show records folder summary and detection counts."""
    st.title("1. Library")
    records = Path(
        st.text_input(
            "Records folder",
            value=str(st.session_state.get("records_path", DEFAULT_RECORDS)),
        )
    )
    st.session_state["records_path"] = str(records)

    files = _statement_files(records)
    st.metric("Statements", len(files))
    if not files:
        st.warning("No statement files found.")
        return

    accounts = sorted({path.parent.name for path in files})
    st.metric("Accounts", len(accounts))

    counts: Counter[str] = Counter()
    failures: list[str] = []
    for path in files:
        try:
            detection = detect_broker(path)
            counts[f"{detection.broker.value}:{detection.format.value}"] += 1
        except Exception as exc:  # noqa: BLE001 - GUI summary should not crash
            failures.append(f"{path.parent.name}/{path.name}: {type(exc).__name__}")

    st.subheader("Detected formats")
    st.json(dict(counts))
    if failures:
        st.subheader("Detection failures")
        st.write(failures[:20])


def preflight_page() -> None:
    """Run safe parser pre-flight checks."""
    st.title("2. Pre-flight")
    records = Path(st.session_state.get("records_path", DEFAULT_RECORDS))
    files = _statement_files(records)
    if st.button("Run pre-flight parse checks"):
        rows: list[dict[str, object]] = []
        for path in files:
            try:
                detection = detect_broker(path)
                parsed = False
                if detection.format.value == "csv":
                    parse_statement(path)
                    parsed = True
                rows.append(
                    {
                        "account": path.parent.name,
                        "file": path.name,
                        "broker": detection.broker.value,
                        "format": detection.format.value,
                        "parsed": parsed,
                        "error": "",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - displayed in UI
                rows.append(
                    {
                        "account": path.parent.name,
                        "file": path.name,
                        "broker": "",
                        "format": path.suffix.lower().lstrip("."),
                        "parsed": False,
                        "error": type(exc).__name__,
                    }
                )
        st.dataframe(rows, use_container_width=True)


def generate_page() -> None:
    """Generate a minimal HTML report with progress feedback."""
    st.title("3. Generate Report")
    output_folder = Path(st.text_input("Output folder", str(DEFAULT_REPORTS)))
    if st.button("Build report"):
        progress = st.progress(0)
        status = st.empty()
        for stage in iter_build_stages():
            progress.progress(stage.progress)
            status.write(f"Stage {stage.index}/{stage.total}: {stage.name}")

        schema = _placeholder_schema()
        path = write_html(schema, output_folder / "sample_report.html")
        st.session_state["last_report"] = str(path)
        st.success(f"Report written to {path}")


def report_viewer_page() -> None:
    """Preview the latest generated report."""
    st.title("4. Report Viewer")
    last_report = st.session_state.get("last_report")
    if not last_report:
        st.info("No report generated in this session yet.")
        return
    path = Path(str(last_report))
    st.write(path)
    if path.exists():
        st.components.v1.html(path.read_text(encoding="utf-8"), height=800, scrolling=True)


def settings_page() -> None:
    """Display basic local settings."""
    st.title("5. Settings")
    st.write("Settings persistence under ~/.spa will be implemented with the full GUI.")


def _statement_files(records: Path) -> list[Path]:
    if not records.exists():
        return []
    return sorted(
        path
        for path in records.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def _placeholder_schema() -> ReportSchema:
    return ReportSchema(
        title="Portfolio Performance Audit",
        accounts=("local",),
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        base_currency="USD",
        generated_at=datetime.now(),
        kpis=KpiStrip(
            twr=0.0,
            mwr_irr=0.0,
            mwr_dietz=0.0,
            total_pnl=0.0,
            max_drawdown=0.0,
            sharpe=0.0,
            sortino=0.0,
            calmar=0.0,
            tcost_bps=0.0,
        ),
        yearly_summary=(
            YearlySummaryRow(
                label="Placeholder",
                twr=0.0,
                mwr_irr=0.0,
                price_bps=0.0,
                dividend_bps=0.0,
                option_bps=0.0,
                fx_bps=0.0,
                lending_bps=0.0,
                tcost_bps=0.0,
                pnl=0.0,
            ),
        ),
    )


if __name__ == "__main__":
    main()
