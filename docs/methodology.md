# Methodology

This document summarizes how the HTML performance audit report is built from parsed broker statements.

## Audit window

Every report is bounded by `--start-date` and `--end-date` (inclusive). Statements that partially overlap the window are **clipped**: in-window transactions and snapshots are kept; out-of-window data is dropped. Clip decisions are logged so you can audit what was included.

## Currency and FX

Non-USD holdings and transactions are converted to the statement's base currency (typically USD) through a single FX chokepoint (`fx_convert_statements`). Holdings use period-end rates for market value and acquisition-date rates for cost basis. If a required FX rate is missing, report generation fails with an actionable error rather than proceeding with partial data.

## Returns

- **Time-weighted return (TWR)** — pooled across accounts using month-end NAV and external cash flows.
- **Money-weighted return (MWR)** — IRR-style return on the pooled portfolio using dated cash flows.

## Return decomposition

Total PnL is decomposed into:

- Price (mark-to-market and realized stock PnL)
- Dividends
- Options (premium cash flows)
- FX translation
- Securities lending
- Cost drag (commissions and fees)

Per-position attribution reconciles broker-provided realized PnL, unrealized changes, and MTM where available.

## Concentration

Monthly concentration metrics (HHI, effective number of positions, top-N share) are computed on **stock and ETF holdings only** (options excluded). Values use broker-parsed USD market values. When only annual snapshots exist, quantities are reconstructed by replaying in-window trades against the nearest holdings snapshot, then valued at the snapshot's unit USD price.

## Turnover

Turnover uses a **two-way monthly** formula:

```text
monthly_turnover = (stock_buys + stock_sells) / 2 / average_NAV_for_month
```

Yearly turnover sums monthly ratios within each calendar year. The total KPI is the average of per-year turnover rates across years in the audit window. Stock trading volume comes from per-trade records (IBKR) or Schwab cash-summary aggregates (monthly statements).

## Risk metrics

Full-window beta, alpha, and information ratio are computed against the primary benchmark (default: URTH, MSCI World). Sharpe, Sortino, Calmar, and max drawdown are also reported on the pooled return series.

## Benchmarks

Benchmark total returns are fetched from external market-data providers (cached locally). Portfolio returns are compared on a monthly basis where data is available.
