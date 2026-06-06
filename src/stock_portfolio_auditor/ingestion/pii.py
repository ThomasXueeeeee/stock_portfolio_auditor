# SPDX-License-Identifier: MIT
"""PII redaction helpers."""

from __future__ import annotations

import re
from pathlib import Path

ACCOUNT_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])\d{4}-\d{4}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])\d{8,12}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])U\d{7,8}(?![A-Za-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])XXX[-\w]*\d+[A-Z]?(?![A-Za-z0-9])", re.IGNORECASE),
]


def redact_text(value: str) -> str:
    """Redact likely account numbers from a string."""
    redacted = value
    for pattern in ACCOUNT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def account_label_from_path(path: str | Path) -> str:
    """Use the account folder name as the public account label."""
    file_path = Path(path)
    return file_path.parent.name or "account"
