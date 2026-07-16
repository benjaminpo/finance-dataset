"""Unit tests for scripts.run_intraday_shards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts.run_intraday_shards import load_shards, main, merge_summaries, run_shards


def _write_shards(path: Path) -> None:
    path.write_text(
        yaml.dump(
            {
                "shards": [
                    {
                        "id": "a",
                        "asset_classes": ["stocks_us"],
                        "shard_index": 0,
                        "shard_count": 2,
                    },
                    {
                        "id": "b",
                        "asset_classes": ["crypto"],
                        "shard_index": 1,
                        "shard_count": 2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_load_shards(tmp_path: Path) -> None:
    shards_path = tmp_path / "shards.yaml"
    _write_shards(shards_path)
    shards = load_shards(shards_path)
    assert [s["id"] for s in shards] == ["a", "b"]


def test_load_shards_rejects_empty(tmp_path: Path) -> None:
    path = tmp_path / "shards.yaml"
    path.write_text("shards: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No shards"):
        load_shards(path)


def test_merge_summaries() -> None:
    merged = merge_summaries(
        [
            {
                "success": 2,
                "failed": 1,
                "skipped": 0,
                "by_interval": {"1m": {"success": 2, "failed": 1, "skipped": 0}},
                "by_asset_class": {"stocks_us": {"success": 2, "failed": 1, "skipped": 0}},
                "failures": [{"ticker": "X"}],
            },
            {
                "success": 3,
                "failed": 0,
                "skipped": 1,
                "by_interval": {"5m": {"success": 3, "failed": 0, "skipped": 1}},
                "by_asset_class": {"crypto": {"success": 3, "failed": 0, "skipped": 1}},
                "failures": [],
            },
        ]
    )
    assert merged["success"] == 5
    assert merged["failed"] == 1
    assert merged["skipped"] == 1
    assert merged["by_interval"]["1m"]["failed"] == 1
    assert merged["by_interval"]["5m"]["success"] == 3
    assert len(merged["failures"]) == 1


def test_run_shards_calls_pipeline_per_shard(tmp_path: Path) -> None:
    shards_path = tmp_path / "shards.yaml"
    _write_shards(shards_path)
    config_path = tmp_path / "tickers.yaml"
    config_path.write_text("asset_classes: {}\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    summaries = [
        {"success": 1, "failed": 0, "skipped": 0, "failure_rate": 0.0},
        {"success": 2, "failed": 0, "skipped": 0, "failure_rate": 0.0},
    ]

    with (
        patch("scripts.run_intraday_shards.refresh_listings", return_value={"checked": 0, "updated": 0, "failed": 0}),
        patch("scripts.run_intraday_shards.run_pipeline", side_effect=summaries) as pipeline,
    ):
        merged = run_shards(
            shards_path=shards_path,
            config_path=config_path,
            data_dir=data_dir,
            intervals=("1m",),
            summary_path=tmp_path / "summary.json",
        )

    assert pipeline.call_count == 2
    assert merged["success"] == 3
    assert (tmp_path / "summary.json").is_file()


def test_main_exit_all_failed(tmp_path: Path) -> None:
    shards_path = tmp_path / "shards.yaml"
    _write_shards(shards_path)
    config_path = tmp_path / "tickers.yaml"
    config_path.write_text("asset_classes: {}\n", encoding="utf-8")

    with patch(
        "scripts.run_intraday_shards.run_shards",
        return_value={"success": 0, "failed": 2, "skipped": 0, "failure_rate": 1.0},
    ):
        assert (
            main(
                [
                    "--shards",
                    str(shards_path),
                    "--config",
                    str(config_path),
                    "--data-dir",
                    str(tmp_path / "data"),
                ]
            )
            == 2
        )
