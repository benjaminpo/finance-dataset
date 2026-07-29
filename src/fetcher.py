"""Core Yahoo Finance data fetching and CSV persistence logic."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, TypedDict

import pandas as pd
import yaml
import yfinance as yf

logger = logging.getLogger(__name__)


class JobResult(TypedDict):
    """Outcome of a single ticker/interval fetch job."""

    status: str  # success | failed | skipped
    ticker: str
    asset_class: str
    interval: str
    message: str

# Columns we persist to CSV (OHLCV + Dividends/Stock Splits when present).
OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]

# Pause after each ticker request (per worker) to reduce rate-limit risk.
REQUEST_DELAY_SECONDS = 0.25

# Default parallel Yahoo requests. Keep modest to avoid hard rate limits.
DEFAULT_WORKERS = 8

# Yahoo Finance intervals this pipeline supports (all valid yfinance intervals).
ALL_INTERVALS: tuple[str, ...] = (
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "90m",
    "1h",
    "1d",
    "5d",
    "1wk",
    "1mo",
    "3mo",
)

# Default fetch set when --intervals is omitted.
DEFAULT_INTERVALS: list[str] = list(ALL_INTERVALS)

# CI daily workflow: full universe, lower volume.
DAILY_INTERVALS: tuple[str, ...] = ("1d", "1wk")

# CI intraday workflow: smaller universe (see config/tickers_intraday.yaml).
INTRADAY_INTERVALS: tuple[str, ...] = (
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "90m",
    "1h",
)

# Intraday intervals: Yahoo keeps a rolling window only.
# 1m ≈ 7 days; other intraday ≤ 60 days.
# Stored as one consolidated CSV per ticker (same layout as daily), pruned to
# the Yahoo retention window on each write so Kaggle file counts stay bounded.
SNAPSHOT_PERIODS: dict[str, str] = {
    "1m": "7d",
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "60d",
    "90m": "60d",
    "1h": "60d",
}

# Legacy dated snapshot filenames: TICKER_YYYY-MM-DD.csv
_DATED_SNAPSHOT_RE = re.compile(
    r"^(?P<stem>.+)_(?P<day>\d{4}-\d{2}-\d{2})\.csv$"
)


def _period_to_days(period: str) -> int:
    """Parse yfinance-style period strings like ``7d`` / ``60d`` to day counts."""
    text = period.strip().lower()
    if text.endswith("d") and text[:-1].isdigit():
        return int(text[:-1])
    raise ValueError(f"Unsupported period string for retention: {period!r}")


SNAPSHOT_RETENTION_DAYS: dict[str, int] = {
    interval: _period_to_days(period) for interval, period in SNAPSHOT_PERIODS.items()
}

# Day-or-longer bars: one cumulative CSV per ticker with incremental merges.
CUMULATIVE_INTERVALS: frozenset[str] = frozenset({"1d", "5d", "1wk", "1mo", "3mo"})

# Backward-compat alias used by older call sites / docs.
INTRADAY_PERIOD = SNAPSHOT_PERIODS["1m"]

# Cumulative CSVs shorter than this are treated as truncated (e.g. period=5d
# fallback during a rate-limited bulk run) and re-fetched with full history.
MIN_TRUSTED_DAILY_ROWS = 20

# Fixed decimal places for OHLCV price columns in CSV output (Volume stays int).
CSV_FLOAT_FORMAT = "%.10f"

# Some thin instruments (new SPACs, warrants) reject period=max; try shorter windows.
# Put 5d early — some names only allow 1d/5d on Yahoo.
DAILY_PERIOD_FALLBACKS = (
    "max",
    "5d",
    "1mo",
    "3mo",
    "6mo",
    "1y",
    "2y",
    "5y",
    "10y",
)

# Process-wide cache so the same Yahoo request is not repeated in one run
# (e.g. a symbol listed under more than one asset class).
# In-flight Futures coalesce concurrent duplicate keys (singleflight).
_fetch_cache: dict[tuple, pd.DataFrame] = {}
_fetch_inflight: dict[tuple, Future] = {}
_fetch_cache_lock = threading.Lock()
_print_lock = threading.Lock()

# NASDAQ Security Name tokens with very limited Yahoo history.
_SKIP_SECURITY_NAME_RE = re.compile(
    r"\b(?:Warrant|Warrants|Right|Rights|Unit|Units)\b",
    re.IGNORECASE,
)


def _to_yahoo_symbol(symbol: str) -> str:
    """
    Map listing symbols to Yahoo Finance form.

    US share classes use a hyphen (BRK.B → BRK-B). Exchange suffixes such as
    ``.KS``, ``.KQ``, ``.L``, and ``.HK`` must be preserved.
    """
    symbol = symbol.strip()
    upper = symbol.upper()
    exchange_suffixes = (
        ".KS", ".KQ", ".L", ".HK", ".T", ".SS", ".SZ",
        ".DE", ".PA", ".AS", ".BR", ".MI", ".MC", ".SW",
        ".ST", ".HE", ".CO", ".OL", ".LS", ".VI", ".IR", ".F",
    )
    if any(upper.endswith(sfx) for sfx in exchange_suffixes):
        return symbol
    if upper.endswith(("=X", "=F")) or "-" in symbol:
        return symbol
    # Dual-class shares in S&P-style listings: BRK.B / BF.B → BRK-B / BF-B
    if "." in symbol:
        base, _, klass = symbol.rpartition(".")
        if base and len(klass) == 1 and klass.isalpha():
            return f"{base}-{klass}"
    return symbol


def load_symbols_from_csv(csv_path: Path) -> list[str]:
    """
    Read a Symbol column from a listing CSV.

    Drops NASDAQ test issues when a ``Test Issue`` column is present.
    Drops warrants / rights / units when a ``Security Name`` column is present
    (Yahoo often only allows 1d/5d history for those).
    Converts share-class dots to hyphens for Yahoo Finance compatibility.
    """
    df = pd.read_csv(csv_path)
    if "Symbol" not in df.columns:
        raise ValueError(f"No 'Symbol' column in {csv_path}")

    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].fillna("N").astype(str).str.upper() != "Y"]

    if "Security Name" in df.columns:
        names = df["Security Name"].fillna("").astype(str)
        df = df[~names.str.contains(_SKIP_SECURITY_NAME_RE, regex=True)]

    symbols: list[str] = []
    seen: set[str] = set()
    for raw in df["Symbol"].dropna().astype(str):
        sym = _to_yahoo_symbol(raw)
        if not sym or sym.lower() == "nan" or sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
    return symbols


def load_tickers(config_path: Path) -> dict[str, list[str]]:
    """
    Load asset-class → ticker list mapping from a YAML config file.

    Optional top-level ``listings`` maps an asset class to CSV entries.
    Each entry may be a path string or ``{path, url}``. Symbols from those
    files are unioned with any inline list under the same asset class.
    """
    from src.listings import normalize_listing_entry

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    config_dir = config_path.parent
    listings = raw.pop("listings", None) or {}

    tickers: dict[str, list[str]] = {}
    for asset_class, symbols in raw.items():
        if symbols is None:
            continue
        # Coerce to str so YAML integers (e.g. unquoted 0700) stay zero-padded.
        tickers[str(asset_class)] = [str(s) for s in symbols]

    for asset_class, entries in listings.items():
        if not entries:
            continue
        merged: list[str] = list(tickers.get(asset_class, []))
        seen = set(merged)
        csv_count = 0
        for entry in entries:
            meta = normalize_listing_entry(entry)
            path = Path(str(meta["path"]))
            if not path.is_absolute():
                path = config_dir / path
            if not path.exists():
                logger.warning("Listing file missing, skipping: %s", path)
                continue
            csv_count += 1
            for sym in load_symbols_from_csv(path):
                if sym not in seen:
                    seen.add(sym)
                    merged.append(sym)
        tickers[str(asset_class)] = merged
        logger.info(
            "Loaded %d symbol(s) for %s from %d listing file(s)",
            len(merged),
            asset_class,
            csv_count,
        )

    return tickers


def select_tickers(
    tickers_by_class: dict[str, list[str]],
    *,
    asset_classes: Optional[list[str]] = None,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict[str, list[str]]:
    """
    Filter *tickers_by_class* to selected asset classes and/or a stable shard.

    Sharding is by sorted symbol index within each kept class:
    ``index % shard_count == shard_index``. Empty classes are dropped.
    """
    if shard_count < 1:
        raise ValueError(f"shard_count must be >= 1, got {shard_count}")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError(
            f"shard_index must be in [0, {shard_count}), got {shard_index}"
        )

    if asset_classes is None:
        selected = dict(tickers_by_class)
    else:
        wanted = {str(name) for name in asset_classes}
        unknown = sorted(wanted - set(tickers_by_class))
        if unknown:
            raise ValueError(
                "Unknown asset class(es): "
                + ", ".join(unknown)
                + ". Known: "
                + ", ".join(sorted(tickers_by_class))
            )
        selected = {
            name: list(symbols)
            for name, symbols in tickers_by_class.items()
            if name in wanted
        }

    if shard_count == 1:
        return {name: symbols for name, symbols in selected.items() if symbols}

    out: dict[str, list[str]] = {}
    for name, symbols in selected.items():
        # Stable order so shard membership does not drift across runs.
        ordered = sorted(set(symbols))
        shard = [
            sym for i, sym in enumerate(ordered) if i % shard_count == shard_index
        ]
        if shard:
            out[name] = shard
    return out


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize a yfinance DataFrame: UTC index named Datetime, sorted OHLCV cols."""
    if df is None or df.empty:
        return pd.DataFrame()

    # Flatten MultiIndex columns that appear when downloading a single ticker
    # via some yfinance versions (e.g. ("Close", "AAPL")).
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "Datetime"

    # Keep known columns that exist; ignore extras.
    keep = [c for c in OHLCV_COLUMNS if c in df.columns]
    # Also retain Dividends / Stock Splits if yfinance included them.
    for extra in ("Dividends", "Stock Splits"):
        if extra in df.columns and extra not in keep:
            keep.append(extra)

    df = df[keep].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def clear_fetch_cache() -> None:
    """Drop the in-process Yahoo response cache (mainly for tests)."""
    with _fetch_cache_lock:
        _fetch_cache.clear()
        _fetch_inflight.clear()


