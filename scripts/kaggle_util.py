"""Shared Kaggle helpers for pull/publish (ready polling, credentials, counts)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

METADATA_NAME = "dataset-metadata.json"
PULL_STATE_NAME = ".kaggle-pull-state.json"
SKIP_COUNT_NAMES = {".gitkeep", METADATA_NAME, PULL_STATE_NAME, ".DS_Store"}

READY_STATUS_NAMES = {"READY"}
FAILED_STATUS_NAMES = {"FAILED", "DELETED"}

# Two Kaggle datasets: daily/weekly cumulative vs intradaily snapshots.
# Keeps Ready indexing smaller and lets the workflows run in parallel.
DAILY_HANDLE = "benjaminpo/finance-dataset"
INTRADAY_HANDLE = "benjaminpo/finance-dataset-intraday"
DAILY_METADATA = "config/kaggle/dataset-metadata.json"
INTRADAY_METADATA = "config/kaggle/dataset-metadata-intraday.json"

# All cumulative intervals belong on the daily dataset (CI publishes 1d+1wk).
DAILY_INTERVALS: tuple[str, ...] = ("1d", "5d", "1wk", "1mo", "3mo")
DAILY_REQUIRE_INTERVALS: tuple[str, ...] = ("1d", "1wk")
INTRADAY_INTERVALS: tuple[str, ...] = (
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "90m",
    "1h",
)
INTRADAY_REQUIRE_INTERVALS: tuple[str, ...] = INTRADAY_INTERVALS
KNOWN_INTERVALS: tuple[str, ...] = DAILY_INTERVALS + INTRADAY_INTERVALS


@dataclass(frozen=True)
class DatasetSlice:
    """Named Kaggle publish/pull target (daily vs intradaily)."""

    name: str
    handle: str
    metadata: str
    intervals: tuple[str, ...]
    require_intervals: tuple[str, ...]


SLICES: dict[str, DatasetSlice] = {
    "daily": DatasetSlice(
        name="daily",
        handle=DAILY_HANDLE,
        metadata=DAILY_METADATA,
        intervals=DAILY_INTERVALS,
        require_intervals=DAILY_REQUIRE_INTERVALS,
    ),
    "intraday": DatasetSlice(
        name="intraday",
        handle=INTRADAY_HANDLE,
        metadata=INTRADAY_METADATA,
        intervals=INTRADAY_INTERVALS,
        require_intervals=INTRADAY_REQUIRE_INTERVALS,
    ),
}


def get_slice(name: str) -> DatasetSlice:
    key = name.strip().lower()
    try:
        return SLICES[key]
    except KeyError as exc:
        known = ", ".join(sorted(SLICES))
        raise ValueError(f"Unknown Kaggle slice {name!r}; expected one of: {known}") from exc


def interval_ignore_patterns(include_intervals: tuple[str, ...] | list[str]) -> list[str]:
    """kagglehub ignore_patterns that drop interval dirs outside *include_intervals*."""
    include = set(include_intervals)
    return [f"**/{interval}/**" for interval in KNOWN_INTERVALS if interval not in include]


def has_kaggle_credentials() -> bool:
    if os.environ.get("KAGGLE_API_TOKEN"):
        return True
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        return True
    home = Path.home() / ".kaggle"
    return (home / "access_token").is_file() or (home / "kaggle.json").is_file()


def split_handle(handle: str) -> tuple[str, str]:
    parts = handle.strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid Kaggle dataset handle: {handle!r}")
    return parts[0], parts[1]


def count_data_files(
    data_dir: Path,
    *,
    intervals: tuple[str, ...] | list[str] | None = None,
) -> int:
    if not data_dir.is_dir():
        return 0
    allowed = set(intervals) if intervals is not None else None
    total = 0
    for path in data_dir.rglob("*"):
        if not path.is_file() or path.name in SKIP_COUNT_NAMES:
            continue
        if allowed is not None:
            parts = path.relative_to(data_dir).parts
            if len(parts) < 3 or parts[1] not in allowed:
                continue
        total += 1
    return total


def count_data_files_by_interval(
    data_dir: Path,
    *,
    intervals: tuple[str, ...] | list[str] | None = None,
) -> dict[str, int]:
    """Count files in the expected ``asset_class/interval/file`` layout."""
    counts: dict[str, int] = {}
    if not data_dir.is_dir():
        return counts
    allowed = set(intervals) if intervals is not None else None
    for path in data_dir.rglob("*"):
        if not path.is_file() or path.name in SKIP_COUNT_NAMES:
            continue
        parts = path.relative_to(data_dir).parts
        if len(parts) < 3:
            continue
        interval = parts[1]
        if allowed is not None and interval not in allowed:
            continue
        counts[interval] = counts.get(interval, 0) + 1
    return dict(sorted(counts.items()))


def _status_name(value: object) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.upper()
    text = str(value)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.upper()


@dataclass(frozen=True)
class DatasetSnapshot:
    handle: str
    current_version: int
    status: str
    total_bytes: int
    pending_versions: tuple[int, ...]
    failed_versions: tuple[int, ...]
    max_version: int

    @property
    def is_ready(self) -> bool:
        if (
            self.status not in READY_STATUS_NAMES
            or self.pending_versions
            or self.current_version <= 0
        ):
            return False
        if self.current_version == self.max_version:
            return True
        # Failed versions ahead of current are not in-flight processing.
        return all(
            v in self.failed_versions
            for v in range(self.current_version + 1, self.max_version + 1)
        )


def get_dataset_snapshot(handle: str) -> DatasetSnapshot:
    """Fetch current dataset version/status from the Kaggle API."""
    from kagglehub.clients import build_kaggle_client
    from kagglehub.exceptions import handle_call
    from kagglesdk.datasets.types.dataset_api_service import (
        ApiGetDatasetRequest,
        ApiGetDatasetStatusRequest,
    )

    owner, slug = split_handle(handle)
    with build_kaggle_client() as api_client:
        req = ApiGetDatasetRequest()
        req.owner_slug = owner
        req.dataset_slug = slug
        dataset = handle_call(lambda: api_client.datasets.dataset_api_client.get_dataset(req))

        status_req = ApiGetDatasetStatusRequest()
        status_req.owner_slug = owner
        status_req.dataset_slug = slug
        status_resp = handle_call(
            lambda: api_client.datasets.dataset_api_client.get_dataset_status(status_req)
        )

    versions = list(getattr(dataset, "versions", None) or [])
    pending: list[int] = []
    failed: list[int] = []
    max_version = int(getattr(dataset, "current_version_number", 0) or 0)
    for ver in versions:
        number = int(getattr(ver, "version_number", 0) or 0)
        max_version = max(max_version, number)
        name = _status_name(getattr(ver, "status", None))
        if name in FAILED_STATUS_NAMES:
            failed.append(number)
        elif name and name not in READY_STATUS_NAMES:
            pending.append(number)

    return DatasetSnapshot(
        handle=handle,
        current_version=int(getattr(dataset, "current_version_number", 0) or 0),
        status=_status_name(getattr(status_resp, "status", None)),
        total_bytes=int(getattr(dataset, "total_bytes", 0) or 0),
        pending_versions=tuple(sorted(pending)),
        failed_versions=tuple(sorted(failed)),
        max_version=max_version,
    )


def is_missing_dataset_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "404",
        "not found",
        "does not exist",
        "couldn't find",
        "could not find",
        "no dataset",
    )
    return any(m in text for m in markers)


def is_transient_dataset_access_error(exc: BaseException) -> bool:
    """
    True for brief 403/permission errors right after creating a private dataset.

    kagglehub can create + upload a new dataset, then GetDataset returns 403
    until Kaggle finishes registering it. Treat that as "not ready yet".
    """
    text = str(exc).lower()
    if "403" not in text and "permission" not in text and "forbidden" not in text:
        return False
    # Permanent auth failures should still abort — missing token / wrong user.
    permanent = (
        "invalid credentials",
        "unauthorized",
        "401",
        "api token",
        "authentication failed",
    )
    return not any(p in text for p in permanent)


def _ready_poll_interval(elapsed_sec: float, base_poll_sec: float) -> float:
    """Poll faster early, then settle on *base_poll_sec* for long Kaggle jobs."""
    if elapsed_sec < 300:
        return min(base_poll_sec, 15.0)
    if elapsed_sec < 1800:
        return min(base_poll_sec, 30.0)
    return base_poll_sec


def wait_until_ready(
    handle: str,
    *,
    min_version: int | None = None,
    timeout_sec: float = 14400,
    poll_sec: float = 60,
) -> DatasetSnapshot:
    """
    Block until the dataset's latest version is Ready and current.

    Kaggle accepts uploads asynchronously ("Files are being processed...").
    Pulling before that finishes returns the previous Ready version, which
    makes a partial fetch look complete and wipes the in-flight version.
    """
    started = time.monotonic()
    deadline = started + timeout_sec
    last: DatasetSnapshot | None = None
    while True:
        try:
            last = get_dataset_snapshot(handle)
        except Exception as exc:  # noqa: BLE001 — retry transient 403 after create
            if not is_transient_dataset_access_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout_sec:.0f}s waiting for {handle} to be Ready "
                    f"(min_version={min_version}). Last error: {exc}"
                ) from exc
            elapsed = time.monotonic() - started
            print(
                f"Waiting for Kaggle dataset access ({handle}: {exc}; "
                f"elapsed={elapsed:.0f}s)...",
                flush=True,
            )
            time.sleep(_ready_poll_interval(elapsed, poll_sec))
            continue

        version_ok = min_version is None or last.current_version >= min_version
        # Only abort when the specific version we are waiting for failed (publish).
        # Stale failures (e.g. v12/v13 while waiting for a new v14) must not abort,
        # and must not block pulling the current READY version either.
        if min_version is not None and min_version in last.failed_versions:
            raise RuntimeError(
                f"Kaggle dataset {handle} has failed version(s) "
                f"{list(last.failed_versions)}; current={last.current_version} "
                f"status={last.status}"
            )
        if last.is_ready and version_ok:
            elapsed = time.monotonic() - started
            print(
                f"Kaggle dataset ready: {handle} v{last.current_version} "
                f"({last.total_bytes} bytes, waited {elapsed:.0f}s)",
                flush=True,
            )
            return last

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout_sec:.0f}s waiting for {handle} to be Ready "
                f"(min_version={min_version}). Last snapshot: current=v{last.current_version} "
                f"max=v{last.max_version} status={last.status} "
                f"pending={list(last.pending_versions)}"
            )

        elapsed = time.monotonic() - started
        detail = (
            f"current=v{last.current_version} max=v{last.max_version} "
            f"status={last.status} pending={list(last.pending_versions)} "
            f"elapsed={elapsed:.0f}s"
        )
        if min_version is not None:
            detail += f" waiting_for>=v{min_version}"
        print(f"Waiting for Kaggle processing ({detail})...", flush=True)
        time.sleep(_ready_poll_interval(elapsed, poll_sec))


def write_pull_state(
    data_dir: Path,
    handle: str,
    version: int,
    file_count: int,
    *,
    intervals: tuple[str, ...] | list[str] | None = None,
) -> Path:
    import json

    path = Path(data_dir) / PULL_STATE_NAME
    path.write_text(
        json.dumps(
            {
                "handle": handle,
                "version": version,
                "file_count": file_count,
                "interval_counts": count_data_files_by_interval(
                    data_dir, intervals=intervals
                ),
                "intervals": list(intervals) if intervals is not None else None,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def read_pull_state(data_dir: Path) -> dict | None:
    import json

    path = Path(data_dir) / PULL_STATE_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clear_pull_state(data_dir: Path) -> None:
    path = Path(data_dir) / PULL_STATE_NAME
    path.unlink(missing_ok=True)
