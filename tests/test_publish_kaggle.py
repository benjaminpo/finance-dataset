"""Unit tests for scripts.publish_kaggle."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.publish_kaggle import (
    count_data_files,
    has_kaggle_credentials,
    load_metadata,
    main,
    parse_args,
    publish,
    write_upload_metadata,
)


def _write_metadata(path: Path, handle: str = "benjaminpo/finance-dataset") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "title": "Finance Dataset",
                "id": handle,
                "licenses": [{"name": "CC0-1.0"}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_has_kaggle_credentials_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    with patch("scripts.publish_kaggle.Path.home") as home:
        home.return_value = Path("/nonexistent-home-for-test")
        assert has_kaggle_credentials() is False
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")
    assert has_kaggle_credentials() is True


def test_has_kaggle_credentials_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.setenv("KAGGLE_USERNAME", "u")
    monkeypatch.setenv("KAGGLE_KEY", "k")
    assert has_kaggle_credentials() is True


def test_count_and_write_metadata(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "crypto" / "1d").mkdir(parents=True)
    (data / "crypto" / "1d" / "BTC-USD.csv").write_text("a", encoding="utf-8")
    (data / ".gitkeep").write_text("", encoding="utf-8")
    meta = _write_metadata(tmp_path / "meta.json")

    assert count_data_files(data) == 1
    dest = write_upload_metadata(data, meta, "benjaminpo/finance-dataset")
    try:
        assert dest == data / "dataset-metadata.json"
        loaded = json.loads(dest.read_text(encoding="utf-8"))
        assert loaded["id"] == "benjaminpo/finance-dataset"
        assert count_data_files(data) == 1  # metadata not counted
    finally:
        dest.unlink(missing_ok=True)


def test_write_metadata_requires_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / ".gitkeep").write_text("", encoding="utf-8")
    meta = _write_metadata(tmp_path / "meta.json")
    with pytest.raises(FileNotFoundError, match="No data files"):
        write_upload_metadata(data, meta, "benjaminpo/finance-dataset")


def test_load_metadata_defaults(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text("{}", encoding="utf-8")
    meta = load_metadata(path, "owner/slug-name")
    assert meta["id"] == "owner/slug-name"
    assert meta["title"] == "Slug Name"
    assert meta["licenses"]


def test_publish_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    (data / "indices" / "1d").mkdir(parents=True)
    (data / "indices" / "1d" / "GSPC.csv").write_text("x", encoding="utf-8")
    meta = _write_metadata(tmp_path / "meta.json")
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)

    notes = publish(
        handle="benjaminpo/finance-dataset",
        data_dir=data,
        metadata=meta,
        dry_run=True,
    )
    assert "1 file" in notes
    assert not (data / "dataset-metadata.json").exists()


def test_publish_uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    (data / "rates" / "1d").mkdir(parents=True)
    (data / "rates" / "1d" / "TNX.csv").write_text("x", encoding="utf-8")
    meta = _write_metadata(tmp_path / "meta.json")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")

    mock_hub = MagicMock()
    mock_exc = MagicMock()
    mock_exc.BackendError = type("BackendError", (Exception,), {})
    with patch.dict("sys.modules", {"kagglehub": mock_hub, "kagglehub.exceptions": mock_exc}):
        notes = publish(
            handle="benjaminpo/finance-dataset",
            data_dir=data,
            metadata=meta,
            version_notes="test notes",
        )
    assert notes == "test notes"
    mock_hub.dataset_upload.assert_called_once()
    args, kwargs = mock_hub.dataset_upload.call_args
    assert args[0] == "benjaminpo/finance-dataset"
    assert Path(args[1]) == data
    assert kwargs["version_notes"] == "test notes"
    assert not (data / "dataset-metadata.json").exists()


def test_publish_incompatible_dataset_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "x.csv").write_text("1", encoding="utf-8")
    meta = _write_metadata(tmp_path / "meta.json")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")

    class BackendError(Exception):
        pass

    mock_hub = MagicMock()
    mock_hub.dataset_upload.side_effect = BackendError("Incompatible Dataset Type")
    mock_exc = MagicMock()
    mock_exc.BackendError = BackendError
    with patch.dict("sys.modules", {"kagglehub": mock_hub, "kagglehub.exceptions": mock_exc}):
        with pytest.raises(RuntimeError, match="GitHub-synced"):
            publish(data_dir=data, metadata=meta, dry_run=False)


def test_publish_missing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "x.csv").write_text("1", encoding="utf-8")
    meta = _write_metadata(tmp_path / "meta.json")
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    with patch("scripts.publish_kaggle.Path.home") as home:
        home.return_value = tmp_path / "no-kaggle-home"
        with pytest.raises(RuntimeError, match="Missing Kaggle credentials"):
            publish(data_dir=data, metadata=meta, dry_run=False)


def test_main_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.csv").write_text("1", encoding="utf-8")
    meta = _write_metadata(tmp_path / "meta.json")
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "--data-dir",
                str(data),
                "--metadata",
                str(meta),
                "--dry-run",
            ]
        )
        == 0
    )


def test_main_error(tmp_path: Path) -> None:
    assert main(["--data-dir", str(tmp_path / "missing"), "--dry-run"]) == 1


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.handle == "benjaminpo/finance-dataset"
    assert args.data_dir == "data"
