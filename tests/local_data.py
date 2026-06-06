from __future__ import annotations

import os
from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".csv", ".html", ".htm"}


def discover_records_path() -> Path | None:
    """Locate the broker records directory, honouring environment overrides.

    Local-data tests opt in to running against a user's own statement folder
    only when one of the following resolves to an existing directory:

      * ``SPA_RECORDS_PATH`` env var (legacy spelling, kept for compat)
      * ``SPA_RECORDS_DIR`` env var (the runtime / GUI variable name)
      * ``./records`` relative to the working directory

    Returns ``None`` otherwise so the local_data-marked tests deselect
    cleanly in CI.
    """
    candidates = [
        os.getenv("SPA_RECORDS_PATH"),
        os.getenv("SPA_RECORDS_DIR"),
    ]
    for candidate_str in candidates:
        if candidate_str:
            candidate = Path(candidate_str)
            if candidate.exists():
                return candidate
    cwd_records = Path.cwd() / "records"
    if cwd_records.exists():
        return cwd_records
    return None


def discover_statement_files() -> list[Path]:
    records_path = discover_records_path()
    if records_path is None:
        return []
    return sorted(
        path
        for path in records_path.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
