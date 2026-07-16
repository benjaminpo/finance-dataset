#!/usr/bin/env python3
"""CLI entry point for the Yahoo Finance data pipeline."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow `python src/main.py` without installing the package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fetcher import (  # noqa: E402
    ALL_INTERVALS,
    DEFAULT_INTERVALS,
    DEFAULT_WORKERS,
    run_pipeline,
)
from src.listings import refresh_listings  # noqa: E402
from src.summary import write_fetch_summary  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch historical and intraday market data from Yahoo Finance "
        "and store it as partitioned CSV files under data/.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "tickers.yaml",
        help="Path to tickers YAML config (default: config/tickers.yaml)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "data",
        help="Root directory for CSV output (default: data/)",
    )
    parser.add_argument(
        "--intervals",
        nargs="+",
        default=list(DEFAULT_INTERVALS),
        choices=list(ALL_INTERVALS),
        help=(
            "Intervals to fetch (default: all supported — "
            + " ".join(ALL_INTERVALS)
            + ")"
        ),
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="Seconds to sleep after each ticker request (default: 0.25; "
        "raise if Yahoo rate-limits)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel Yahoo fetch workers (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip tickers that already have a cumulative CSV (or today's "
        "intraday snapshot); useful to resume a long first backfill",
    )
    parser.add_argument(
        "--skip-listings-refresh",
        action="store_true",
        help="Do not check remote listing CSVs for updates before fetching",
    )
    parser.add_argument(
        "--listings-only",
        action="store_true",
        help="Only refresh listing CSVs; skip market data fetch",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=None,
        help="Write fetch counts/failure details as JSON (also writes sibling "
        ".md). Under GitHub Actions, Markdown is appended to the job summary.",
    )
    parser.add_argument(
        "--asset-classes",
        nargs="+",
        default=None,
        help="Only fetch these asset classes (default: all in the config)",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="Zero-based shard index when splitting a class across CI jobs "
        "(default: 0)",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="Number of shards for --shard-index (default: 1 = no split)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.config.exists():
        logging.error("Config file not found: %s", args.config)
        return 1

    if not args.skip_listings_refresh:
        listing_summary = refresh_listings(args.config)
        print(
            f"Listings: checked={listing_summary['checked']} "
            f"updated={listing_summary['updated']} "
            f"failed={listing_summary['failed']}"
        )
        if args.listings_only:
            return 1 if listing_summary["failed"] and not listing_summary["updated"] else 0

    if args.listings_only:
        return 0

    args.data_dir.mkdir(parents=True, exist_ok=True)

    summary = run_pipeline(
        config_path=args.config,
        data_dir=args.data_dir,
        intervals=args.intervals,
        sleep_seconds=args.sleep,
        workers=args.workers,
        skip_existing=args.skip_existing,
        asset_classes=args.asset_classes,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )

    rate_pct = 100.0 * float(summary.get("failure_rate", 0.0))
    print(
        f"\nDone. success={summary['success']} "
        f"failed={summary['failed']} skipped={summary['skipped']} "
        f"failure_rate={rate_pct:.2f}%"
    )

    if args.summary_path is not None:
        out = write_fetch_summary(summary, args.summary_path)
        print(f"Wrote fetch summary → {out} (+ {out.with_suffix('.md').name})")

    # Non-zero only if everything failed (partial success is still useful).
    if summary["success"] == 0 and summary["failed"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
