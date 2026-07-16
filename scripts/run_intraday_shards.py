#!/usr/bin/env python3
"""Run all intraday CI shards sequentially in one job (one Kaggle publish)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.fetcher import run_pipeline  # noqa: E402
from src.listings import refresh_listings  # noqa: E402
from src.summary import write_fetch_summary  # noqa: E402

DEFAULT_SHARDS = _ROOT / "config" / "intraday_shards.yaml"
DEFAULT_CONFIG = _ROOT / "config" / "tickers_intraday.yaml"
DEFAULT_INTERVALS = ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h")


def load_shards(path: Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    shards = raw.get("shards") if isinstance(raw, dict) else None
    if not isinstance(shards, list) or not shards:
        raise ValueError(f"No shards defined in {path}")
    for shard in shards:
        if not isinstance(shard, dict) or "id" not in shard:
            raise ValueError(f"Invalid shard entry in {path}: {shard!r}")
        if "asset_classes" not in shard:
            raise ValueError(f"Shard {shard['id']!r} missing asset_classes")
    return shards


def merge_summaries(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-shard fetch summaries into one report."""
    success = failed = skipped = 0
    by_interval: dict[str, dict[str, int]] = {}
    by_asset: dict[str, dict[str, int]] = {}
    failures: list[dict[str, Any]] = []

    def bump(bucket: dict[str, dict[str, int]], key: str, field: str, n: int) -> None:
        bucket.setdefault(key, {"success": 0, "failed": 0, "skipped": 0})
        bucket[key][field] = bucket[key].get(field, 0) + n

    for part in parts:
        success += int(part.get("success", 0))
        failed += int(part.get("failed", 0))
        skipped += int(part.get("skipped", 0))
        for interval, counts in (part.get("by_interval") or {}).items():
            for field in ("success", "failed", "skipped"):
                bump(by_interval, str(interval), field, int(counts.get(field, 0)))
        for asset_class, counts in (part.get("by_asset_class") or {}).items():
            for field in ("success", "failed", "skipped"):
                bump(by_asset, str(asset_class), field, int(counts.get(field, 0)))
        failures.extend(list(part.get("failures") or []))

    attempted = success + failed
    failure_rate = (failed / attempted) if attempted else 0.0
    return {
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "total": success + failed + skipped,
        "attempted": attempted,
        "failure_rate": failure_rate,
        "by_interval": by_interval,
        "by_asset_class": by_asset,
        "failures": failures,
    }


def run_shards(
    *,
    shards_path: Path = DEFAULT_SHARDS,
    config_path: Path = DEFAULT_CONFIG,
    data_dir: Path | None = None,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    workers: int = 8,
    sleep_seconds: float = 0.25,
    skip_existing: bool = False,
    summary_path: Path | None = None,
) -> dict[str, Any]:
    """Fetch every configured shard into *data_dir* and return merged summary."""
    data_root = data_dir or (_ROOT / "data")
    data_root.mkdir(parents=True, exist_ok=True)
    shards = load_shards(shards_path)

    listing_summary = refresh_listings(config_path)
    print(
        f"Listings: checked={listing_summary['checked']} "
        f"updated={listing_summary['updated']} "
        f"failed={listing_summary['failed']}",
        flush=True,
    )

    parts: list[dict[str, Any]] = []
    for index, shard in enumerate(shards):
        shard_id = str(shard["id"])
        asset_classes = list(shard["asset_classes"])
        shard_index = int(shard.get("shard_index", 0))
        shard_count = int(shard.get("shard_count", 1))
        print(
            f"\n=== Shard {index + 1}/{len(shards)}: {shard_id} "
            f"({', '.join(asset_classes)} {shard_index}/{shard_count}) ===",
            flush=True,
        )
        summary = run_pipeline(
            config_path=config_path,
            data_dir=data_root,
            intervals=list(intervals),
            sleep_seconds=sleep_seconds,
            workers=workers,
            skip_existing=skip_existing,
            asset_classes=asset_classes,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        rate_pct = 100.0 * float(summary.get("failure_rate", 0.0))
        print(
            f"Shard {shard_id}: success={summary['success']} "
            f"failed={summary['failed']} skipped={summary['skipped']} "
            f"failure_rate={rate_pct:.2f}%",
            flush=True,
        )
        parts.append(summary)

    merged = merge_summaries(parts)
    if summary_path is not None:
        out = write_fetch_summary(merged, summary_path)
        print(f"Wrote merged fetch summary → {out}", flush=True)
    return merged


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run all intraday CI shards sequentially (single Kaggle publish).",
    )
    parser.add_argument(
        "--shards",
        type=Path,
        default=DEFAULT_SHARDS,
        help=f"Shard plan YAML (default: {DEFAULT_SHARDS.relative_to(_ROOT)})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Tickers config (default: {DEFAULT_CONFIG.relative_to(_ROOT)})",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_ROOT / "data",
        help="OHLCV output root (default: data/)",
    )
    parser.add_argument(
        "--intervals",
        nargs="+",
        default=list(DEFAULT_INTERVALS),
        help="Intervals to fetch (default: all intraday)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel Yahoo fetch workers per shard (default: 8)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="Sleep seconds after each ticker request (default: 0.25)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip tickers that already have data for today",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=None,
        help="Write merged JSON (+ .md) fetch summary",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not args.config.is_file():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 1
    if not args.shards.is_file():
        print(f"Shard plan not found: {args.shards}", file=sys.stderr)
        return 1

    try:
        merged = run_shards(
            shards_path=args.shards,
            config_path=args.config,
            data_dir=args.data_dir,
            intervals=tuple(args.intervals),
            workers=args.workers,
            sleep_seconds=args.sleep,
            skip_existing=args.skip_existing,
            summary_path=args.summary_path,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(exc, file=sys.stderr)
        return 1

    rate_pct = 100.0 * float(merged.get("failure_rate", 0.0))
    print(
        f"\nAll shards done. success={merged['success']} "
        f"failed={merged['failed']} skipped={merged['skipped']} "
        f"failure_rate={rate_pct:.2f}%",
        flush=True,
    )
    if merged["success"] == 0 and merged["failed"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
