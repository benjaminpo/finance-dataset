"""Unit tests for scripts.kaggle_util."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.kaggle_util import (
    DatasetSnapshot,
    count_data_files,
    count_data_files_by_interval,
    get_slice,
    interval_ignore_patterns,
    is_missing_archive_error,
    is_missing_dataset_error,
    is_transient_dataset_access_error,
    newest_archived_version,
    read_pull_state,
    split_handle,
    versioned_dataset_handle,
    wait_until_ready,
    write_pull_state,
)


def test_split_handle() -> None:
    assert split_handle("benjaminpo/finance-dataset") == ("benjaminpo", "finance-dataset")
    with pytest.raises(ValueError, match="Invalid"):
        split_handle("nope")


def test_count_skips_state_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "crypto" / "1d").mkdir(parents=True)
    (data / "crypto" / "1d" / "BTC.csv").write_text("x", encoding="utf-8")
    (data / ".gitkeep").write_text("", encoding="utf-8")
    (data / "dataset-metadata.json").write_text("{}", encoding="utf-8")
    write_pull_state(data, "owner/slug", 2, 1)
    assert count_data_files(data) == 1
    assert count_data_files_by_interval(data) == {"1d": 1}


def test_count_filters_intervals(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "crypto" / "1d").mkdir(parents=True)
    (data / "crypto" / "1m").mkdir(parents=True)
    (data / "crypto" / "1d" / "BTC.csv").write_text("x", encoding="utf-8")
    (data / "crypto" / "1m" / "BTC.csv").write_text("x", encoding="utf-8")
    assert count_data_files(data, intervals=("1d",)) == 1
    assert count_data_files_by_interval(data, intervals=("1m",)) == {"1m": 1}


def test_get_slice_and_ignore_patterns() -> None:
    daily = get_slice("daily")
    assert daily.handle == "benjaminpo/finance-dataset"
    assert "1d" in daily.intervals
    assert "1m" not in daily.intervals
    intradaily = get_slice("intraday")
    assert intradaily.handle.endswith("-intraday")
    assert "**/1d/**" in interval_ignore_patterns(intradaily.intervals)
    assert "**/1m/**" not in interval_ignore_patterns(intradaily.intervals)
    with pytest.raises(ValueError, match="Unknown Kaggle slice"):
        get_slice("weekly")


def test_pull_state_roundtrip(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    write_pull_state(data, "owner/slug", 3, 42)
    assert read_pull_state(data) == {
        "handle": "owner/slug",
        "version": 3,
        "file_count": 42,
        "interval_counts": {},
        "intervals": None,
    }


def test_is_missing_dataset_error() -> None:
    assert is_missing_dataset_error(RuntimeError("404 Not Found"))
    assert is_missing_dataset_error(RuntimeError("Dataset does not exist"))
    assert not is_missing_dataset_error(RuntimeError("403 permission"))


def test_is_missing_archive_error() -> None:
    plain = RuntimeError("404 Client Error. Resource not found")
    assert is_missing_archive_error(plain) is False
    missing = RuntimeError("404 Client Error")
    missing.response = type("Resp", (), {"text": '{"error":{"message":"No gcs url found"}}'})()
    assert is_missing_archive_error(missing) is True


def test_versioned_dataset_handle() -> None:
    assert versioned_dataset_handle("owner/slug", None) == "owner/slug"
    assert versioned_dataset_handle("owner/slug", 77) == "owner/slug/versions/77"
    assert versioned_dataset_handle("owner/slug/versions/9", 8) == "owner/slug/versions/8"


def test_newest_archived_version_skips_missing_bundle() -> None:
    import requests

    closed: list[int] = []

    class _Resp:
        def close(self) -> None:
            closed.append(1)

    def download(req: object) -> _Resp:
        version = getattr(req, "dataset_version_number")
        if version == 78:
            err = requests.HTTPError("404 Client Error")
            err.response = type(  # type: ignore[attr-defined]
                "Resp",
                (),
                {"status_code": 404, "text": '{"error":{"message":"No gcs url found"}}'},
            )()
            raise err
        return _Resp()

    client = MagicMock()
    client.__enter__.return_value = client
    client.datasets.dataset_api_client.download_dataset.side_effect = download
    with patch("kagglehub.clients.build_kaggle_client", return_value=client):
        assert newest_archived_version("benjaminpo/finance-dataset", 78) == 77
    assert closed == [1]


def test_newest_archived_version_none_when_current_is_first() -> None:
    assert newest_archived_version("owner/slug", 1) is None


def test_newest_archived_version_reraises_unexpected_http_error() -> None:
    import requests

    def download(req: object) -> None:
        err = requests.HTTPError("500 Server Error")
        err.response = type("Resp", (), {"status_code": 500, "text": "boom"})()
        raise err

    client = MagicMock()
    client.__enter__.return_value = client
    client.datasets.dataset_api_client.download_dataset.side_effect = download
    with (
        patch("kagglehub.clients.build_kaggle_client", return_value=client),
        pytest.raises(requests.HTTPError, match="500"),
    ):
        newest_archived_version("owner/slug", 4, max_back=1)


def test_newest_archived_version_returns_none_when_window_has_no_bundle() -> None:
    def download(req: object) -> None:
        raise RuntimeError("404 not found")

    client = MagicMock()
    client.__enter__.return_value = client
    client.datasets.dataset_api_client.download_dataset.side_effect = download
    with patch("kagglehub.clients.build_kaggle_client", return_value=client):
        assert newest_archived_version("owner/slug", 3, max_back=1) is None


def test_is_transient_dataset_access_error() -> None:
    assert is_transient_dataset_access_error(
        RuntimeError(
            "403 Client Error. You don't have permission to access resource "
            "at URL: .../GetDataset"
        )
    )
    assert not is_transient_dataset_access_error(RuntimeError("401 unauthorized"))
    assert not is_transient_dataset_access_error(RuntimeError("404 Not Found"))


def test_wait_until_ready_retries_transient_403() -> None:
    snap = DatasetSnapshot(
        handle="o/s",
        current_version=1,
        status="READY",
        total_bytes=10,
        pending_versions=(),
        failed_versions=(),
        max_version=1,
    )
    with patch(
        "scripts.kaggle_util.get_dataset_snapshot",
        side_effect=[
            RuntimeError("403 Client Error. You don't have permission"),
            snap,
        ],
    ):
        assert wait_until_ready("o/s", timeout_sec=2, poll_sec=0.01).current_version == 1


def test_wait_until_ready_success() -> None:
    snap = DatasetSnapshot(
        handle="o/s",
        current_version=2,
        status="READY",
        total_bytes=10,
        pending_versions=(),
        failed_versions=(),
        max_version=2,
    )
    with patch("scripts.kaggle_util.get_dataset_snapshot", return_value=snap):
        assert wait_until_ready("o/s", timeout_sec=1, poll_sec=0.01).current_version == 2


def test_wait_until_ready_timeout() -> None:
    snap = DatasetSnapshot(
        handle="o/s",
        current_version=1,
        status="READY",
        total_bytes=10,
        pending_versions=(2,),
        failed_versions=(),
        max_version=2,
    )
    with patch("scripts.kaggle_util.get_dataset_snapshot", return_value=snap):
        with pytest.raises(TimeoutError, match="Timed out"):
            wait_until_ready("o/s", min_version=2, timeout_sec=0.05, poll_sec=0.01)


def test_wait_until_ready_failed_version() -> None:
    snap = DatasetSnapshot(
        handle="o/s",
        current_version=1,
        status="READY",
        total_bytes=10,
        pending_versions=(),
        failed_versions=(2,),
        max_version=2,
    )
    with patch("scripts.kaggle_util.get_dataset_snapshot", return_value=snap):
        with pytest.raises(RuntimeError, match="failed version"):
            wait_until_ready("o/s", min_version=2, timeout_sec=1, poll_sec=0.01)


def test_wait_until_ready_ignores_stale_failed_version() -> None:
    """Pull should use current READY even if a newer upload failed."""
    snap = DatasetSnapshot(
        handle="o/s",
        current_version=7,
        status="READY",
        total_bytes=10,
        pending_versions=(),
        failed_versions=(8,),
        max_version=8,
    )
    assert snap.is_ready is True
    with patch("scripts.kaggle_util.get_dataset_snapshot", return_value=snap):
        assert wait_until_ready("o/s", timeout_sec=1, poll_sec=0.01).current_version == 7


def test_wait_until_ready_ignores_stale_failures_while_waiting_for_newer() -> None:
    """Publish must not abort on older failed versions while a newer one is pending."""
    pending = DatasetSnapshot(
        handle="o/s",
        current_version=11,
        status="READY",
        total_bytes=10,
        pending_versions=(14,),
        failed_versions=(8, 10, 12, 13),
        max_version=14,
    )
    ready = DatasetSnapshot(
        handle="o/s",
        current_version=14,
        status="READY",
        total_bytes=10,
        pending_versions=(),
        failed_versions=(8, 10, 12, 13),
        max_version=14,
    )
    with patch(
        "scripts.kaggle_util.get_dataset_snapshot",
        side_effect=[pending, ready],
    ):
        assert (
            wait_until_ready("o/s", min_version=14, timeout_sec=2, poll_sec=0.01).current_version
            == 14
        )


def test_dataset_snapshot_not_ready_when_pending() -> None:
    snap = DatasetSnapshot(
        handle="o/s",
        current_version=1,
        status="READY",
        total_bytes=1,
        pending_versions=(2,),
        failed_versions=(),
        max_version=2,
    )
    assert snap.is_ready is False
