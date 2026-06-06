# SPDX-License-Identifier: MIT
"""Generate a local report from supported files under the records directory.

The records directory is taken from (in order) the ``--records`` CLI flag,
the ``SPA_RECORDS_DIR`` environment variable, or ``./records`` relative to
the current working directory. Likewise the output directory falls back to
``SPA_REPORTS_DIR`` / ``./reports`` so the tool runs cross-platform without
baking a Windows-specific path into source.

The audit window is set explicitly via ``--start-date`` / ``--end-date``.
When provided, statements are *clipped* to the requested window:
fully-in-window statements pass through unchanged, fully-out-of-window
statements are dropped, and partial-overlap statements (e.g. an annual
1099 Composite when the audit starts mid-year, or an IBKR YTD CSV whose
end runs past the audit end) have their transactions / holdings /
per-symbol PnL trimmed to the in-window slice. Per-disposition realized
PnL is rebuilt from in-window dispositions whenever the source publishes
per-trade data (Schwab 1099-B); when it doesn't (IBKR MTM Performance
Summary), the full-period aggregate is kept with a logged note.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from stock_portfolio_auditor.domain.models import Statement
from stock_portfolio_auditor.ingestion.fx_reconstruction import fx_convert_statements
from stock_portfolio_auditor.ingestion.persistence import write_parsed_csvs
from stock_portfolio_auditor.parsers.base import parse_statement
from stock_portfolio_auditor.parsers.detect import detect_broker
from stock_portfolio_auditor.reporting import build_report_schema, write_html

SUPPORTED_EXTENSIONS = {".pdf", ".csv", ".html", ".htm"}
VERSIONED_NAME_RE = re.compile(r"^local_records_report_v(?P<version>\d+)\.html$")
DEFAULT_RECORDS_DIR = os.environ.get("SPA_RECORDS_DIR", "./records")
DEFAULT_REPORTS_DIR = os.environ.get("SPA_REPORTS_DIR", "./reports")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--records",
        default=DEFAULT_RECORDS_DIR,
        help=(
            "Directory containing broker statements organised per-account."
            " Defaults to $SPA_RECORDS_DIR or ./records."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_REPORTS_DIR,
        help=(
            "Directory to write the generated HTML report to."
            " Defaults to $SPA_REPORTS_DIR or ./reports."
        ),
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=None,
        help=(
            "Inclusive audit-window start date, ISO format (YYYY-MM-DD)."
            " Partial-overlap statements (e.g. an annual 1099 whose tax"
            " year extends back before this date) are clipped: only"
            " transactions and dispositions on/after this date contribute."
            " Omit to auto-detect from the earliest available statement."
        ),
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help=(
            "Inclusive audit-window end date, ISO format (YYYY-MM-DD)."
            " Partial-overlap statements (e.g. an IBKR YTD CSV whose"
            " period_end runs past this date) are clipped: only"
            " transactions on/before this date contribute, and the"
            " period_end holdings snapshot is dropped if it falls"
            " outside. Omit to auto-detect from the latest available"
            " month-end coverage."
        ),
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--csv-only", action="store_true")
    parser.add_argument(
        "--version",
        type=int,
        default=None,
        help="Override the version suffix. Defaults to next-available v<n>.",
    )
    parser.add_argument(
        "--primary-benchmark",
        default="URTH",
        help=(
            "Ticker used for the KPI strip's beta / alpha / IR regression."
            " Defaults to URTH (iShares MSCI World ETF), which is the right"
            " baseline for a global portfolio. Pass SPY for a US-only audit."
            " The cumulative-return chart still renders every benchmark in"
            " the default set regardless of this choice."
        ),
    )
    parser.add_argument(
        "--no-dollar-amounts",
        action="store_true",
        help=(
            "Suppress every surface that quotes absolute portfolio scale:"
            " Total $ P&L on the KPI strip, $ columns on the contribution"
            " tables, the value-vs-contributions / dollar PnL / option"
            " income charts, and the option-income table. Returns, bps"
            " decomposition, turnover, concentration, beta / alpha / IR"
            " stay visible so the report is shareable without revealing"
            " portfolio size."
        ),
    )
    args = parser.parse_args()

    records = Path(args.records)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)
    files = [
        path
        for path in sorted(records.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if args.csv_only:
        files = [path for path in files if path.suffix.lower() == ".csv"]
    statements, skipped = _parse_files_parallel(files, workers=args.workers)

    if not statements:
        print({"statements": 0, "skipped": len(skipped), "html": None})
        print({"first_skipped": skipped[:20]})
        return 1

    # Stage 1: parsed -> CSV (per-account and pooled). This is the inspectable
    # ground-truth that every downstream analytic reads from.
    fx_translated = fx_convert_statements(statements)
    parsed_paths = write_parsed_csvs(fx_translated)

    # Stage 2: analytics -> HTML report from the same FX-translated statements,
    # honouring the operator-supplied audit window (clips partial-overlap
    # statements to the in-window slice so the totals reflect only
    # in-range broker data).
    schema = build_report_schema(
        fx_translated,
        audit_start=args.start_date,
        audit_end=args.end_date,
        primary_benchmark=args.primary_benchmark,
        include_dollar_amounts=not args.no_dollar_amounts,
    )
    version = args.version if args.version is not None else _next_version(output_dir)
    html_path = write_html(schema, output_dir / f"local_records_report_v{version}.html")
    print(
        {
            "statements": len(statements),
            "accounts": len(schema.accounts),
            "period_start": schema.period_start.isoformat(),
            "period_end": schema.period_end.isoformat(),
            "skipped": len(skipped),
            "version": version,
            "html": str(html_path),
            "parsed_csvs": str(parsed_paths["pooled::holdings"].parent),
        }
    )
    if skipped:
        print({"first_skipped": skipped[:20]})
    return 0


def _next_version(output_dir: Path) -> int:
    """Find the next available version suffix in ``output_dir``.

    Existing files named ``local_records_report_v<n>.html`` are scanned and the
    highest ``n`` is incremented by 1. Returns 1 when no versioned report exists.
    """
    highest = 0
    if output_dir.exists():
        for candidate in output_dir.iterdir():
            match = VERSIONED_NAME_RE.match(candidate.name)
            if match:
                value = int(match.group("version"))
                highest = max(highest, value)
    return highest + 1


def _parse_files_parallel(
    files: Iterable[Path], *, workers: int
) -> tuple[list[Statement], list[str]]:
    statements = []
    skipped = []
    with ProcessPoolExecutor(max_workers=max(workers, 1)) as executor:
        futures = {executor.submit(_parse_one, path): path for path in files}
        for future in as_completed(futures):
            statement, error = future.result()
            if statement is not None:
                statements.append(statement)
            elif error is not None:
                skipped.append(error)
    return statements, skipped


def _parse_one(path: Path) -> tuple[Statement | None, str | None]:
    try:
        detection = detect_broker(path)
        if detection.broker.value == "ibkr" and detection.format.value != "csv":
            return None, f"{path.parent.name}/{path.name}: unsupported format"
        return parse_statement(path), None
    except Exception as exc:  # noqa: BLE001 - local build reports safely by file only
        return None, f"{path.parent.name}/{path.name}: {type(exc).__name__}"


if __name__ == "__main__":
    raise SystemExit(main())