def fetch_history(
    ticker: str,
    interval: str,
    *,
    start: Optional[str] = None,
    period: Optional[str] = None,
) -> pd.DataFrame:
    """
    Download OHLCV history for a single ticker.

    Prefer ``start`` for incremental daily updates; use ``period`` for
    full history or the fixed 7-day intraday window.

    Identical requests within one process are served from an in-memory cache
    so duplicate symbols across asset classes do not hit Yahoo twice.
    Concurrent callers with the same key share one in-flight request
    (singleflight) rather than racing duplicate Yahoo downloads.
    """
    cache_key = (ticker, interval, start, period)
    with _fetch_cache_lock:
        cached = _fetch_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        inflight = _fetch_inflight.get(cache_key)
        if inflight is None:
            inflight = Future()
            _fetch_inflight[cache_key] = inflight
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        return inflight.result().copy()

    try:
        kwargs: dict = {"interval": interval, "auto_adjust": False, "progress": False}
        if start is not None:
            kwargs["start"] = start
        elif period is not None:
            kwargs["period"] = period
        else:
            kwargs["period"] = "max"

        # yfinance logs noisy ERROR lines for empty/invalid downloads; keep ours.
        yf_logger = logging.getLogger("yfinance")
        prev_level = yf_logger.level
        yf_logger.setLevel(logging.CRITICAL)
        try:
            raw = yf.download(ticker, **kwargs)
        finally:
            yf_logger.setLevel(prev_level)

        df = _normalize_frame(raw)
        with _fetch_cache_lock:
            _fetch_cache[cache_key] = df
            _fetch_inflight.pop(cache_key, None)
        inflight.set_result(df)
        return df.copy()
    except Exception as exc:
        with _fetch_cache_lock:
            _fetch_inflight.pop(cache_key, None)
        inflight.set_exception(exc)
        raise


