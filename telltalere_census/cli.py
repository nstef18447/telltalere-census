"""
Command-line entry point for telltalere-census.

Subcommands:
  warm-cache   Bulk-fetch ACS detailed tables and data profiles into the
               local bulk cache.
  warm-pums    Bulk-download Census PUMS ZIPs from the FTP and convert
               each to parquet.

Both subcommands accept `--dry-run` to enumerate the work plan and exit
without fetching. See module-level docstrings in `warm_cache.py` and
`pums_bulk.py` for cache layout and scope details.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from telltalere_census.pums_bulk import download_pums_bulk
from telltalere_census.warm_cache import (
    DEFAULT_WARM_GEOGRAPHIES_1YR,
    DEFAULT_WARM_GEOGRAPHIES_5YR,
    DEFAULT_WARM_TABLES,
    DEFAULT_WARM_VINTAGES_1YR,
    DEFAULT_WARM_VINTAGES_5YR,
    warm_cache,
)


def _csv_list(value: Optional[str]) -> Optional[list[str]]:
    if value is None:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _csv_int_list(value: Optional[str]) -> Optional[list[int]]:
    parts = _csv_list(value)
    if parts is None:
        return None
    return [int(p) for p in parts]


def _add_warm_cache_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cache-dir", type=Path, default=None,
                   help="Override bulk cache root.")
    p.add_argument("--vintages-5yr", type=str, default=None,
                   help=f"Comma-separated 5-year vintage tags (e.g. '2020-2024,2019-2023'). "
                        f"Default: all {len(DEFAULT_WARM_VINTAGES_5YR)} ({DEFAULT_WARM_VINTAGES_5YR[0]}..{DEFAULT_WARM_VINTAGES_5YR[-1]}).")
    p.add_argument("--vintages-1yr", type=str, default=None,
                   help=f"Comma-separated 1-year vintage years. "
                        f"Default: all {len(DEFAULT_WARM_VINTAGES_1YR)} ({DEFAULT_WARM_VINTAGES_1YR[0]}..{DEFAULT_WARM_VINTAGES_1YR[-1]} ex. 2020).")
    p.add_argument("--states", type=str, default=None,
                   help="Comma-separated 2-digit state FIPS codes. Default: all 52 (50+DC+PR).")
    p.add_argument("--tables", type=str, default=None,
                   help=f"Comma-separated table IDs. Default: all {len(DEFAULT_WARM_TABLES)} default tables.")
    p.add_argument("--geographies-5yr", type=str, default=None,
                   help=f"Comma-separated 5-year geographies. Default: {','.join(DEFAULT_WARM_GEOGRAPHIES_5YR)}.")
    p.add_argument("--geographies-1yr", type=str, default=None,
                   help=f"Comma-separated 1-year geographies. Default: {','.join(DEFAULT_WARM_GEOGRAPHIES_1YR)}.")
    p.add_argument("--max-workers", type=int, default=5,
                   help="ThreadPoolExecutor size (default 5).")
    p.add_argument("--target-rate", type=float, default=5.0,
                   help="Target aggregate Census API request rate, req/sec (default 5.0).")
    p.add_argument("--api-key", type=str, default=None,
                   help="Census API key (else read from CENSUS_API_KEY env).")
    p.add_argument("--log-path", type=Path, default=None,
                   help="Override warm log file location.")
    p.add_argument("--min-free-gb", type=float, default=20.0,
                   help="Halt if cache disk has less free space (default 20).")
    p.add_argument("--dry-run", action="store_true",
                   help="Enumerate the work plan and exit without fetching.")


def _add_warm_pums_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cache-dir", type=Path, default=None)
    p.add_argument("--vintages-5yr", type=str, default=None,
                   help="Comma-separated 5-year vintage tags.")
    p.add_argument("--vintages-1yr", type=str, default=None,
                   help="Comma-separated 1-year vintage years.")
    p.add_argument("--pums-type", type=str, default=None,
                   help="Comma-separated PUMS types (subset of '5yr,1yr'). Default both.")
    p.add_argument("--states", type=str, default=None)
    p.add_argument("--record-types", type=str, default=None,
                   help="Comma-separated record types (subset of 'h,p'). Default both.")
    p.add_argument("--max-concurrent", type=int, default=4)
    p.add_argument("--min-free-gb", type=float, default=40.0)
    p.add_argument("--dry-run", action="store_true")


def _run_warm_cache(args: argparse.Namespace) -> int:
    result = warm_cache(
        cache_dir=args.cache_dir,
        vintages_5yr=_csv_list(args.vintages_5yr),
        vintages_1yr=_csv_int_list(args.vintages_1yr),
        geographies_5yr=_csv_list(args.geographies_5yr),
        geographies_1yr=_csv_list(args.geographies_1yr),
        states=_csv_list(args.states),
        tables=_csv_list(args.tables),
        max_workers=args.max_workers,
        target_rate_per_sec=args.target_rate,
        api_key=args.api_key,
        log_path=args.log_path,
        dry_run=args.dry_run,
        min_free_gb=args.min_free_gb,
    )
    print(result)
    if result.log_path is not None:
        print(f"log: {result.log_path}")
    if result.failed_count > 0:
        # Print up to the first 20 failures so they're visible without grepping the log.
        print(f"\nFirst {min(20, result.failed_count)} failures:")
        for row in result.failed_tuples[:20]:
            print(f"  {row}")
    return 0 if result.failed_count == 0 else 1


def _run_warm_pums(args: argparse.Namespace) -> int:
    result = download_pums_bulk(
        cache_dir=args.cache_dir,
        vintages_5yr=_csv_list(args.vintages_5yr),
        vintages_1yr=_csv_int_list(args.vintages_1yr),
        pums_type=_csv_list(args.pums_type),
        states=_csv_list(args.states),
        record_types=_csv_list(args.record_types),
        max_concurrent_downloads=args.max_concurrent,
        min_free_gb=args.min_free_gb,
        dry_run=args.dry_run,
    )
    print(result)
    if result.failed_count > 0:
        print(f"\nFirst {min(20, result.failed_count)} failures:")
        for row in result.failed_tuples[:20]:
            print(f"  {row}")
    return 0 if result.failed_count == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="telltalere-census",
        description="Bulk-warm the local Census cache (ACS detailed tables, "
                    "data profiles, and PUMS).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_warm = subparsers.add_parser(
        "warm-cache",
        help="Bulk-fetch ACS B-tables and DP-tables into the bulk cache.",
    )
    _add_warm_cache_args(p_warm)

    p_pums = subparsers.add_parser(
        "warm-pums",
        help="Bulk-download Census PUMS ZIPs from the FTP, convert to parquet.",
    )
    _add_warm_pums_args(p_pums)

    args = parser.parse_args(argv)

    if args.command == "warm-cache":
        return _run_warm_cache(args)
    if args.command == "warm-pums":
        return _run_warm_pums(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
