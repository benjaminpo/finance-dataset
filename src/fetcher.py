"""Core Yahoo Finance data fetching and CSV persistence logic."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
import yfinance as yf

logger = logging.getLogger(__name__)

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

# Intraday intervals: Yahoo keeps a rolling window only. We store dated day
# snapshot CSVs so history accumulates across runs.
# 1m ≈ 7 days; other intraday ≤ 60 days.
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


def _csv_path_snapshot(
    data_dir: Path, asset_class: str, interval: str, ticker: str, day: str
) -> Path:
    """
    Path for a dated intraday snapshot.

    Example: data/crypto/5m/BTC-USD_2026-07-10.csv
    """
    return (
        data_dir / asset_class / interval / f"{_safe_ticker_filename(ticker)}_{day}.csv"
    )


def _csv_path_1m(data_dir: Path, asset_class: str, ticker: str, day: str) -> Path:
    """Path for a dated 1-minute snapshot."""
    return _csv_path_snapshot(data_dir, asset_class, "1m", ticker, day)


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


def save_intraday_snapshots(
    df: pd.DataFrame,
    data_dir: Path,
    asset_class: str,
    ticker: str,
    interval: str = "1m",
) -> int:
    """
    Split intraday bars by calendar day and write dated snapshot CSVs.

    Re-running the pipeline overwrites recent day files with the latest
    Yahoo window, so corrections land without unbounded single-file growth.
    Returns the total number of rows written across all day files.
    """
    if df.empty:
        return 0

    rows_written = 0
    # Group by UTC calendar date.
    for day, day_df in df.groupby(df.index.strftime("%Y-%m-%d")):
        path = _csv_path_snapshot(data_dir, asset_class, interval, ticker, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        day_df = day_df.sort_index()
        day_df = day_df[~day_df.index.duplicated(keep="last")]
        _write_ohlcv_csv(day_df, path)
        rows_written += len(day_df)

    return rows_written


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
    """Fetch the rolling intraday window and write dated snapshots."""
    period = SNAPSHOT_PERIODS.get(interval)
    if period is None:
        return False, f"Unsupported snapshot interval '{interval}'"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if skip_existing:
        today_path = _csv_path_snapshot(data_dir, asset_class, interval, ticker, today)
        if today_path.exists():
            return True, f"skipped (exists) → {today_path.relative_to(data_dir.parent)}"

    try:
        df = fetch_history(ticker, interval, period=period)
        if df.empty:
            return False, f"No {interval} data (illiquid, halted, or unsupported)"

        n = save_intraday_snapshots(df, data_dir, asset_class, ticker, interval=interval)
        return True, f"{n} row(s) across day snapshots (through {today})"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def update_ticker_1m(
    ticker: str,
    asset_class: str,
    data_dir: Path,
    *,
    skip_existing: bool = False,
) -> tuple[bool, str]:
    """Fetch the rolling 7-day 1-minute window and write dated snapshots."""
    return update_ticker_snapshot(
        ticker, asset_class, "1m", data_dir, skip_existing=skip_existing
    )


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
) -> str:
    """Execute a single ticker/interval job; return summary status key."""
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
        return "skipped"

    with _print_lock:
        print(f"{label} {'Success' if ok else 'FAILED'} — {msg}")

    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    return "success" if ok else "failed"


def run_pipeline(
    config_path: Path,
    data_dir: Path,
    intervals: Optional[list[str]] = None,
    sleep_seconds: float = REQUEST_DELAY_SECONDS,
    workers: int = DEFAULT_WORKERS,
    skip_existing: bool = False,
) -> dict[str, int]:
    """
    Run the full fetch pipeline for every ticker in the config.

    Returns a summary dict with success / failure counts.

    ``workers`` > 1 fetches tickers concurrently (much faster for large universes).
    ``skip_existing`` resumes a long first backfill by skipping tickers that
    already have a cumulative CSV (or today's intraday snapshot).

    Identical Yahoo requests (same ticker/interval/window) are cached in-process,
    so a symbol listed under more than one asset class only hits Yahoo once.
    """
    clear_fetch_cache()
    intervals = intervals or list(DEFAULT_INTERVALS)
    tickers_by_class = load_tickers(config_path)

    jobs: list[tuple[str, str, str]] = [
        (asset_class, ticker, interval)
        for asset_class, symbols in tickers_by_class.items()
        for ticker in symbols
        for interval in intervals
    ]

    summary = {"success": 0, "failed": 0, "skipped": 0}
    total = len(jobs)
    workers = max(1, int(workers))
    n_tickers = sum(len(v) for v in tickers_by_class.values())

    logger.info(
        "Starting pipeline: %d ticker(s) × %s interval(s) = %d job(s), "
        "workers=%d, skip_existing=%s",
        n_tickers,
        intervals,
        total,
        workers,
        skip_existing,
    )

    if workers == 1:
        for idx, (asset_class, ticker, interval) in enumerate(jobs, start=1):
            status = _run_one_job(
                done=idx,
                total=total,
                ticker=ticker,
                asset_class=asset_class,
                interval=interval,
                data_dir=data_dir,
                sleep_seconds=sleep_seconds,
                skip_existing=skip_existing,
            )
            summary[status] = summary.get(status, 0) + 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
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
                )
                for idx, (asset_class, ticker, interval) in enumerate(jobs, start=1)
            ]
            for fut in as_completed(futures):
                try:
                    status = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Worker crashed: %s", exc)
                    status = "failed"
                    with _print_lock:
                        print(f"Worker FAILED — {exc}")
                summary[status] = summary.get(status, 0) + 1

    logger.info(
        "Pipeline finished: %d success, %d failed, %d skipped",
        summary["success"],
        summary["failed"],
        summary["skipped"],
    )
    return summary