"""Unit tests for scripts.split_kaggle_datasets."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.split_kaggle_datasets import main, split


def test_split_publishes_both_slices(tmp_path: Path) -> None:
    data = tmp_path / "data"
    calls: list[tuple] = []

    def fake_pull(**kwargs):
        calls.append(("pull", kwargs["handle"]))
        (data / "crypto" / "1d").mkdir(parents=True)
        (data / "crypto" / "1m").mkdir(parents=True)
        (data / "crypto" / "1d" / "BTC.csv").write_text("d", encoding="utf-8")
        (data / "crypto" / "1m" / "BTC.csv").write_text("m", encoding="utf-8")
        return 2

    def fake_publish(**kwargs):
        calls.append(("publish", kwargs["handle"], kwargs.get("include_intervals")))
        return "notes"

    with (
        patch("scripts.split_kaggle_datasets.pull", side_effect=fake_pull),
        patch("scripts.split_kaggle_datasets.publish", side_effect=fake_publish),
    ):
        split(source_handle="benjaminpo/finance-dataset", data_dir=data, dry_run=True)

    assert calls[0] == ("pull", "benjaminpo/finance-dataset")
    assert calls[1][0] == "publish"
    assert calls[1][1] == "benjaminpo/finance-dataset-intraday"
    assert "1m" in calls[1][2]
    assert calls[2][1] == "benjaminpo/finance-dataset"
    assert "1d" in calls[2][2]


def test_split_only_daily_skips_intraday_upload(tmp_path: Path) -> None:
    data = tmp_path / "data"
    calls: list[str] = []

    def fake_pull(**kwargs):
        (data / "crypto" / "1d").mkdir(parents=True)
        (data / "crypto" / "1d" / "BTC.csv").write_text("d", encoding="utf-8")
        return 1

    def fake_publish(**kwargs):
        calls.append(kwargs["handle"])
        return "notes"

    with (
        patch("scripts.split_kaggle_datasets.pull", side_effect=fake_pull),
        patch("scripts.split_kaggle_datasets.publish", side_effect=fake_publish),
        patch("scripts.split_kaggle_datasets.wait_until_ready") as wait,
    ):
        split(data_dir=data, dry_run=False, only="daily")

    assert calls == ["benjaminpo/finance-dataset"]
    wait.assert_called_once()


def test_split_requires_pulled_files(tmp_path: Path) -> None:
    with patch("scripts.split_kaggle_datasets.pull", return_value=0):
        with pytest.raises(RuntimeError, match="No files pulled"):
            split(data_dir=tmp_path / "data", dry_run=True)


def test_main_error() -> None:
    with patch("scripts.split_kaggle_datasets.split", side_effect=RuntimeError("boom")):
        assert main(["--dry-run"]) == 1
