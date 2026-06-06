# Stock Portfolio Auditor

`stock_portfolio_auditor` is a local-first tool for parsing broker statements and producing an institutional-style performance audit report for fundamental equity portfolios.

**Supported brokers**

- **Charles Schwab** — brokerage monthly statements (PDF) and Form 1099 Composite / Year-End Summary (PDF).
- **Interactive Brokers (IBKR)** — Activity Statements (CSV preferred; HTML and PDF also parse).

Other brokers (Fidelity, Robinhood, etc.) are **not supported yet**. The detector recognises some of their statement formats but no parser is registered, so those files will be skipped during ingestion with a `BrokerNotDetectedError` in the run log. Open an issue if you want to contribute a parser.

The tool is designed to keep private data local. Broker records are never committed, account numbers are redacted at parse time, and account labels come from folder names.

## Quickstart

```bash
conda env create -f environment.yml
conda activate perf_audit
python -m pip install -e .
streamlit run -m stock_portfolio_auditor.gui_streamlit
```

`environment.yml` carries cross-platform version ranges and is the supported install path on Windows, macOS, and Linux. `environment.lock.win-64.yml` is a Windows-specific bit-for-bit snapshot used for exact reproducibility of the original developer's environment; it is not portable to other operating systems. To produce a lock file for your own platform, run `conda env export --no-builds > environment.lock.$(uname -s)-$(uname -m).yml` once your environment is up.

By default the app looks for records under:

```text
./records/
```

To point at a different folder, set ``SPA_RECORDS_DIR`` (and optionally
``SPA_REPORTS_DIR`` for the HTML output directory) before running the
report generator or the Streamlit GUI:

```bash
export SPA_RECORDS_DIR=/path/to/my/broker/statements
export SPA_REPORTS_DIR=/path/to/output/reports
python scripts/generate_local_report.py --start-date 2022-10-01 --end-date 2026-04-30
```

The CLI also accepts ``--records`` and ``--output-dir`` flags that override
the environment variables when both are present.

### Audit window

The report's audit window is bounded by ``--start-date`` and ``--end-date`` (ISO format, inclusive). Statements are *clipped* to the requested window before any analytics run:

