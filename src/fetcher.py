"""Core Yahoo Finance data fetching and CSV persistence logic."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
import yfinance as yf

logger = logging.getLogger(__name__)

# Columns we persist to CSV (OHLCV + Dividends/Stock Splits when present).
OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]

# Pause between individual ticker requests to reduce rate-limit risk.
REQUEST_DELAY_SECONDS = 1.0

# Yahoo Finance only retains ~7 days of 1-minute bars.
INTRADAY_PERIOD = "7d"


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
    Converts share-class dots to hyphens for Yahoo Finance compatibility.
    """
    df = pd.read_csv(csv_path)
    if "Symbol" not in df.columns:
        raise ValueError(f"No 'Symbol' column in {csv_path}")

    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].fillna("N").astype(str).str.upper() != "Y"]

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
    """
    kwargs: dict = {"interval": interval, "auto_adjust": False, "progress": False}
    if start is not None:
        kwargs["start"] = start
    elif period is not None:
        kwargs["period"] = period
    else:
        kwargs["period"] = "max"

    raw = yf.download(ticker, **kwargs)
    return _normalize_frame(raw)


def _csv_path_1d(data_dir: Path, asset_class: str, ticker: str) -> Path:
    """Path for a cumulative daily CSV, e.g. data/stocks_us/1d/AAPL.csv."""
    safe = ticker.replace("^", "").replace("=", "_").replace("/", "_")
    return data_dir / asset_class / "1d" / f"{safe}.csv"


def _csv_path_1m(data_dir: Path, asset_class: str, ticker: str, day: str) -> Path:
    """
    Path for a dated 1-minute snapshot.

    Example: data/crypto/1m/BTC-USD_2026-07-10.csv
    """
    safe = ticker.replace("^", "").replace("=", "_").replace("/", "_")
    return data_dir / asset_class / "1m" / f"{safe}_{day}.csv"


def _last_timestamp(csv_path: Path) -> Optional[pd.Timestamp]:
    """Return the latest Datetime index value from an existing CSV, or None."""
    if not csv_path.exists():
        return None
    try:
        existing = pd.read_csv(csv_path, index_col="Datetime", parse_dates=True)
        if existing.empty:
            return None
        ts = pd.to_datetime(existing.index, utc=True).max()
        return ts
    except Exception as exc:  # noqa: BLE001 — corrupt CSV should not abort the run
        logger.warning("Could not read %s (%s); will refetch full history.", csv_path, exc)
        return None


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

    combined.to_csv(csv_path, date_format="%Y-%m-%dT%H:%M:%S%z")
    return max(new_rows, 0)


def save_intraday_snapshots(
    df: pd.DataFrame,
    data_dir: Path,
    asset_class: str,
    ticker: str,
) -> int:
    """
    Split 1-minute bars by calendar day and write dated snapshot CSVs.

    Re-running the pipeline overwrites recent day files with the latest
    Yahoo window, so corrections land without unbounded single-file growth.
    Returns the total number of rows written across all day files.
    """
    if df.empty:
        return 0

    rows_written = 0
    # Group by UTC calendar date.
    for day, day_df in df.groupby(df.index.strftime("%Y-%m-%d")):
        path = _csv_path_1m(data_dir, asset_class, ticker, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        day_df = day_df.sort_index()
        day_df = day_df[~day_df.index.duplicated(keep="last")]
        day_df.to_csv(path, date_format="%Y-%m-%dT%H:%M:%S%z")
        rows_written += len(day_df)

    return rows_written


def update_ticker_1d(
    ticker: str,
    asset_class: str,
    data_dir: Path,
) -> tuple[bool, str]:
    """Fetch and incrementally update 1-day data for one ticker."""
    csv_path = _csv_path_1d(data_dir, asset_class, ticker)
    last_ts = _last_timestamp(csv_path)

    try:
        if last_ts is not None:
            # Start one day before the last bar so the latest candle can be refreshed.
            start = (last_ts - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            df = fetch_history(ticker, "1d", start=start)
        else:
            df = fetch_history(ticker, "1d", period="max")

        if df.empty:
            return False, "No data returned"

        n = save_daily(df, csv_path)
        return True, f"{n} new/updated row(s) → {csv_path.relative_to(data_dir.parent)}"
    except Exception as exc:  # noqa: BLE001 — isolate per-ticker failures
        return False, str(exc)


def update_ticker_1m(
    ticker: str,
    asset_class: str,
    data_dir: Path,
) -> tuple[bool, str]:
    """Fetch the rolling 7-day 1-minute window and write dated snapshots."""
    try:
        df = fetch_history(ticker, "1m", period=INTRADAY_PERIOD)
        if df.empty:
            return False, "No data returned"

        n = save_intraday_snapshots(df, data_dir, asset_class, ticker)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return True, f"{n} row(s) across day snapshots (through {today})"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_pipeline(
    config_path: Path,
    data_dir: Path,
    intervals: Optional[list[str]] = None,
    sleep_seconds: float = REQUEST_DELAY_SECONDS,
) -> dict[str, int]:
    """
    Run the full fetch pipeline for every ticker in the config.

    Returns a summary dict with success / failure counts.
    """
    intervals = intervals or ["1d", "1m"]
    tickers_by_class = load_tickers(config_path)

    summary = {"success": 0, "failed": 0, "skipped": 0}
    total = sum(len(v) for v in tickers_by_class.values()) * len(intervals)
    done = 0

    logger.info(
        "Starting pipeline: %d ticker(s) × %s interval(s) = %d job(s)",
        sum(len(v) for v in tickers_by_class.values()),
        intervals,
        total,
    )

    for asset_class, symbols in tickers_by_class.items():
        for ticker in symbols:
            for interval in intervals:
                done += 1
                label = f"[{done}/{total}] Fetching {ticker} [{interval}]..."
                print(label, end=" ", flush=True)

                if interval == "1d":
                    ok, msg = update_ticker_1d(ticker, asset_class, data_dir)
                elif interval == "1m":
                    ok, msg = update_ticker_1m(ticker, asset_class, data_dir)
                else:
                    print(f"Skipped (unsupported interval '{interval}')")
                    summary["skipped"] += 1
                    continue

                if ok:
                    print(f"Success — {msg}")
                    summary["success"] += 1
                else:
                    print(f"FAILED — {msg}")
                    summary["failed"] += 1

                time.sleep(sleep_seconds)

    logger.info(
        "Pipeline finished: %d success, %d failed, %d skipped",
        summary["success"],
        summary["failed"],
        summary["skipped"],
    )
    return summary
