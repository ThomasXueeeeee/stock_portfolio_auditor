# SPDX-License-Identifier: MIT
"""Pre-warm the on-disk benchmark monthly close cache.

Usage::

    python scripts/warm_benchmark_cache.py --start 2018-01-01

This pulls monthly closes for the default benchmark tickers and stores them at
``data_cache/benchmarks/<ticker>.parquet``. Re-running the script later only
fetches the months that aren't already cached, so it is safe to run on a cron.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from stock_portfolio_auditor.reporting.benchmark_data import (
    DEFAULT_BENCHMARK_TICKERS,
    fetch_benchmark_series,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        default=date(2018, 1, 1),
        help="Earliest month to cache (YYYY-MM-DD). Defaults to 2018-01-01.",
    )
    parser.add_argument(
        "--end",
        type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        default=datetime.now().date(),
        help="Latest month to cache (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=list(DEFAULT_BENCHMARK_TICKERS),
        help="Override the benchmark ticker list.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Override the benchmark cache directory.",
    )
    args = parser.parse_args()

    series = fetch_benchmark_series(
        start=args.start,
        end=args.end,
        tickers=tuple(args.tickers),
        cache_dir=args.cache_dir,
    )
    print(f"Cached {len(series)} of {len(args.tickers)} benchmark series.")
    for entry in series:
        print(f"  {entry.ticker:>6s}  {len(entry.dates):>4d} months")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
