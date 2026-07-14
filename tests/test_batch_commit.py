"""Unit tests for scripts.batch_commit helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.batch_commit import (
    batch_commit,
    batch_label,
    expand_paths,
    filter_ignored,
    main,
    parse_args,
    push_with_retry,
)


def test_changed_files_parses_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.batch_commit import changed_files

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "listings").mkdir(parents=True)
    (tmp_path / "data" / "a.csv").write_text("x", encoding="utf-8")
    (tmp_path / "config" / "listings" / "b.csv").write_text("y", encoding="utf-8")

    def fake_git_output(cmd):
        root = cmd[-1]
        if root == "data":
            return "?? data/\n"
        if root == "config/listings":
            return " M config/listings/b.csv\n"
        return ""

    with (
        patch("scripts.batch_commit.git_output", side_effect=fake_git_output),
        patch("scripts.batch_commit.filter_ignored", side_effect=lambda files: files),
    ):
        files = changed_files(("config/listings", "data"))
    assert "config/listings/b.csv" in files
    assert any(f.endswith("a.csv") for f in files)
    # Listings sorted before data/
    assert files[0].startswith("config/")


def test_changed_files_handles_rename_and_missing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.batch_commit import changed_files

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    def fake_git_output(cmd):
        return "R  data/old.csv -> data/new.csv\n"

    with (
        patch("scripts.batch_commit.git_output", side_effect=fake_git_output),
        patch(
            "scripts.batch_commit.expand_paths",
            side_effect=lambda paths: paths,
        ),
        patch("scripts.batch_commit.filter_ignored", side_effect=lambda files: files),
    ):
        files = changed_files(("data", "missing"))
    assert files == ["data/new.csv"]


def test_run_and_git_output() -> None:
    from scripts.batch_commit import git_output, run

    with patch("scripts.batch_commit.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        assert run(["echo", "hi"]).stdout == "ok\n"
        assert git_output(["git", "status"]) == "ok\n"


def test_expand_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "data" / "crypto" / "1d"
    d.mkdir(parents=True)
    (d / "BTC-USD.csv").write_text("x", encoding="utf-8")
    (tmp_path / "lone.txt").write_text("y", encoding="utf-8")
    files = expand_paths(["data", "lone.txt", "missing-dir"])
    assert "data/crypto/1d/BTC-USD.csv" in files or any(
        f.endswith("BTC-USD.csv") for f in files
    )
    assert "lone.txt" in files


def test_filter_ignored() -> None:
    with patch("scripts.batch_commit.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="a/.DS_Store\n", returncode=0)
        out = filter_ignored(["a/.DS_Store", "a/keep.csv"])
    assert out == ["a/keep.csv"]
    assert filter_ignored([]) == []


def test_batch_label() -> None:
    assert batch_label(["config/listings/nasdaq.csv"]) == "listings"
    assert batch_label(["data/crypto/1d/BTC-USD.csv", "data/crypto/1d/ETH-USD.csv"]) == (
        "crypto/1d"
    )
    assert (
        batch_label(
            ["data/crypto/1d/BTC-USD.csv", "data/stocks_us/1m/AAPL_2024-01-01.csv"]
        )
        == "mixed"
    )


def test_push_with_retry_success() -> None:
    ok = MagicMock(returncode=0, stdout="", stderr="")
    with patch("scripts.batch_commit.run", return_value=ok) as mock_run:
        push_with_retry("main", attempts=2)
    assert mock_run.call_count == 1


def test_push_with_retry_then_rebase() -> None:
    fail = MagicMock(returncode=1, stdout="", stderr="rejected")
    ok = MagicMock(returncode=0, stdout="", stderr="")
    with patch("scripts.batch_commit.run", side_effect=[fail, ok, ok]) as mock_run:
        # fail push, rebase pull, success push
        push_with_retry("main", attempts=2)
    assert mock_run.call_count == 3


def test_push_with_retry_exhausted() -> None:
    fail = MagicMock(returncode=1, stdout="", stderr="nope")
    with patch("scripts.batch_commit.run", return_value=fail):
        with pytest.raises(subprocess.CalledProcessError):
            push_with_retry("main", attempts=2)


def test_batch_commit_no_changes() -> None:
    with patch("scripts.batch_commit.changed_files", return_value=[]):
        assert batch_commit(push=False) == 0


def test_batch_commit_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    files = [f"data/f{i}.csv" for i in range(5)]
    for f in files:
        Path(f).parent.mkdir(parents=True, exist_ok=True)
        Path(f).write_text("x", encoding="utf-8")

    def fake_run(cmd, check=True):
        if cmd[:3] == ["git", "diff", "--cached"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        if cmd[:2] == ["git", "commit"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "add"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="main\n", stderr="")

    with (
        patch("scripts.batch_commit.changed_files", return_value=files),
        patch("scripts.batch_commit.git_output", return_value="main\n"),
        patch("scripts.batch_commit.run", side_effect=fake_run),
        patch("scripts.batch_commit.push_with_retry") as mock_push,
    ):
        n = batch_commit(batch_size=2, push=True)
    assert n == 3
    assert mock_push.call_count == 3


def test_batch_commit_skips_empty_staged() -> None:
    with (
        patch("scripts.batch_commit.changed_files", return_value=["data/a.csv"]),
        patch("scripts.batch_commit.git_output", return_value="main\n"),
        patch("scripts.batch_commit.run") as mock_run,
    ):
        # git add ok, then git diff --cached --quiet returns 0 (nothing staged)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # add
            MagicMock(returncode=0, stdout="", stderr=""),  # diff quiet
        ]
        assert batch_commit(push=False) == 0


def test_parse_args_and_main_success() -> None:
    defaults = parse_args([])
    assert defaults.roots == ["config/listings"]
    args = parse_args(["--batch-size", "10", "--no-push", "--roots", "data"])
    assert args.batch_size == 10
    assert args.no_push is True
    assert args.roots == ["data"]
    with patch("scripts.batch_commit.batch_commit", return_value=2) as mock_bc:
        assert main(["--no-push", "--batch-size", "50"]) == 0
        mock_bc.assert_called_once()


def test_main_git_failure() -> None:
    with patch(
        "scripts.batch_commit.batch_commit",
        side_effect=subprocess.CalledProcessError(1, "git", stderr="boom"),
    ):
        assert main(["--no-push"]) == 1
