# Privacy Model

`stock_portfolio_auditor` is designed to keep broker data on your machine.

## What stays local

- **Broker statements** — PDFs and CSVs under `records/` are gitignored and never uploaded by this tool.
- **Generated reports** — HTML output under `reports/` (or `SPA_REPORTS_DIR`) is gitignored.
- **Market-data cache** — downloaded prices and FX rates under `data_cache/` are gitignored.

Set `SPA_RECORDS_DIR` and `SPA_REPORTS_DIR` to point at directories outside the repository if you prefer.

## Redaction at parse time

Parsers redact likely account identifiers as text is extracted:

- Schwab `####-####` account numbers
- IBKR `U########` account IDs
- Long numeric runs that resemble account or tax IDs

Redacted values are replaced with `[REDACTED]` before any analytics run. Account labels in the report come from **folder names** (e.g. `schwab_individual/`, `IRA/`), not from broker header fields.

## Pre-commit PII scanner

`scripts/pii_scan.py` scans all git-tracked text files for patterns that commonly indicate real account data leaking into the repository. It is wired as a pre-commit hook via `.pre-commit-config.yaml`.

Run manually before pushing:

```bash
python scripts/pii_scan.py
```

Synthetic fixtures under `tests/` and `examples/` are exempt.

## Contributing safely

- Do not commit real broker statements, balances, names, or addresses.
- Use synthetic fixtures under `tests/fixtures/synthetic/` for parser tests.
- See `CONTRIBUTING.md` for the full checklist.