def fetch_daily_history(
    ticker: str,
    *,
    start: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch daily bars, falling back through shorter periods when ``max`` is rejected.

    Thin names (new listings, some SPACs) only allow ``1d``/``5d`` on Yahoo.
    """
    return fetch_cumulative_history(ticker, "1d", start=start)


def fetch_cumulative_history(
    ticker: str,
    interval: str,
    *,
    start: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch day-or-longer bars for *interval*.

    For ``1d``, fall back through shorter periods when ``max`` is rejected.
    Other cumulative intervals use ``period=max`` (or ``start`` when set).
    """
    if interval not in CUMULATIVE_INTERVALS:
        raise ValueError(f"Not a cumulative interval: {interval}")

    if start is not None:
        return fetch_history(ticker, interval, start=start)

    if interval != "1d":
        return fetch_history(ticker, interval, period="max")

    for period in DAILY_PERIOD_FALLBACKS:
        df = fetch_history(ticker, "1d", period=period)
        if not df.empty:
            if period != "max":
                logger.info("%s: daily history via period=%s (max unavailable)", ticker, period)
            return df
    return pd.DataFrame()


def _safe_ticker_filename(ticker: str) -> str:
    """Sanitize ticker for use in filenames."""
    return ticker.replace("^", "").replace("=", "_").replace("/", "_")


def _csv_path_cumulative(
    data_dir: Path, asset_class: str, interval: str, ticker: str
) -> Path:
    """Path for a cumulative CSV, e.g. data/stocks_us/1d/AAPL.csv."""
    return data_dir / asset_class / interval / f"{_safe_ticker_filename(ticker)}.csv"


def _csv_path_1d(data_dir: Path, asset_class: str, ticker: str) -> Path:
    """Path for a cumulative daily CSV, e.g. data/stocks_us/1d/AAPL.csv."""
    return _csv_path_cumulative(data_dir, asset_class, "1d", ticker)


def _csv_path_intraday(
    data_dir: Path, asset_class: str, interval: str, ticker: str
) -> Path:
    """Path for a consolidated intraday CSV, e.g. data/crypto/5m/BTC-USD.csv."""
    return _csv_path_cumulative(data_dir, asset_class, interval, ticker)


def _csv_path_snapshot(
    data_dir: Path, asset_class: str, interval: str, ticker: str, day: str
) -> Path:
    """
    Legacy dated intraday snapshot path (pre-consolidation layout).

    Example: data/crypto/5m/BTC-USD_2026-07-10.csv
    """
    return (
        data_dir / asset_class / interval / f"{_safe_ticker_filename(ticker)}_{day}.csv"
    )


def _csv_path_1m(data_dir: Path, asset_class: str, ticker: str) -> Path:
    """Path for a consolidated 1-minute CSV."""
    return _csv_path_intraday(data_dir, asset_class, "1m", ticker)


def _parse_dated_snapshot_name(name: str) -> tuple[str, str] | None:
    """Return ``(ticker_stem, YYYY-MM-DD)`` for legacy dated filenames, else None."""
    match = _DATED_SNAPSHOT_RE.match(name)
    if match is None:
        return None
    return match.group("stem"), match.group("day")


def _read_ohlcv_csv(path: Path) -> pd.DataFrame:
    """Load an OHLCV CSV; return empty on missing/corrupt files."""
    if not path.is_file():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, index_col="Datetime", parse_dates=True)
        if df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "Datetime"
        return df
    except Exception as exc:  # noqa: BLE001 — corrupt CSV should not abort the run
        logger.warning("Could not read %s (%s); ignoring.", path, exc)
        return pd.DataFrame()


