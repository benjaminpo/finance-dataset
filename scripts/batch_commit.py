#!/usr/bin/env python3
"""Commit and push listing CSV changes in small batches."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Listings stay in git; OHLCV under data/ is published to Kaggle instead.
DEFAULT_ROOTS = ("config/listings",)
DEFAULT_BATCH_SIZE = 400


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def git_output(cmd: list[str]) -> str:
    return run(cmd).stdout


def expand_paths(paths: list[str]) -> list[str]:
    """Expand directory entries from `git status` into individual file paths."""
    files: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(str(p) for p in sorted(path.rglob("*")) if p.is_file())
        elif path.is_file():
            files.append(raw)
    return files


def filter_ignored(files: list[str]) -> list[str]:
    """Drop paths ignored by .gitignore (e.g. .DS_Store)."""
    if not files:
        return files
    proc = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input="\n".join(files) + "\n",
        check=False,
        text=True,
        capture_output=True,
    )
    ignored = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return [f for f in files if f not in ignored]


def changed_files(roots: tuple[str, ...]) -> list[str]:
    """Return tracked/untracked paths under *roots* that differ from HEAD."""
    paths: list[str] = []
    for root in roots:
        if not Path(root).exists():
            continue
        out = git_output(["git", "status", "--porcelain", root])
        for line in out.splitlines():
            if not line.strip():
                continue
            # XY path (or "-> rename"); path starts at column 3
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            paths.append(path)
    files = filter_ignored(expand_paths(paths))
    # Stable order: listings first, then data sorted by path.
    return sorted(set(files), key=lambda p: (0 if p.startswith("config/") else 1, p))


def batch_label(paths: list[str]) -> str:
    """Short human-readable label for a commit batch."""
    data_paths = [p for p in paths if p.startswith("data/")]
    if not data_paths:
        return "listings"
    parts = {Path(p).parts[1:3] for p in data_paths if len(Path(p).parts) >= 3}
    if len(parts) == 1:
        asset, interval = next(iter(parts))
        return f"{asset}/{interval}"
    return "mixed"


def push_with_retry(branch: str, attempts: int = 4) -> None:
    for attempt in range(1, attempts + 1):
        result = run(["git", "push", "origin", f"HEAD:{branch}"], check=False)
        if result.returncode == 0:
            return
        print(result.stderr or result.stdout, file=sys.stderr)
        if attempt == attempts:
            raise subprocess.CalledProcessError(result.returncode, "git push")
        print(f"Push failed (attempt {attempt}/{attempts}); rebasing on origin/{branch}...")
        run(["git", "pull", "--rebase", "origin", branch])


def batch_commit(
    roots: tuple[str, ...] = DEFAULT_ROOTS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    push: bool = True,
) -> int:
    """
    Stage, commit, and optionally push changes in batches.

    Returns the number of commits created.
    """
    files = changed_files(roots)
    if not files:
        print("No changes under", ", ".join(roots))
        return 0

    branch = git_output(["git", "branch", "--show-current"]).strip() or "main"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    commits = 0
    total_batches = (len(files) + batch_size - 1) // batch_size

    for idx in range(0, len(files), batch_size):
        batch = files[idx : idx + batch_size]
        batch_no = idx // batch_size + 1
        label = batch_label(batch)
        run(["git", "add", "--"] + batch)
        if run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
            continue
        msg = f"chore: update financial data {date} ({label}, batch {batch_no}/{total_batches})"
        run(["git", "commit", "-m", msg])
        commits += 1
        if push:
            push_with_retry(branch)
        print(f"Committed batch {batch_no}/{total_batches} ({len(batch)} file(s)): {label}")

    print(f"Done: {commits} commit(s), {len(files)} file(s) total.")
    return commits


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Commit listing CSV changes in batches and push.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Max files per commit (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Create commits locally without pushing",
    )
    parser.add_argument(
        "--roots",
        nargs="+",
        default=list(DEFAULT_ROOTS),
        help="Directories to include (default: config/listings)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        batch_commit(
            roots=tuple(args.roots),
            batch_size=max(1, args.batch_size),
            push=not args.no_push,
        )
    except subprocess.CalledProcessError as exc:
        print(exc.stderr if hasattr(exc, "stderr") and exc.stderr else exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
