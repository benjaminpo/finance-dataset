"""Unit tests for src.main CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.main import main, parse_args


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.intervals == ["1d", "1m"]
    assert args.workers == 8
    assert args.skip_existing is False
    assert args.listings_only is False


def test_parse_args_custom() -> None:
    args = parse_args(
        [
            "--intervals",
            "1d",
            "--workers",
            "4",
            "--skip-existing",
            "--skip-listings-refresh",
            "-v",
        ]
    )
    assert args.intervals == ["1d"]
    assert args.workers == 4
    assert args.skip_existing is True
    assert args.skip_listings_refresh is True
    assert args.verbose is True


def test_main_missing_config(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    assert main(["--config", str(missing), "--skip-listings-refresh"]) == 1


def test_main_listings_only_success(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("crypto:\n  - BTC-USD\n", encoding="utf-8")
    with patch(
        "src.main.refresh_listings",
        return_value={"checked": 1, "updated": 1, "failed": 0},
    ) as mock_ref:
        code = main(["--config", str(config), "--listings-only"])
    assert code == 0
    mock_ref.assert_called_once()


def test_main_listings_only_all_failed(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("crypto:\n  - BTC-USD\n", encoding="utf-8")
    with patch(
        "src.main.refresh_listings",
        return_value={"checked": 2, "updated": 0, "failed": 2},
    ):
        assert main(["--config", str(config), "--listings-only"]) == 1


def test_main_runs_pipeline(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("crypto:\n  - BTC-USD\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    with (
        patch(
            "src.main.refresh_listings",
            return_value={"checked": 0, "updated": 0, "failed": 0},
        ),
        patch(
            "src.main.run_pipeline",
            return_value={"success": 2, "failed": 0, "skipped": 0},
        ) as mock_pipe,
    ):
        code = main(
            [
                "--config",
                str(config),
                "--data-dir",
                str(data_dir),
                "--intervals",
                "1d",
                "--sleep",
                "0",
                "--workers",
                "1",
            ]
        )
    assert code == 0
    assert data_dir.is_dir()
    mock_pipe.assert_called_once()


def test_main_all_fetch_failed(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("crypto:\n  - BTC-USD\n", encoding="utf-8")
    with (
        patch(
            "src.main.refresh_listings",
            return_value={"checked": 0, "updated": 0, "failed": 0},
        ),
        patch(
            "src.main.run_pipeline",
            return_value={"success": 0, "failed": 3, "skipped": 0},
        ),
    ):
        assert main(
            [
                "--config",
                str(config),
                "--data-dir",
                str(tmp_path / "data"),
                "--skip-listings-refresh",
            ]
        ) == 2


def test_main_skip_listings_refresh(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("crypto:\n  - BTC-USD\n", encoding="utf-8")
    with (
        patch("src.main.refresh_listings") as mock_ref,
        patch(
            "src.main.run_pipeline",
            return_value={"success": 1, "failed": 0, "skipped": 0},
        ),
    ):
        code = main(
            [
                "--config",
                str(config),
                "--data-dir",
                str(tmp_path / "data"),
                "--skip-listings-refresh",
            ]
        )
    assert code == 0
    mock_ref.assert_not_called()
