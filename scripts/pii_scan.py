# SPDX-License-Identifier: MIT
"""Pre-commit PII scanner for the staged diff.

Greps the working tree's tracked text files for patterns that commonly
identify a specific user's data leaking into the public repository:

  * 8-12 consecutive digits not inside ``[REDACTED]`` (looks like an
    account or tax-id number).
  * ``\d{4}-\d{4}`` (Schwab account number format).
  * ``\d{3}-\d{2}-\d{4}`` (US SSN format).
  * ``Uxxxxxxxx`` (IBKR account identifier).

Synthetic test fixtures in ``tests/`` and ``examples/`` are allowed to
contain account-like numbers; they're skipped via prefix matching.

Exits non-zero with a per-file list of suspicious matches so pre-commit
fails before the commit lands. Run manually:

    python scripts/pii_scan.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ALLOWED_PATH_PREFIXES = (
    "tests/",
    "examples/",
)
# Auto-generated lock files routinely contain 8+-digit version stamps
# (e.g. pdfminer-six=={YYYYMMDD}) that look like account numbers but are
# not.
ALLOWED_PATH_GLOBS = (
    "environment.lock.",
    ".github/",
)
# Filename extensions to scan -- text-only.
SCANNABLE_SUFFIXES = {
    ".py",
    ".md",
    ".rst",
    ".txt",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".html",
    ".j2",
    ".sh",
    ".ps1",
    ".cmd",
    ".bat",
}

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("schwab_account_number", re.compile(r"(?<![A-Za-z0-9])\d{4}-\d{4}(?![A-Za-z0-9])")),
    ("ibkr_account_id", re.compile(r"(?<![A-Za-z0-9])U\d{7,8}(?![A-Za-z0-9])")),
    ("ssn_like", re.compile(r"(?<![A-Za-z0-9])\d{3}-\d{2}-\d{4}(?![A-Za-z0-9])")),
    # Long bare digit run not already redacted. ``[REDACTED]`` substrings are
    # explicitly allowed because the parser uses them as placeholders.
    (
        "long_digit_run",
        re.compile(r"(?<![A-Za-z0-9\[])\d{8,12}(?![A-Za-z0-9\]])"),
    ),
)


def _tracked_files() -> list[Path]:
    """Return paths of all git-tracked files in the current repo."""
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def _is_allowed(path: Path) -> bool:
    """Return True for paths that may legitimately contain account-like data."""
    posix = path.as_posix()
    if any(posix.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
        return True
    return any(fragment in posix for fragment in ALLOWED_PATH_GLOBS)


def main() -> int:
    paths = [p for p in _tracked_files() if p.suffix.lower() in SCANNABLE_SUFFIXES]
    failures: list[str] = []
    for path in paths:
        if _is_allowed(path):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pattern in PATTERNS:
            for line_no, line in enumerate(content.splitlines(), start=1):
                if pattern.search(line):
                    snippet = line.strip()[:160]
                    failures.append(f"{path}:{line_no}: {name}: {snippet}")
    if failures:
        print("PII scanner found potentially-sensitive patterns:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nIf these are false positives, exempt them by moving the file "
            "under tests/ or examples/, or by replacing the value with a "
            "synthetic placeholder.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
