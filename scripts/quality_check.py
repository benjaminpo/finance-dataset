"""OHLCV checks run before a Kaggle publish."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from scripts.kaggle_util import iter_data_files

# Every uploaded bar file must carry these columns. Volume may be blank
# (indices often have no share volume); the price columns may not.
REQUIRED_COLUMNS = ("Datetime", "Open", "High", "Low", "Close", "Volume")
PRICE_COLUMNS = ("Open", "High", "Low", "Close")

# Row-by-row scans of the whole tree are too expensive once intraday history
# accumulates. Every file still gets a header and first-bar check; this many
# files, spread evenly across the tree, are scanned in full.
DEFAULT_QUALITY_SAMPLE = 200
DEFAULT_MAX_FAILURES = 20
_PROGRESS_EVERY = 20000
_MISSING_MARKERS = {"", "nan", "none", "null"}
# Ignore sub-tick gaps (Yahoo float noise). Real inversions are far larger.
_PRICE_TOLERANCE = Decimal("0.000001")


def sample_indices(n_files: int, sample_size: int) -> set[int]:
    """Evenly spaced indexes, always including the first and last file."""
    if sample_size < 1:
        raise ValueError("quality sample size must be >= 1")
    if n_files <= sample_size:
        return set(range(n_files))
    if sample_size == 1:
        return {0}
    return {
        round(i * (n_files - 1) / (sample_size - 1)) for i in range(sample_size)
    }


def check_ohlcv_tree(
    data_dir: Path,
    *,
    intervals: tuple[str, ...] | list[str] | None = None,
    sample_size: int = DEFAULT_QUALITY_SAMPLE,
    max_failures: int = DEFAULT_MAX_FAILURES,
) -> tuple[int, int]:
    """
    Validate CSVs that would be uploaded.

    Returns ``(files_checked, files_fully_scanned)``.
    Raises ``RuntimeError`` when a file fails the OHLCV rules.
    """
    root = Path(data_dir)
    files = sorted(
        iter_data_files(root, intervals=intervals),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not files:
        raise FileNotFoundError(f"No data files to quality-check under {root}")

    chosen = sample_indices(len(files), sample_size)
    failures: list[tuple[str, str]] = []
    for index, path in enumerate(files):
        if index and index % _PROGRESS_EVERY == 0:
            print(
                f"Quality check: {index}/{len(files)} file(s)...",
                flush=True,
            )
        problem = _validate_file(path, full=index in chosen)
        if problem is None:
            continue
        failures.append((path.relative_to(root).as_posix(), problem))
        if len(failures) >= max_failures:
            break

    if failures:
        truncated = len(failures) >= max_failures and index < len(files) - 1
        shown = len(failures)
        headline = (
            f"Refusing to publish: OHLCV quality check failed "
            f"(at least {shown} file(s)):"
            if truncated
            else f"Refusing to publish: OHLCV quality check failed ({shown} file(s)):"
        )
        details = "\n".join(f"  {rel}: {message}" for rel, message in failures)
        raise RuntimeError(f"{headline}\n{details}")

    fully_scanned = len(chosen)
    if fully_scanned == len(files):
        print(f"Quality check passed: {len(files)} file(s) fully scanned", flush=True)
    else:
        print(
            "Quality check passed: "
            f"{len(files)} file(s) "
            f"({fully_scanned} fully scanned, "
            f"{len(files) - fully_scanned} header-checked)",
            flush=True,
        )
    return len(files), fully_scanned


def _validate_file(path: Path, *, full: bool) -> str | None:
    try:
        if path.suffix != ".csv":
            raise ValueError("expected a .csv file")
        if full:
            _scan_all_rows(path)
        else:
            _scan_header_and_first_row(path)
    except UnicodeError:
        return "not valid UTF-8"
    except ValueError as exc:
        return str(exc)
    except csv.Error as exc:
        return f"invalid CSV: {exc}"
    return None


def _scan_header_and_first_row(path: Path) -> None:
    with path.open("r", encoding="utf-8", newline="") as handle:
        header_line = handle.readline()
        if not header_line:
            raise ValueError("empty file")
        first_line = handle.readline()
    rows = list(csv.reader(io.StringIO(header_line + first_line)))
    columns = _column_index(rows[0])
    data_rows = [row for row in rows[1:] if _row_has_cells(row)]
    if not data_rows:
        raise ValueError("no data rows")
    _check_bar(data_rows[0], columns, line_no=2)


def _scan_all_rows(path: Path) -> None:
    previous: datetime | None = None
    saw_bar = False
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            raise ValueError("empty file")
        columns = _column_index(header)
        for line_no, row in enumerate(reader, start=2):
            if not _row_has_cells(row):
                continue
            timestamp = _check_bar(row, columns, line_no=line_no)
            if previous is not None:
                if timestamp == previous:
                    raise ValueError(f"duplicate timestamp at line {line_no}")
                if timestamp < previous:
                    raise ValueError(f"timestamps not sorted at line {line_no}")
            previous = timestamp
            saw_bar = True
    if not saw_bar:
        raise ValueError("no data rows")


def _column_index(header: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, name in enumerate(header):
        key = name.strip().lstrip("\ufeff")
        if key and key not in index:
            index[key] = position
    missing = [column for column in REQUIRED_COLUMNS if column not in index]
    if missing:
        raise ValueError("missing columns: " + ", ".join(missing))
    return index


def _row_has_cells(row: list[str]) -> bool:
    return any(cell.strip() for cell in row)


def _cell(row: list[str], columns: dict[str, int], name: str) -> str:
    position = columns[name]
    if position >= len(row):
        return ""
    return row[position].strip()


def _parse_timestamp(text: str, line_no: int) -> datetime:
    if not text:
        raise ValueError(f"missing Datetime at line {line_no}")
    # pandas date_format %z writes +0000; fromisoformat wants +00:00.
    normalized = text
    if len(normalized) >= 5 and normalized[-5] in "+-" and ":" not in normalized[-5:]:
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid Datetime at line {line_no}") from exc


def _parse_decimal(text: str, column: str, line_no: int) -> Decimal | None:
    if text.lower() in _MISSING_MARKERS:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{column} is not a number at line {line_no}") from exc


def _meaningfully_below(left: Decimal, right: Decimal) -> bool:
    """True when *left* is below *right* by more than float noise."""
    span = max(abs(left), abs(right), Decimal("1"))
    return (right - left) > span * _PRICE_TOLERANCE


def _check_bar(row: list[str], columns: dict[str, int], *, line_no: int) -> datetime:
    timestamp = _parse_timestamp(_cell(row, columns, "Datetime"), line_no)
    prices: dict[str, Decimal] = {}
    for name in PRICE_COLUMNS:
        value = _parse_decimal(_cell(row, columns, name), name, line_no)
        if value is None:
            raise ValueError(f"missing {name} at line {line_no}")
        prices[name] = value

    high, low = prices["High"], prices["Low"]
    open_, close = prices["Open"], prices["Close"]
    if _meaningfully_below(high, low):
        raise ValueError(f"High < Low at line {line_no}")
    if _meaningfully_below(high, open_):
        raise ValueError(f"High < Open at line {line_no}")
    if _meaningfully_below(high, close):
        raise ValueError(f"High < Close at line {line_no}")
    if _meaningfully_below(open_, low):
        raise ValueError(f"Low > Open at line {line_no}")
    if _meaningfully_below(close, low):
        raise ValueError(f"Low > Close at line {line_no}")

    volume = _parse_decimal(_cell(row, columns, "Volume"), "Volume", line_no)
    if volume is not None and volume < 0:
        raise ValueError(f"negative Volume at line {line_no}")
    return timestamp
