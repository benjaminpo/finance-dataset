#!/usr/bin/env python3
"""Download the latest Ready Kaggle dataset version into data/ before a partial fetch."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.kaggle_util import (
    SKIP_COUNT_NAMES,
    clear_pull_state,
    count_data_files,
    get_dataset_snapshot,
    has_kaggle_credentials,
    is_missing_dataset_error,
    wait_until_ready,
    write_pull_state,
)

DEFAULT_HANDLE = "benjaminpo/finance-dataset"
DEFAULT_DATA_DIR = "data"
DEFAULT_READY_TIMEOUT_SEC = 14400
DEFAULT_READY_POLL_SEC = 60
# Kaggle can mark a version READY before the downloadable archive is served.
DEFAULT_DOWNLOAD_RETRIES = 8
DEFAULT_DOWNLOAD_RETRY_SEC = 30.0


def _merge_tree(src: Path, dest: Path) -> int:
    """Copy files from *src* into *dest*, overwriting. Returns file count."""
    copied = 0
    for path in src.rglob("*"):
        if not path.is_file() or path.name in SKIP_COUNT_NAMES:
            continue
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def _download_dataset(
    handle: str,
    *,
    force: bool,
    version: int,
    retries: int = DEFAULT_DOWNLOAD_RETRIES,
    retry_sec: float = DEFAULT_DOWNLOAD_RETRY_SEC,
) -> Path:
    """Download *handle*, retrying when READY metadata races the archive."""
    import kagglehub

    attempts = max(1, retries)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return Path(kagglehub.dataset_download(handle, force_download=force))
        except Exception as exc:  # noqa: BLE001 — kagglehub raises various HTTP errors
            last_exc = exc
            # Only retry "not found" — auth/quota/etc. should fail immediately.
            if not is_missing_dataset_error(exc) or attempt >= attempts:
                raise
            print(
                f"WARNING: download of {handle} v{version} not available yet "
                f"(attempt {attempt}/{attempts}): {exc}. "
                f"Retrying in {retry_sec:.0f}s...",
                flush=True,
            )
            time.sleep(retry_sec)
    assert last_exc is not None
    raise last_exc


def pull(
    handle: str = DEFAULT_HANDLE,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    *,
    force: bool = True,
    optional: bool = False,
    wait_ready: bool = True,
    ready_timeout_sec: float = DEFAULT_READY_TIMEOUT_SEC,
    ready_poll_sec: float = DEFAULT_READY_POLL_SEC,
    download_retries: int = DEFAULT_DOWNLOAD_RETRIES,
    download_retry_sec: float = DEFAULT_DOWNLOAD_RETRY_SEC,
) -> int:
    """
    Download *handle* into *data_dir*.

    Returns the number of files copied. When *optional* is True, a missing
    dataset (first publish) warns and returns 0. Auth and other errors still
    raise — soft-failing those previously allowed CI to publish a partial tree
    and wipe the other slice.

    Once the dataset is confirmed to exist (Ready snapshot), download failures
    always raise — including 404s on a specific version URL. Soft-failing those
    left CI with only the refreshed slice and no intradaily history.
    """
    dest = Path(data_dir)
    dest.mkdir(parents=True, exist_ok=True)
    clear_pull_state(dest)

    if not has_kaggle_credentials():
        msg = (
            "Missing Kaggle credentials. Set KAGGLE_API_TOKEN "
            "(or KAGGLE_USERNAME + KAGGLE_KEY), or place credentials in ~/.kaggle/."
        )
        if optional:
            print(f"WARNING: {msg} Continuing with local data/.", flush=True)
            return 0
        raise RuntimeError(msg)

    try:
        if wait_ready:
            snap = wait_until_ready(
                handle,
                timeout_sec=ready_timeout_sec,
                poll_sec=ready_poll_sec,
            )
        else:
            snap = get_dataset_snapshot(handle)
    except Exception as exc:  # noqa: BLE001 — optional soft-fail for first CI run
        if optional and is_missing_dataset_error(exc):
            print(
                f"WARNING: Kaggle dataset {handle} not found yet ({exc}). "
                "Continuing with local data/.",
                flush=True,
            )
            return 0
        raise

    version = snap.current_version
    print(
        f"Downloading https://www.kaggle.com/datasets/{handle} "
        f"(v{version}) ...",
        flush=True,
    )
    cache_path = _download_dataset(
        handle,
        force=force,
        version=version,
        retries=download_retries,
        retry_sec=download_retry_sec,
    )
    # Prefer copying from the cache path so we merge into an existing data/
    # tree instead of requiring an empty output_dir.
    if cache_path.resolve() == dest.resolve():
        n = count_data_files(dest)
        print(f"Dataset already at {dest} ({n} file(s), v{version})", flush=True)
    else:
        n = _merge_tree(cache_path, dest)
        print(f"Pulled {n} file(s) into {dest} (v{version})", flush=True)
        # Drop the kagglehub cache copy so CI does not keep the dataset twice
        # (runners often have only ~14GB free).
        try:
            shutil.rmtree(cache_path)
            print(f"Removed kagglehub cache at {cache_path}", flush=True)
        except OSError as exc:
            print(f"WARNING: could not remove kagglehub cache ({exc})", flush=True)

    write_pull_state(dest, handle, version, n)
    return n


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the latest Ready Kaggle OHLCV dataset into data/.",
    )
    parser.add_argument(
        "--handle",
        default=os.environ.get("KAGGLE_DATASET_HANDLE", DEFAULT_HANDLE),
        help=f"Kaggle dataset handle (default: {DEFAULT_HANDLE})",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="Local directory to merge into (default: data)",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Allow kagglehub cache hit (default: force fresh download)",
    )
    parser.add_argument(
        "--optional",
        action="store_true",
        help="Warn and continue only if the dataset is missing (first publish)",
    )
    parser.add_argument(
        "--no-wait-ready",
        action="store_true",
        help="Skip waiting for Kaggle processing to finish before download",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        pull(
            handle=args.handle,
            data_dir=args.data_dir,
            force=not args.no_force,
            optional=args.optional,
            wait_ready=not args.no_wait_ready,
            ready_timeout_sec=args.ready_timeout_sec,
            ready_poll_sec=args.ready_poll_sec,
        )
    except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
