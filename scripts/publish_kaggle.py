#!/usr/bin/env python3
"""Publish local OHLCV CSVs under data/ as a new Kaggle dataset version."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.kaggle_util import (
    METADATA_NAME,
    PULL_STATE_NAME,
    clear_pull_state,
    count_data_files,
    count_data_files_by_interval,
    get_dataset_snapshot,
    has_kaggle_credentials,
    is_missing_dataset_error,
    read_pull_state,
    wait_until_ready,
)

DEFAULT_HANDLE = "benjaminpo/finance-dataset"
DEFAULT_DATA_DIR = "data"
DEFAULT_METADATA = "config/kaggle/dataset-metadata.json"
DEFAULT_READY_TIMEOUT_SEC = 14400
DEFAULT_READY_POLL_SEC = 60
IGNORE_PATTERNS = [".DS_Store", ".gitkeep", "**/__pycache__/", "*.pyc", PULL_STATE_NAME]


def load_metadata(path: Path, handle: str) -> dict:
    meta = json.loads(path.read_text(encoding="utf-8"))
    if "id" not in meta:
        meta["id"] = handle
    if "title" not in meta:
        meta["title"] = handle.split("/", 1)[-1].replace("-", " ").title()
    if "licenses" not in meta:
        meta["licenses"] = [{"name": "CC0-1.0"}]
    return meta


def write_upload_metadata(data_dir: Path, metadata_path: Path, handle: str) -> Path:
    """Write dataset-metadata.json into *data_dir* for the Kaggle upload API."""
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")
    n_files = count_data_files(data_dir)
    if n_files == 0:
        raise FileNotFoundError(f"No data files to publish under {data_dir}")

    dest = data_dir / METADATA_NAME
    meta = load_metadata(metadata_path, handle)
    dest.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return dest


def _guard_no_shrink(
    data_dir: Path,
    n_files: int,
    *,
    allow_shrink: bool,
    required_intervals: tuple[str, ...] = (),
) -> None:
    """Refuse to publish a tree missing required or previously pulled intervals."""
    current_intervals = count_data_files_by_interval(data_dir)
    missing = [interval for interval in required_intervals if current_intervals.get(interval, 0) == 0]
    if missing:
        raise RuntimeError(
            "Refusing to publish: required interval data is missing: "
            f"{', '.join(missing)}. Rebuild that slice before publishing."
        )

    state = read_pull_state(data_dir)
    if not state:
        return
    pulled = int(state.get("file_count") or 0)
    pulled_intervals = {
        str(interval): int(count)
        for interval, count in (state.get("interval_counts") or {}).items()
    }
    reduced_intervals = {
        interval: (count, current_intervals.get(interval, 0))
        for interval, count in pulled_intervals.items()
        if current_intervals.get(interval, 0) < count
    }
    if (pulled <= 0 or n_files >= pulled) and not reduced_intervals:
        return
    msg = (
        f"Refusing to publish {n_files} file(s): this job pulled {pulled} file(s) "
        f"from {state.get('handle')} v{state.get('version')}. Publishing fewer "
        "files would wipe data on Kaggle."
    )
    if reduced_intervals:
        details = ", ".join(
            f"{interval} {before}->{after}"
            for interval, (before, after) in sorted(reduced_intervals.items())
        )
        msg += f" Reduced interval counts: {details}."
    if allow_shrink:
        print(f"WARNING: {msg} Continuing because --allow-shrink was set.", flush=True)
        return
    raise RuntimeError(msg)


def publish(
    handle: str = DEFAULT_HANDLE,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    metadata: str | Path = DEFAULT_METADATA,
    version_notes: str | None = None,
    *,
    dry_run: bool = False,
    wait_ready: bool = True,
    ready_timeout_sec: float = DEFAULT_READY_TIMEOUT_SEC,
    ready_poll_sec: float = DEFAULT_READY_POLL_SEC,
    allow_shrink: bool = False,
    required_intervals: tuple[str, ...] = (),
) -> str:
    """
    Upload *data_dir* as a new version of the Kaggle dataset *handle*.

    Returns the version notes used for the upload.
    """
    data_path = Path(data_dir)
    meta_path = Path(metadata)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Metadata file not found: {meta_path}")
    if not has_kaggle_credentials() and not dry_run:
        raise RuntimeError(
            "Missing Kaggle credentials. Set KAGGLE_API_TOKEN "
            "(or KAGGLE_USERNAME + KAGGLE_KEY), or place credentials in ~/.kaggle/."
        )

    n_files = count_data_files(data_path)
    if n_files == 0:
        raise FileNotFoundError(f"No data files to publish under {data_path}")
    _guard_no_shrink(
        data_path,
        n_files,
        allow_shrink=allow_shrink,
        required_intervals=required_intervals,
    )

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notes = version_notes or f"Daily OHLCV refresh {date} ({n_files} file(s))"
    if f"{n_files} file" not in notes:
        notes = f"{notes} ({n_files} file(s))"

    before_version = 0
    if not dry_run:
        try:
            before_version = get_dataset_snapshot(handle).current_version
        except Exception as exc:  # noqa: BLE001 — first create has no dataset yet
            if not is_missing_dataset_error(exc):
                print(f"WARNING: could not read current Kaggle version ({exc})", flush=True)

    meta_dest = write_upload_metadata(data_path, meta_path, handle)
    try:
        if dry_run:
            print(f"[dry-run] Would upload {n_files} file(s) to {handle}")
            print(f"[dry-run] Upload root: {data_path.resolve()}")
            print(f"[dry-run] Version notes: {notes}")
            return notes

        import kagglehub
        from kagglehub.exceptions import BackendError

        print(
            f"Uploading {n_files} file(s) to https://www.kaggle.com/datasets/{handle} ...",
            flush=True,
        )
        upload_started = time.monotonic()
        try:
            kagglehub.dataset_upload(
                handle,
                str(data_path),
                version_notes=notes,
                ignore_patterns=IGNORE_PATTERNS,
            )
        except BackendError as exc:
            message = str(exc)
            if "Incompatible Dataset Type" in message:
                raise RuntimeError(
                    f"Kaggle rejected a new version of {handle}: Incompatible Dataset Type.\n"
                    "The existing dataset is almost certainly a GitHub-synced (or otherwise "
                    "non-file) dataset, not a normal file upload — check the Data Explorer: "
                    "repo files (README, src/, …) instead of OHLCV CSVs.\n"
                    "Fix: delete that dataset on Kaggle (or publish to a new --handle / "
                    "KAGGLE_DATASET_HANDLE slug), then re-run so kagglehub can create a "
                    "file-based dataset."
                ) from exc
            raise
        upload_sec = time.monotonic() - upload_started
        print(
            f"Published {handle}: {notes} (upload took {upload_sec:.0f}s)",
            flush=True,
        )

        if wait_ready:
            target = before_version + 1 if before_version > 0 else None
            wait_started = time.monotonic()
            wait_until_ready(
                handle,
                min_version=target,
                timeout_sec=ready_timeout_sec,
                poll_sec=ready_poll_sec,
            )
            wait_sec = time.monotonic() - wait_started
            print(f"Kaggle processing finished in {wait_sec:.0f}s", flush=True)

        clear_pull_state(data_path)
        return notes
    finally:
        meta_dest.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish data/ OHLCV CSVs to a Kaggle dataset version.",
    )
    parser.add_argument(
        "--handle",
        default=os.environ.get("KAGGLE_DATASET_HANDLE", DEFAULT_HANDLE),
        help=f"Kaggle dataset handle (default: {DEFAULT_HANDLE})",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="Local OHLCV root to upload (default: data)",
    )
    parser.add_argument(
        "--metadata",
        default=DEFAULT_METADATA,
        help=f"Path to dataset-metadata.json (default: {DEFAULT_METADATA})",
    )
    parser.add_argument(
        "--version-notes",
        default=None,
        help="Kaggle version notes (default: dated summary with file count)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print plan without uploading",
    )
    parser.add_argument(
        "--no-wait-ready",
        action="store_true",
        help="Return immediately after upload without waiting for Ready",
    )
    parser.add_argument(
        "--ready-timeout-sec",
        type=float,
        default=DEFAULT_READY_TIMEOUT_SEC,
        help=f"Max seconds to wait for Ready (default: {DEFAULT_READY_TIMEOUT_SEC})",
    )
    parser.add_argument(
        "--ready-poll-sec",
        type=float,
        default=DEFAULT_READY_POLL_SEC,
        help=f"Seconds between Ready polls (default: {DEFAULT_READY_POLL_SEC})",
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="Allow publishing fewer files than were pulled (dangerous)",
    )
    parser.add_argument(
        "--require-intervals",
        nargs="+",
        default=[],
        help="Refuse to publish unless each listed interval has files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        publish(
            handle=args.handle,
            data_dir=args.data_dir,
            metadata=args.metadata,
            version_notes=args.version_notes,
            dry_run=args.dry_run,
            wait_ready=not args.no_wait_ready,
            ready_timeout_sec=args.ready_timeout_sec,
            ready_poll_sec=args.ready_poll_sec,
            allow_shrink=args.allow_shrink,
            required_intervals=tuple(args.require_intervals),
        )
    except (FileNotFoundError, RuntimeError, OSError, TimeoutError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