def _retention_cutoff(
    interval: str, *, now: pd.Timestamp | None = None
) -> pd.Timestamp:
    days = SNAPSHOT_RETENTION_DAYS[interval]
    anchor = now if now is not None else pd.Timestamp.now(tz="UTC")
    if anchor.tzinfo is None:
        anchor = anchor.tz_localize("UTC")
    else:
        anchor = anchor.tz_convert("UTC")
    return anchor - pd.Timedelta(days=days)


def _prune_intraday_frame(
    df: pd.DataFrame, interval: str, *, now: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Drop bars older than the Yahoo retention window for *interval*."""
    if df.empty:
        return df
    cutoff = _retention_cutoff(interval, now=now)
    pruned = df[df.index >= cutoff]
    return pruned.sort_index()


def _iter_legacy_dated_paths(
    data_dir: Path, asset_class: str, interval: str, ticker: str
) -> list[Path]:
    """List legacy dated snapshot CSVs for one ticker (if any)."""
    folder = data_dir / asset_class / interval
    if not folder.is_dir():
        return []
    stem = _safe_ticker_filename(ticker)
    prefix = f"{stem}_"
    paths: list[Path] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        parsed = _parse_dated_snapshot_name(path.name)
        if parsed is None:
            continue
        file_stem, _day = parsed
        if file_stem == stem and path.name.startswith(prefix):
            paths.append(path)
    return sorted(paths)


def _merge_ohlcv_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    if not nonempty:
        return pd.DataFrame()
    combined = pd.concat(nonempty)
    combined.index = pd.to_datetime(combined.index, utc=True)
    combined.index.name = "Datetime"
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def _last_timestamp(csv_path: Path) -> Optional[pd.Timestamp]:
    """Return the latest Datetime index value from an existing CSV, or None."""
    if not csv_path.exists():
        return None
    try:
        existing = pd.read_csv(csv_path, index_col="Datetime", parse_dates=True)
        if existing.empty:
            return None
        if len(existing) < MIN_TRUSTED_DAILY_ROWS:
            logger.info(
                "%s has only %d row(s); will refetch full history.",
                csv_path.name,
                len(existing),
            )
            return None
        ts = pd.to_datetime(existing.index, utc=True).max()
        return ts
    except Exception as exc:  # noqa: BLE001 — corrupt CSV should not abort the run
        logger.warning("Could not read %s (%s); will refetch full history.", csv_path, exc)
        return None


def _write_ohlcv_csv(df: pd.DataFrame, csv_path: Path) -> None:
    """Write a normalized OHLCV frame with consistent float formatting."""
    df.to_csv(
        csv_path,
        date_format="%Y-%m-%dT%H:%M:%S%z",
        float_format=CSV_FLOAT_FORMAT,
    )


def save_daily(df: pd.DataFrame, csv_path: Path) -> int:
    """
    Incrementally merge *df* into *csv_path*.

    Returns the number of new/updated rows written.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if csv_path.exists():
        existing = pd.read_csv(csv_path, index_col="Datetime", parse_dates=True)
        existing.index = pd.to_datetime(existing.index, utc=True)
        existing.index.name = "Datetime"
        combined = pd.concat([existing, df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        new_rows = len(combined) - len(existing)
    else:
        combined = df
        new_rows = len(combined)

    _write_ohlcv_csv(combined, csv_path)
    return max(new_rows, 0)


def save_intraday(
    df: pd.DataFrame,
    data_dir: Path,
    asset_class: str,
    ticker: str,
    interval: str = "1m",
    *,
    now: pd.Timestamp | None = None,
) -> int:
    """
    Merge *df* into a consolidated per-ticker intraday CSV and prune retention.

    Also absorbs and deletes any legacy dated day files
    (``TICKER_YYYY-MM-DD.csv``) for the same ticker/interval.
    Returns the number of rows kept after pruning.
    """
    if interval not in SNAPSHOT_RETENTION_DAYS:
        raise ValueError(f"Unsupported intraday interval '{interval}'")

    csv_path = _csv_path_intraday(data_dir, asset_class, interval, ticker)
    legacy_paths = _iter_legacy_dated_paths(data_dir, asset_class, interval, ticker)

    frames: list[pd.DataFrame] = []
    if not df.empty:
        frames.append(df)
    frames.append(_read_ohlcv_csv(csv_path))
    for legacy in legacy_paths:
        frames.append(_read_ohlcv_csv(legacy))

    combined = _prune_intraday_frame(
        _merge_ohlcv_frames(frames), interval, now=now
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if combined.empty:
        csv_path.unlink(missing_ok=True)
        for legacy in legacy_paths:
            legacy.unlink(missing_ok=True)
        return 0

    _write_ohlcv_csv(combined, csv_path)
    for legacy in legacy_paths:
        legacy.unlink(missing_ok=True)
    return len(combined)


def save_intraday_snapshots(
    df: pd.DataFrame,
    data_dir: Path,
    asset_class: str,
    ticker: str,
    interval: str = "1m",
    *,
    now: pd.Timestamp | None = None,
) -> int:
    """Backward-compatible alias for :func:`save_intraday`."""
    return save_intraday(
        df, data_dir, asset_class, ticker, interval=interval, now=now
    )


def consolidate_intraday_layout(
    data_dir: Path,
    *,
    intervals: tuple[str, ...] | list[str] | None = None,
    now: pd.Timestamp | None = None,
) -> dict[str, int]:
    """
    Migrate legacy dated snapshot CSVs into consolidated per-ticker files.

    For each intraday interval directory under *data_dir*:
    - merge ``TICKER_YYYY-MM-DD.csv`` files into ``TICKER.csv``
    - prune bars outside the Yahoo retention window
    - delete the dated files

    Also re-prunes existing consolidated files that have no dated siblings.
    Safe to run repeatedly. Returns counts useful for CI logging.
    """
    root = Path(data_dir)
    allowed = (
        set(intervals)
        if intervals is not None
        else set(SNAPSHOT_RETENTION_DAYS)
    )
    allowed &= set(SNAPSHOT_RETENTION_DAYS)

    dated_files_removed = 0
    tickers_consolidated = 0
    tickers_pruned = 0

    if not root.is_dir():
        return {
            "dated_files_removed": 0,
            "tickers_consolidated": 0,
            "tickers_pruned": 0,
        }

    for asset_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for interval_dir in sorted(p for p in asset_dir.iterdir() if p.is_dir()):
            interval = interval_dir.name
            if interval not in allowed:
                continue

            dated_by_stem: dict[str, list[Path]] = {}
            consolidated_paths: list[Path] = []
            for path in interval_dir.iterdir():
                if not path.is_file() or path.suffix.lower() != ".csv":
                    continue
                parsed = _parse_dated_snapshot_name(path.name)
                if parsed is not None:
                    stem, _day = parsed
                    dated_by_stem.setdefault(stem, []).append(path)
                else:
                    consolidated_paths.append(path)

            for stem, legacy_paths in dated_by_stem.items():
                csv_path = interval_dir / f"{stem}.csv"
                frames = [_read_ohlcv_csv(csv_path)]
                frames.extend(_read_ohlcv_csv(p) for p in legacy_paths)
                combined = _prune_intraday_frame(
                    _merge_ohlcv_frames(frames), interval, now=now
                )
                if combined.empty:
                    csv_path.unlink(missing_ok=True)
                else:
                    _write_ohlcv_csv(combined, csv_path)
                for legacy in legacy_paths:
                    legacy.unlink(missing_ok=True)
                dated_files_removed += len(legacy_paths)
                tickers_consolidated += 1

            # Re-prune consolidated-only files (no dated siblings this pass).
            for csv_path in consolidated_paths:
                if _parse_dated_snapshot_name(csv_path.name) is not None:
                    continue
                if csv_path.stem in dated_by_stem:
                    # Already rewritten above.
                    continue
                existing = _read_ohlcv_csv(csv_path)
                if existing.empty:
                    continue
                pruned = _prune_intraday_frame(existing, interval, now=now)
                if len(pruned) == len(existing):
                    continue
                if pruned.empty:
                    csv_path.unlink(missing_ok=True)
                else:
                    _write_ohlcv_csv(pruned, csv_path)
                tickers_pruned += 1

    return {
        "dated_files_removed": dated_files_removed,
        "tickers_consolidated": tickers_consolidated,
        "tickers_pruned": tickers_pruned,
    }

def update_ticker_cumulative(
    ticker: str,
    asset_class: str,
    interval: str,
    data_dir: Path,
    *,
    skip_existing: bool = False,
) -> tuple[bool, str]:
    """Fetch and incrementally update a cumulative (day+) interval for one ticker."""
    if interval not in CUMULATIVE_INTERVALS:
        return False, f"Unsupported cumulative interval '{interval}'"

    csv_path = _csv_path_cumulative(data_dir, asset_class, interval, ticker)
    last_ts = _last_timestamp(csv_path)

    if skip_existing and last_ts is not None:
        return True, f"skipped (exists) → {csv_path.relative_to(data_dir.parent)}"

    try:
        if last_ts is not None:
            # Start one day before the last bar so the latest candle can be refreshed.
            start = (last_ts - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            df = fetch_cumulative_history(ticker, interval, start=start)
            if df.empty:
                return True, f"0 new/updated row(s) → {csv_path.relative_to(data_dir.parent)}"
        else:
            df = fetch_cumulative_history(ticker, interval)
            if df.empty:
                return False, f"No {interval} data returned"

        n = save_daily(df, csv_path)
        return True, f"{n} new/updated row(s) → {csv_path.relative_to(data_dir.parent)}"
    except Exception as exc:  # noqa: BLE001 — isolate per-ticker failures
        return False, str(exc)


def update_ticker_1d(
    ticker: str,
    asset_class: str,
    data_dir: Path,
    *,
    skip_existing: bool = False,
) -> tuple[bool, str]:
    """Fetch and incrementally update 1-day data for one ticker."""
    return update_ticker_cumulative(
        ticker, asset_class, "1d", data_dir, skip_existing=skip_existing
    )


def update_ticker_snapshot(
    ticker: str,
    asset_class: str,
    interval: str,
    data_dir: Path,
    *,
    skip_existing: bool = False,
) -> tuple[bool, str]:
    """Fetch the rolling intraday window and merge into a consolidated CSV."""
    period = SNAPSHOT_PERIODS.get(interval)
    if period is None:
        return False, f"Unsupported snapshot interval '{interval}'"

    csv_path = _csv_path_intraday(data_dir, asset_class, interval, ticker)
    if skip_existing and csv_path.exists():
        return True, f"skipped (exists) → {csv_path.relative_to(data_dir.parent)}"

    try:
        df = fetch_history(ticker, interval, period=period)
        if df.empty:
            return False, f"No {interval} data (illiquid, halted, or unsupported)"

        n = save_intraday(df, data_dir, asset_class, ticker, interval=interval)
        return True, f"{n} row(s) retained → {csv_path.relative_to(data_dir.parent)}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def update_ticker_1m(
    ticker: str,
    asset_class: str,
    data_dir: Path,
    *,
    skip_existing: bool = False,
) -> tuple[bool, str]:
    """Fetch the rolling 7-day 1-minute window into a consolidated CSV."""
    return update_ticker_snapshot(
        ticker, asset_class, "1m", data_dir, skip_existing=skip_existing
    )


def _job_result(
    status: str,
    *,
    ticker: str,
    asset_class: str,
    interval: str,
    message: str = "",
) -> JobResult:
    return {
        "status": status,
        "ticker": ticker,
        "asset_class": asset_class,
        "interval": interval,
        "message": message,
    }


def _empty_count_bucket() -> dict[str, int]:
    return {"success": 0, "failed": 0, "skipped": 0}


def _new_pipeline_summary() -> dict:
    return {
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "total": 0,
        "attempted": 0,
        "failure_rate": 0.0,
        "by_interval": {},
        "by_asset_class": {},
        "failures": [],
    }


def _record_job_result(summary: dict, result: JobResult) -> None:
    """Accumulate one job outcome into the pipeline summary."""
    status = result["status"]
    if status not in ("success", "failed", "skipped"):
        status = "failed"
        result = {**result, "status": status}

    summary[status] = int(summary.get(status, 0)) + 1
    summary["total"] = int(summary.get("total", 0)) + 1

    interval = result["interval"] or "(unknown)"
    asset_class = result["asset_class"] or "(unknown)"
    by_interval = summary.setdefault("by_interval", {})
    by_asset = summary.setdefault("by_asset_class", {})
    if interval not in by_interval:
        by_interval[interval] = _empty_count_bucket()
    if asset_class not in by_asset:
        by_asset[asset_class] = _empty_count_bucket()
    by_interval[interval][status] = by_interval[interval].get(status, 0) + 1
    by_asset[asset_class][status] = by_asset[asset_class].get(status, 0) + 1

    if status == "failed":
        summary.setdefault("failures", []).append(
            {
                "ticker": result["ticker"],
                "asset_class": result["asset_class"],
                "interval": result["interval"],
                "message": result["message"],
            }
        )


def _finalize_pipeline_summary(summary: dict) -> dict:
    attempted = int(summary.get("success", 0)) + int(summary.get("failed", 0))
    summary["attempted"] = attempted
    summary["failure_rate"] = (
        round(int(summary.get("failed", 0)) / attempted, 6) if attempted else 0.0
    )
    return summary


def _run_one_job(
    *,
    done: int,
    total: int,
    ticker: str,
    asset_class: str,
    interval: str,
    data_dir: Path,
    sleep_seconds: float,
    skip_existing: bool,
) -> JobResult:
    """Execute a single ticker/interval job; return a structured result."""
    label = f"[{done}/{total}] Fetching {ticker} [{interval}]..."

    if interval in CUMULATIVE_INTERVALS:
        ok, msg = update_ticker_cumulative(
            ticker, asset_class, interval, data_dir, skip_existing=skip_existing
        )
    elif interval in SNAPSHOT_PERIODS:
        ok, msg = update_ticker_snapshot(
            ticker, asset_class, interval, data_dir, skip_existing=skip_existing
        )
    else:
        with _print_lock:
            print(f"{label} Skipped (unsupported interval '{interval}')")
        return _job_result(
            "skipped",
            ticker=ticker,
            asset_class=asset_class,
            interval=interval,
            message=f"unsupported interval '{interval}'",
        )

    with _print_lock:
        print(f"{label} {'Success' if ok else 'FAILED'} — {msg}")

    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    return _job_result(
        "success" if ok else "failed",
        ticker=ticker,
        asset_class=asset_class,
        interval=interval,
        message=msg,
    )


def run_pipeline(
    config_path: Path,
    data_dir: Path,
    intervals: Optional[list[str]] = None,
    sleep_seconds: float = REQUEST_DELAY_SECONDS,
    workers: int = DEFAULT_WORKERS,
    skip_existing: bool = False,
    asset_classes: Optional[list[str]] = None,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict:
    """
    Run the full fetch pipeline for every ticker in the config.

    Returns a summary dict with success / failure counts, per-interval and
    per-asset-class breakdowns, ``failure_rate`` (failed / attempted), and a
    ``failures`` list of ``{ticker, asset_class, interval, message}``.

    ``workers`` > 1 fetches tickers concurrently (much faster for large universes).
    ``skip_existing`` resumes a long first backfill by skipping tickers that
    already have a cumulative CSV (or consolidated intraday CSV).

    ``asset_classes`` / ``shard_index`` / ``shard_count`` limit work to a slice
    (used by CI to split large intraday runs across jobs).

    Identical Yahoo requests (same ticker/interval/window) are cached in-process,
    so a symbol listed under more than one asset class only hits Yahoo once.
    """
    clear_fetch_cache()
    intervals = intervals or list(DEFAULT_INTERVALS)
    tickers_by_class = select_tickers(
        load_tickers(config_path),
        asset_classes=asset_classes,
        shard_index=shard_index,
        shard_count=shard_count,
    )

    jobs: list[tuple[str, str, str]] = [
        (asset_class, ticker, interval)
        for asset_class, symbols in tickers_by_class.items()
        for ticker in symbols
        for interval in intervals
    ]

    summary = _new_pipeline_summary()
    total = len(jobs)
    workers = max(1, int(workers))
    n_tickers = sum(len(v) for v in tickers_by_class.values())

    logger.info(
        "Starting pipeline: %d ticker(s) × %s interval(s) = %d job(s), "
        "workers=%d, skip_existing=%s, asset_classes=%s, shard=%d/%d",
        n_tickers,
        intervals,
        total,
        workers,
        skip_existing,
        asset_classes or "all",
        shard_index,
        shard_count,
    )

    if workers == 1:
        for idx, (asset_class, ticker, interval) in enumerate(jobs, start=1):
            result = _run_one_job(
                done=idx,
                total=total,
                ticker=ticker,
                asset_class=asset_class,
                interval=interval,
                data_dir=data_dir,
                sleep_seconds=sleep_seconds,
                skip_existing=skip_existing,
            )
            _record_job_result(summary, result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future, tuple[str, str, str]] = {
                pool.submit(
                    _run_one_job,
                    done=idx,
                    total=total,
                    ticker=ticker,
                    asset_class=asset_class,
                    interval=interval,
                    data_dir=data_dir,
                    sleep_seconds=sleep_seconds,
                    skip_existing=skip_existing,
                ): (asset_class, ticker, interval)
                for idx, (asset_class, ticker, interval) in enumerate(jobs, start=1)
            }
            for fut in as_completed(futures):
                asset_class, ticker, interval = futures[fut]
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Worker crashed: %s", exc)
                    with _print_lock:
                        print(f"Worker FAILED — {ticker} [{interval}]: {exc}")
                    result = _job_result(
                        "failed",
                        ticker=ticker,
                        asset_class=asset_class,
                        interval=interval,
                        message=f"Worker crashed: {exc}",
                    )
                _record_job_result(summary, result)

    _finalize_pipeline_summary(summary)
    logger.info(
        "Pipeline finished: %d success, %d failed, %d skipped "
        "(attempted=%d, failure_rate=%.2f%%)",
        summary["success"],
        summary["failed"],
        summary["skipped"],
        summary["attempted"],
        100.0 * summary["failure_rate"],
    )
    return summary