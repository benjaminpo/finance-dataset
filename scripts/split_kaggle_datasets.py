#!/usr/bin/env python3
"""
One-time migration: split the combined Kaggle dataset into daily + intradaily.

Pulls the legacy combined handle, publishes intradaily intervals to
finance-dataset-intraday, then republishes daily intervals to finance-dataset
(with --allow-shrink so intradaily files leave the daily dataset).

Usage:
  export KAGGLE_API_TOKEN=...
  python scripts/split_kaggle_datasets.py
  python scripts/split_kaggle_datasets.py --only daily   # resume after intradaily upload
  python scripts/split_kaggle_datasets.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.kaggle_util import DAILY_HANDLE, SLICES, wait_until_ready
from scripts.publish_kaggle import publish
from scripts.pull_kaggle import pull

DEFAULT_SOURCE = DAILY_HANDLE


def split(
    *,
    source_handle: str = DEFAULT_SOURCE,
    data_dir: str | Path = "data",
    dry_run: bool = False,
    wait_ready: bool = True,
    only: str = "both",
) -> None:
    if only not in {"both", "daily", "intraday"}:
        raise ValueError(f"only must be both|daily|intraday, got {only!r}")

    data_path = Path(data_dir)
    print(f"=== Pulling combined source {source_handle} ===", flush=True)
    n = pull(
        handle=source_handle,
        data_dir=data_path,
        optional=False,
        wait_ready=wait_ready,
    )
    if n <= 0:
        raise RuntimeError(f"No files pulled from {source_handle}")

    daily = SLICES["daily"]
    intradaily = SLICES["intraday"]

    if only in {"both", "intraday"}:
        print(f"\n=== Publishing intradaily slice → {intradaily.handle} ===", flush=True)
        publish(
            handle=intradaily.handle,
            data_dir=data_path,
            metadata=intradaily.metadata,
            version_notes="Initial split: intradaily snapshots from combined dataset",
            dry_run=dry_run,
            wait_ready=wait_ready,
            allow_shrink=True,
            required_intervals=(),
            include_intervals=intradaily.intervals,
        )
    elif wait_ready and not dry_run:
        # Resume path: intradaily already uploaded; wait for it before shrinking daily.
        print(
            f"\n=== Waiting for existing intradaily dataset {intradaily.handle} ===",
            flush=True,
        )
        wait_until_ready(intradaily.handle)

    if only in {"both", "daily"}:
        print(f"\n=== Publishing daily slice → {daily.handle} ===", flush=True)
        publish(
            handle=daily.handle,
            data_dir=data_path,
            metadata=daily.metadata,
            version_notes="Split: daily/weekly only (intradaily moved to separate dataset)",
            dry_run=dry_run,
            wait_ready=wait_ready,
            allow_shrink=True,
            required_intervals=(),
            include_intervals=daily.intervals,
        )

    print(
        "\nDone. Daily and intradaily CI workflows can now use --slice independently.",
        flush=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split combined Kaggle dataset into daily + intradaily handles.",
    )
    parser.add_argument(
        "--source-handle",
        default=DEFAULT_SOURCE,
        help=f"Combined dataset to split (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Local working directory (default: data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pull + plan publishes without uploading",
    )
    parser.add_argument(
        "--no-wait-ready",
        action="store_true",
        help="Skip Ready waits (not recommended)",
    )
    parser.add_argument(
        "--only",
        choices=("both", "daily", "intraday"),
        default="both",
        help="Publish both slices, or resume with only daily / only intradaily",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        split(
            source_handle=args.source_handle,
            data_dir=args.data_dir,
            dry_run=args.dry_run,
            wait_ready=not args.no_wait_ready,
            only=args.only,
        )
    except Exception as exc:  # noqa: BLE001 — surface Kaggle HTTP errors cleanly
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
