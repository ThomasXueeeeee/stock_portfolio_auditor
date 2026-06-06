# Security Policy

## Reporting a Vulnerability

Please do not open a public issue if your report contains account numbers, broker
statements, holdings, transaction history, names, addresses, or other sensitive
financial data.

Use GitHub Security Advisories for private vulnerability reports:

<https://github.com/ThomasXueeeeee/stock_portfolio_auditor/security/advisories>

## Data Privacy

This project is designed as a local-first tool. Broker statement files are read
from disk and reports are written to user-selected local paths. Account numbers
and account-holder names should be redacted before parsed data reaches report
models.
