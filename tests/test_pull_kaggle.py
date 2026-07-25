"""Unit tests for scripts.pull_kaggle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.kaggle_util import DatasetSnapshot
from scripts.pull_kaggle import _merge_tree, has_kaggle_credentials, main, pull


def test_has_kaggle_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    with patch("scripts.kaggle_util.Path.home") as home:
        home.return_value = Path("/nonexistent-home")
        assert has_kaggle_credentials() is False
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")
    assert has_kaggle_credentials() is True


def test_merge_tree(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "crypto" / "1d").mkdir(parents=True)
    (src / "crypto" / "1d" / "BTC-USD.csv").write_text("new", encoding="utf-8")
    (src / "dataset-metadata.json").write_text("{}", encoding="utf-8")
    (dest / "crypto" / "1d").mkdir(parents=True)
    (dest / "crypto" / "1d" / "ETH-USD.csv").write_text("keep", encoding="utf-8")

    n = _merge_tree(src, dest)
    assert n == 1
    assert (dest / "crypto" / "1d" / "BTC-USD.csv").read_text(encoding="utf-8") == "new"
    assert (dest / "crypto" / "1d" / "ETH-USD.csv").read_text(encoding="utf-8") == "keep"
    assert not (dest / "dataset-metadata.json").exists()


def test_pull_optional_no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    with patch("scripts.kaggle_util.Path.home") as home:
        home.return_value = tmp_path / "nohome"
        assert pull(data_dir=tmp_path / "data", optional=True) == 0


def test_pull_requires_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    with patch("scripts.kaggle_util.Path.home") as home:
        home.return_value = tmp_path / "nohome"
        with pytest.raises(RuntimeError, match="Missing Kaggle credentials"):
            pull(data_dir=tmp_path / "data", optional=False)


def test_pull_merges_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache"
    (cache / "stocks_us" / "1d").mkdir(parents=True)
    (cache / "stocks_us" / "1d" / "AAPL.csv").write_text("a", encoding="utf-8")
    data = tmp_path / "data"
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")

    snap = DatasetSnapshot(
        handle="benjaminpo/finance-dataset",
        current_version=3,
        status="READY",
        total_bytes=100,
        pending_versions=(),
        failed_versions=(),
        max_version=3,
    )
    mock_hub = MagicMock()
    mock_hub.dataset_download.return_value = str(cache)
    with (
        patch.dict("sys.modules", {"kagglehub": mock_hub}),
        patch("scripts.pull_kaggle.wait_until_ready", return_value=snap),
    ):
        n = pull(data_dir=data, force=True)
    assert n == 1
    assert (data / "stocks_us" / "1d" / "AAPL.csv").is_file()
    assert (data / ".kaggle-pull-state.json").is_file()
    assert not cache.exists()  # kagglehub cache removed after merge
    mock_hub.dataset_download.assert_called_once()


def test_pull_optional_only_on_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")
    with patch(
        "scripts.pull_kaggle.wait_until_ready",
        side_effect=RuntimeError("404 Not Found: dataset missing"),
    ):
        assert pull(data_dir=tmp_path / "data", optional=True) == 0


def test_pull_optional_does_not_swallow_auth_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")
    with patch(
        "scripts.pull_kaggle.wait_until_ready",
        side_effect=RuntimeError("403 Client Error: permission denied"),
    ):
        with pytest.raises(RuntimeError, match="403"):
            pull(data_dir=tmp_path / "data", optional=True)


def test_pull_optional_does_not_swallow_download_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once Ready, a version archive 404 must fail — not continue with empty data/."""
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")
    snap = DatasetSnapshot(
        handle="benjaminpo/finance-dataset",
        current_version=9,
        status="READY",
        total_bytes=100,
        pending_versions=(),
        failed_versions=(),
        max_version=9,
    )
    mock_hub = MagicMock()
    mock_hub.dataset_download.side_effect = RuntimeError(
        "404 Client Error. Resource not found at URL: "
        "https://kaggle.com/datasets/benjaminpo/finance-dataset/versions/9"
    )
    with (
        patch.dict("sys.modules", {"kagglehub": mock_hub}),
        patch("scripts.pull_kaggle.wait_until_ready", return_value=snap),
        patch("scripts.pull_kaggle.time.sleep"),
    ):
        with pytest.raises(RuntimeError, match="versions/9"):
            pull(
                data_dir=tmp_path / "data",
                optional=True,
                download_retries=3,
                download_retry_sec=0,
            )
    assert mock_hub.dataset_download.call_count == 3


def test_download_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")
    cache = tmp_path / "cache"
    (cache / "crypto" / "1m").mkdir(parents=True)
    (cache / "crypto" / "1m" / "BTC-USD.csv").write_text("x", encoding="utf-8")
    snap = DatasetSnapshot(
        handle="benjaminpo/finance-dataset",
        current_version=9,
        status="READY",
        total_bytes=100,
        pending_versions=(),
        failed_versions=(),
        max_version=9,
    )
    mock_hub = MagicMock()
    mock_hub.dataset_download.side_effect = [
        RuntimeError("404 Client Error: versions/9 not ready"),
        str(cache),
    ]
    with (
        patch.dict("sys.modules", {"kagglehub": mock_hub}),
        patch("scripts.pull_kaggle.wait_until_ready", return_value=snap),
        patch("scripts.pull_kaggle.time.sleep"),
    ):
        n = pull(
            data_dir=tmp_path / "data",
            optional=True,
            download_retries=3,
            download_retry_sec=0,
        )
    assert n == 1
    assert mock_hub.dataset_download.call_count == 2
    assert (tmp_path / "data" / "crypto" / "1m" / "BTC-USD.csv").is_file()


def test_main_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    with patch("scripts.kaggle_util.Path.home") as home:
        home.return_value = tmp_path / "nohome"
        assert main(["--data-dir", str(tmp_path / "data"), "--optional"]) == 0
