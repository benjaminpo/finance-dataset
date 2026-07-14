#!/usr/bin/env python3
"""Download the latest Kaggle dataset version into data/ before a partial fetch."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

DEFAULT_HANDLE = "benjaminpo/finance-dataset"
DEFAULT_DATA_DIR = "data"
SKIP_NAMES = {".DS_Store", "dataset-metadata.json"}


def has_kaggle_credentials() -> bool:
    if os.environ.get("KAGGLE_API_TOKEN"):
        return True
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        return True
    home = Path.home() / ".kaggle"
    return (home / "access_token").is_file() or (home / "kaggle.json").is_file()


def _merge_tree(src: Path, dest: Path) -> int:
    """Copy files from *src* into *dest*, overwriting. Returns file count."""
    copied = 0
    for path in src.rglob("*"):
        if not path.is_file() or path.name in SKIP_NAMES:
            continue
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def pull(
    handle: str = DEFAULT_HANDLE,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    *,
    force: bool = True,
    optional: bool = False,
) -> int:
    """
    Download *handle* into *data_dir*.

    Returns the number of files copied. When *optional* is True, download
    failures (missing credentials, empty dataset, network) print a warning
    and return 0 instead of raising.
    """
    dest = Path(data_dir)
    dest.mkdir(parents=True, exist_ok=True)

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
        import kagglehub

        print(f"Downloading https://www.kaggle.com/datasets/{handle} ...", flush=True)
        cache_path = Path(
            kagglehub.dataset_download(
                handle,
                force_download=force,
            )
        )
        # Prefer copying from the cache path so we merge into an existing data/
        # tree instead of requiring an empty output_dir.
        if cache_path.resolve() == dest.resolve():
            n = sum(
                1
                for p in dest.rglob("*")
                if p.is_file() and p.name not in SKIP_NAMES | {".gitkeep"}
            )
            print(f"Dataset already at {dest} ({n} file(s))", flush=True)
            return n

        n = _merge_tree(cache_path, dest)
        print(f"Pulled {n} file(s) into {dest}", flush=True)
        return n
    except Exception as exc:  # noqa: BLE001 — optional soft-fail for first CI run
        if optional:
            print(
                f"WARNING: Kaggle pull failed ({exc}). Continuing with local data/.",
                flush=True,
            )
            return 0
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the latest Kaggle OHLCV dataset into data/.",
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
        help="Warn and continue if pull fails (first publish / no credentials)",
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
        )
    except (RuntimeError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
