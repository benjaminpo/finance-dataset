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


def count_data_files(data_dir: Path) -> int:
    if not data_dir.is_dir():
        return 0
    return sum(
        1
        for p in data_dir.rglob("*")
        if p.is_file() and p.name not in SKIP_COUNT_NAMES
    )


def count_data_files_by_interval(data_dir: Path) -> dict[str, int]:
    """Count files in the expected ``asset_class/interval/file`` layout."""
    counts: dict[str, int] = {}
    if not data_dir.is_dir():
        return counts
    for path in data_dir.rglob("*"):
        if not path.is_file() or path.name in SKIP_COUNT_NAMES:
            continue
        parts = path.relative_to(data_dir).parts
        if len(parts) < 3:
            continue
        interval = parts[1]
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
        last = get_dataset_snapshot(handle)
        version_ok = min_version is None or last.current_version >= min_version
        # Only abort when the version we are waiting for failed (publish flow).
        # A stale failed version ahead of current must not block pulling READY data.
        if (
            min_version is not None
            and last.failed_versions
            and any(v >= min_version for v in last.failed_versions)
        ):
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


def write_pull_state(data_dir: Path, handle: str, version: int, file_count: int) -> Path:
    import json

    path = Path(data_dir) / PULL_STATE_NAME
    path.write_text(
        json.dumps(
            {
                "handle": handle,
                "version": version,
                "file_count": file_count,
                "interval_counts": count_data_files_by_interval(data_dir),
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