- Statements **fully inside** the window pass through unchanged.
- Statements **fully outside** the window are dropped.
- Statements that **partially overlap** the window (e.g. a full-year 1099 Composite when the audit starts mid-year) are *trimmed*: transactions are filtered to ``trade_date`` inside the window, holdings / cash snapshots are kept only when ``period_end`` is in window, and per-symbol realized PnL is rebuilt from in-window per-disposition rows when the source publishes per-trade data (Schwab 1099-B does; IBKR's *Mark-to-Market Performance Summary* on a YTD CSV does not — see the IBKR notes below).

Every clip is logged with a one-line note so you can see exactly what was kept and what was dropped:

```text
[audit-window] schwab_individual 2022-01-01..2022-12-31: clipped to 2022-10-01..2026-04-30 (start before audit start); per_symbol_pnl_base rebuilt from in-window dispositions
[audit-window] ibkr_individual 2026-01-01..2026-05-22: clipped to 2022-10-01..2026-04-30 (end after audit end); per_symbol_pnl_base kept as full-period aggregate (no per-disposition data available -- accept slight over-attribution or re-download with tighter dates); period_end snapshot dropped (outside audit window)
```

Omit both date flags to auto-detect the window from the available statements (latest month-end with full account coverage). This is the path of least resistance for ad-hoc runs but means you implicitly accept whatever range the statements happen to span.

Expected folder layout (one folder per account, statements inside):

```text
records/
  schwab_individual/                    # Schwab taxable brokerage
    Brokerage Statement_2025-01-31_123.PDF
    Brokerage Statement_2025-02-28_123.PDF
    1099 Composite and Year-End Summary - 2022_2023-02-24_123.PDF
    1099 Composite and Year-End Summary - 2023_2024-02-15_123.PDF
    1099 Composite and Year-End Summary - 2024_2025-02-10_123.PDF
  IRA/                                  # Schwab Traditional IRA
    Brokerage Statement_2024-03-31_456.PDF
  RothIRA/                              # Schwab Roth IRA
    Brokerage Statement_2024-03-31_789.PDF
  ibkr_individual/                      # IBKR taxable
    Uxxxxxxx_2023_yearly.csv
    Uxxxxxxx_2024_yearly.csv
    Uxxxxxxx_2025_yearly.csv
    Uxxxxxxx_2026_01.csv
    Uxxxxxxx_2026_02.csv
    Uxxxxxxx_2026_03.csv
    Uxxxxxxx_2026_04.csv
```

### Required data sources per account (read this before you ingest)

Each broker / account type has **one** correct combination of documents. Mixing other documents (trade logs, transaction-history CSVs, daily portfolio reports) will either be ignored or — for documents the detector can't recognise — surfaced as `BrokerNotDetectedError` in the run log.

#### Schwab — taxable brokerage account (`schwab_individual/`)

| Period                                | Document to drop in                                                                        |
| ------------------------------------- | ------------------------------------------------------------------------------------------ |
| Closed tax years up through 2024      | **Form 1099 Composite and Year-End Summary** (one PDF per tax year)                        |
| Tax year 2025 and onward              | **Monthly Brokerage Statement** PDFs (one per month)                                       |

**Why the split:** Schwab's pre-2025 monthly brokerage statements collapse all stock realized PnL into a single aggregate line (*"Investments Sold $X"*) with no per-symbol detail. The annual 1099 Composite is the only Schwab document that publishes per-lot realized gain/loss for those years. Starting in early 2025 Schwab redesigned the monthly statement to include a per-symbol *Realized Gain or (Loss)* column, so monthly statements are sufficient from 2025 onward.

If you provide both a 1099 Composite and overlapping monthly statements for the same year, the 1099 takes precedence for per-symbol realized PnL and the monthly statements continue to supply holdings snapshots, dividends, options, and cash flows.

#### Schwab — IRA and Roth IRA (`IRA/`, `RothIRA/`)

| Period                                | Document to drop in                                                                        |
| ------------------------------------- | ------------------------------------------------------------------------------------------ |
| All years                             | **Monthly Brokerage Statement** PDFs (one per month)                                       |

**Why monthly is enough:** Schwab IRA / Roth IRA monthly statements include a per-symbol *Gain or (Loss) on Investments Sold* table on every statement, so you do not need a 1099 to recover per-symbol realized PnL. (1099-B isn't even issued for tax-advantaged accounts.)

#### Interactive Brokers — all account types (`ibkr_individual/`, etc.)

The only IBKR document type this tool parses is the **Activity Statement**. Trade Confirmations, daily Portfolio Analyst reports, Flex Queries, MTM Summary one-pagers, and any other IBKR report variant are *not* supported — drop them anywhere else.

| Period                                | Document to drop in                                                                        |
| ------------------------------------- | ------------------------------------------------------------------------------------------ |
| Each closed prior calendar year       | **Yearly Activity Statement** — period `Jan 1, YYYY – Dec 31, YYYY` (one CSV per year)     |
| Current (in-progress) calendar year   | **Monthly Activity Statements** — one CSV per finished month (`Jan 1 – Jan 31`, etc.)      |

**Do not use a YTD Activity Statement for the current year.** The Activity Statement's *Mark-to-Market Performance Summary* section publishes a single per-symbol number per period that mixes realized PnL on closed trades *and* the unrealized PnL change on positions held at the period boundary. When a YTD statement (e.g. `Jan 1, 2026 – May 22, 2026`) extends past your audit end date, that combined number cannot be split per-trade: the audit-window filter can clip the per-trade `Trades` section by date, but the unrealized component for positions held at the YTD's `period_end` cannot be re-priced to the audit end without external market data. The result is over-attribution of the last few weeks. Monthly Activity Statements avoid the problem entirely because each month is a clean unit and audit windows align to month-ends.

CSV is the preferred IBKR format. HTML and PDF parsers exist as fallbacks but extract less detail (CSV publishes Trades-section commission columns and Forex Balances rows that HTML / PDF do not).

#### What *not* to drop in

- Schwab trade-log / transaction-history CSV exports — they list prices but omit commissions, option premiums, lot detail, and corporate-action adjustments. Skip them.
- Daily / weekly portfolio screenshots, position drift reports, broker tax-loss-harvesting reports.
- Any document from a broker other than Schwab or IBKR (see the top of this README — Fidelity, Robinhood, etc. are not supported yet).

## Planned Report

The HTML report includes:

- Time-weighted and money-weighted returns
- Capital-timing skill versus a DCA counterfactual
- Six-way return decomposition: price, dividends, options, FX, securities lending, cost drag
- Dollar PnL waterfall
- Risk and drawdown analysis
- Cost and tax analysis
- FX attribution
- Benchmark comparisons
- Concentration and effective number of positions

The deliverable is a single interactive HTML file with embedded Plotly charts. If you need a static PDF copy, open the HTML in any modern browser and use `File -> Print -> Save as PDF`; the browser preserves Plotly charts as vectors and paginates cleanly.

See `docs/methodology.md` for the calculation methodology and `docs/privacy.md` for the privacy model.
