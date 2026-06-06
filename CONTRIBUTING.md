# Contributing

Thanks for helping improve `stock_portfolio_auditor`.

## Development Setup

Use conda. Plain `pip`/`venv` is not the supported development path.

```bash
conda env create -f environment.yml
conda activate perf_audit
python -m pip install -e ".[dev,docs,gui]"
```

`environment.yml` is the cross-platform spec used by CI and recommended for all contributors. `environment.lock.win-64.yml` is a per-OS snapshot of the original developer's exact build versions and only works on Windows; do not use it on macOS or Linux.

## Privacy Checklist

Before opening a PR:

- Do not commit real broker statements.
- Do not commit account numbers, names, addresses, or balances from real accounts.
- Use synthetic fixtures under `tests/fixtures/synthetic/`.
- Run the PII scanner: `python scripts/pii_scan.py` (also wired as a pre-commit hook).

## Tests

```powershell
pytest -m "not local_data"
```

Local-data tests are opt-in and use your own records folder. They must never run in CI.
