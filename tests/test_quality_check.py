"""Unit tests for the pre-publish OHLCV quality check."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.quality_check import check_ohlcv_tree, sample_indices

HEADER = "Datetime,Open,High,Low,Close,Volume"


def _bar(
    ts: str = "2024-01-02T00:00:00+0000",
    open_: str = "10",
    high: str = "11",
    low: str = "9",
    close: str = "10.5",
    volume: str = "100",
) -> str:
    return f"{ts},{open_},{high},{low},{close},{volume}"


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_sample_indices_covers_ends() -> None:
    assert sample_indices(1, 200) == {0}
    assert sample_indices(5, 2) == {0, 4}
    assert sample_indices(5, 1) == {0}
    with pytest.raises(ValueError, match="sample size"):
        sample_indices(5, 0)


def test_valid_bars_pass(tmp_path: Path) -> None:
    path = tmp_path / "stocks_us" / "1d" / "AAPL.csv"
    _write(
        path,
        HEADER
        + "\n"
        + _bar()
        + "\n"
        + "\n"
        + _bar("2024-01-03T00:00:00+0000", volume="")
        + "\n"
        + _bar(
            "2024-01-04T00:00:00+00:00",
            open_="-40",
            high="-37",
            low="-41",
            close="-38",
            volume="nan",
        )
        + "\n"
        + _bar(
            "2024-01-05T00:00:00+0000",
            high="10.0000000000",
            close="10.0000000001",
        )
        + "\n",
    )
    _write(
        tmp_path / "stocks_us" / "1d" / "MSFT.csv",
        "Datetime,Open,High,Low,Close,Volume,Volume\n" + _bar() + ",\n",
    )
    checked, fully = check_ohlcv_tree(tmp_path)
    assert (checked, fully) == (2, 2)


def test_header_whitespace_and_bom(tmp_path: Path) -> None:
    path = tmp_path / "rates" / "1d" / "TNX.csv"
    _write(
        path,
        "\ufeffDatetime,Open, High,Low,Close,Volume\n" + _bar() + "\n",
    )
    assert check_ohlcv_tree(tmp_path) == (1, 1)


def test_rejects_bad_files(tmp_path: Path) -> None:
    cases = {
        "empty.csv": "",
        "header.csv": HEADER + "\n",
        "columns.csv": "Datetime,Open\n" + _bar() + "\n",
        "text.csv": "hello\n",
        "inverted.csv": HEADER + "\n" + _bar(high="9", low="11") + "\n",
        "high_open.csv": HEADER + "\n" + _bar(open_="12", high="11", low="9") + "\n",
        "high_close.csv": HEADER + "\n" + _bar(close="12", high="11", low="9") + "\n",
        "low_open.csv": HEADER + "\n" + _bar(open_="8", high="11", low="9") + "\n",
        "low_close.csv": HEADER + "\n" + _bar(close="8", high="11", low="9") + "\n",
        "volume.csv": HEADER + "\n" + _bar(volume="-1") + "\n",
        "number.csv": HEADER + "\n" + _bar(open_="nope") + "\n",
        "stamp.csv": HEADER + "\n" + _bar(ts="yesterday") + "\n",
        "blank_ts.csv": HEADER + "\n" + _bar(ts="") + "\n",
        "short.csv": HEADER + "\n2024-01-02T00:00:00+0000\n",
        "blank_px.csv": HEADER + "\n" + _bar(close="") + "\n",
        "dup.csv": HEADER + "\n" + _bar() + "\n" + _bar() + "\n",
        "order.csv": (
            HEADER
            + "\n"
            + _bar("2024-01-03T00:00:00+0000")
            + "\n"
            + _bar("2024-01-02T00:00:00+0000")
            + "\n"
        ),
    }
    for name, body in cases.items():
        root = tmp_path / name
        _write(root / "stocks_us" / "1d" / "AAPL.csv", body)
        with pytest.raises(RuntimeError, match="quality check failed"):
            check_ohlcv_tree(root)


def test_rejects_non_csv_and_binary(tmp_path: Path) -> None:
    _write(tmp_path / "notes.txt", "hello\n")
    with pytest.raises(RuntimeError, match="expected a .csv file"):
        check_ohlcv_tree(tmp_path)

    path = tmp_path / "bad.csv"
    path.write_bytes(b"\xff\xfe not utf-8")
    (tmp_path / "notes.txt").unlink()
    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        check_ohlcv_tree(tmp_path)


def test_ignores_gitkeep_and_other_intervals(tmp_path: Path) -> None:
    _write(tmp_path / "stocks_us" / "1d" / "AAPL.csv", HEADER + "\n" + _bar() + "\n")
    (tmp_path / ".gitkeep").write_text("nope", encoding="utf-8")
    _write(
        tmp_path / "stocks_us" / "1m" / "AAPL.csv",
        HEADER + "\n" + _bar(high="1", low="9") + "\n",
    )
    checked, fully = check_ohlcv_tree(tmp_path, intervals=("1d",))
    assert (checked, fully) == (1, 1)


def test_header_check_skips_later_rows_outside_sample(tmp_path: Path) -> None:
    """Files outside the full scan only need a valid header and first bar."""
    _write(tmp_path / "a" / "1d" / "AAA.csv", HEADER + "\n" + _bar() + "\n")
    _write(
        tmp_path / "a" / "1d" / "BBB.csv",
        HEADER + "\n" + _bar() + "\n" + _bar(high="1", low="9") + "\n",
    )
    _write(tmp_path / "a" / "1d" / "CCC.csv", HEADER + "\n" + _bar() + "\n")
    checked, fully = check_ohlcv_tree(tmp_path, sample_size=2)
    assert checked == 3
    assert fully == 2


def test_full_sample_catches_later_row(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "1d" / "AAA.csv", HEADER + "\n" + _bar() + "\n")
    _write(
        tmp_path / "a" / "1d" / "CCC.csv",
        HEADER + "\n" + _bar() + "\n" + _bar(high="1", low="9") + "\n",
    )
    with pytest.raises(RuntimeError, match="High < Low"):
        check_ohlcv_tree(tmp_path, sample_size=2)


def test_stops_after_max_failures(tmp_path: Path) -> None:
    for name in ("AAA", "BBB", "CCC"):
        _write(tmp_path / "a" / "1d" / f"{name}.csv", "nope\n")
    with pytest.raises(RuntimeError, match=r"at least 2 file"):
        check_ohlcv_tree(tmp_path, max_failures=2)


def test_header_scan_empty_and_header_only(tmp_path: Path) -> None:
    good = HEADER + "\n" + _bar() + "\n"
    _write(tmp_path / "empty" / "a" / "1d" / "AAA.csv", good)
    _write(tmp_path / "empty" / "a" / "1d" / "ZZZ.csv", "")
    with pytest.raises(RuntimeError, match="empty file"):
        check_ohlcv_tree(tmp_path / "empty", sample_size=1)

    _write(tmp_path / "header" / "a" / "1d" / "AAA.csv", good)
    _write(tmp_path / "header" / "a" / "1d" / "ZZZ.csv", HEADER + "\n")
    with pytest.raises(RuntimeError, match="no data rows"):
        check_ohlcv_tree(tmp_path / "header", sample_size=1)


def test_progress_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("scripts.quality_check._PROGRESS_EVERY", 1)
    body = HEADER + "\n" + _bar() + "\n"
    _write(tmp_path / "a" / "1d" / "AAA.csv", body)
    _write(tmp_path / "a" / "1d" / "BBB.csv", body)
    check_ohlcv_tree(tmp_path)
    assert "Quality check: 1/2" in capsys.readouterr().out


def test_missing_tree(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No data files"):
        check_ohlcv_tree(tmp_path / "missing")
