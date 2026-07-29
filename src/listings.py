"""Refresh remote listing CSVs used to build ticker universes."""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

USER_AGENT = "finance-dataset-pipeline/0.1 (+https://github.com/)"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_listing_entry(entry: Any) -> dict[str, str | None]:
    """Accept a plain path string or a ``{path, url, source}`` mapping."""
    if isinstance(entry, str):
        return {"path": entry, "url": None, "source": None}
    if isinstance(entry, dict):
        path = entry.get("path")
        if not path:
            raise ValueError(f"Listing entry missing 'path': {entry!r}")
        url = entry.get("url")
        source = entry.get("source")
        return {
            "path": str(path),
            "url": str(url) if url else None,
            "source": str(source) if source else None,
        }
    raise ValueError(f"Invalid listing entry: {entry!r}")


def download_bytes(url: str, timeout: float = 60.0) -> bytes:
    """Fetch remote content as bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _write_if_changed(local_path: Path, content: bytes) -> bool:
    """Write *content* to *local_path* if missing or different. Return True if written."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists() and _sha256(local_path.read_bytes()) == _sha256(content):
        print(f"  Unchanged — {local_path.name}", flush=True)
        return False
    action = "Updated" if local_path.exists() else "Created"
    local_path.write_bytes(content)
    print(f"  {action} — {local_path.name}", flush=True)
    return True


def build_krx_listing_csv() -> bytes:
    """
    Build a Symbol/Name/Market CSV for KOSPI + KOSDAQ via FinanceDataReader.

    Yahoo Finance uses ``.KS`` (KOSPI) and ``.KQ`` (KOSDAQ) suffixes.
    """
    import pandas as pd
    import FinanceDataReader as fdr

    frames = []
    for market, suffix in (("KOSPI", ".KS"), ("KOSDAQ", ".KQ")):
        df = fdr.StockListing(market)
        if df is None or df.empty:
            raise RuntimeError(f"Empty KRX listing for market={market}")
        frames.append(
            pd.DataFrame(
                {
                    "Symbol": df["Code"].astype(str).str.zfill(6) + suffix,
                    "Name": df["Name"].astype(str),
                    "Market": market,
                }
            )
        )

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["Symbol"]).sort_values("Symbol")
    return combined.to_csv(index=False).encode("utf-8")


def build_tse_listing_csv() -> bytes:
    """
    Build a Symbol/Name/Industry CSV for Tokyo Stock Exchange via FinanceDataReader.

    Yahoo Finance uses the ``.T`` suffix (e.g. ``7203.T`` for Toyota).
    """
    import pandas as pd
    import FinanceDataReader as fdr

    df = fdr.StockListing("TSE")
    if df is None or df.empty:
        raise RuntimeError("Empty TSE listing from FinanceDataReader")

    code_col = "Symbol" if "Symbol" in df.columns else "Code"
    name_col = "Name" if "Name" in df.columns else df.columns[1]

    out = pd.DataFrame(
        {
            "Symbol": df[code_col].astype(str).str.strip() + ".T",
            "Name": df[name_col].astype(str),
        }
    )
    if "Industry" in df.columns:
        out["Industry"] = df["Industry"].astype(str)

    out = out.drop_duplicates(subset=["Symbol"]).sort_values("Symbol")
    return out.to_csv(index=False).encode("utf-8")


# TWSE listed-company directory (OpenAPI JSON array).
TWSE_LISTED_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"


def build_twse_listing_csv(url: str = TWSE_LISTED_URL) -> bytes:
    """
    Build a Symbol/Name/Industry CSV for TWSE listed companies.

    Downloads the TWSE OpenAPI company directory (JSON) and maps each code to
    Yahoo Finance form with a ``.TW`` suffix (e.g. ``2330.TW`` for TSMC).
    """
    import json

    import pandas as pd

    remote = download_bytes(url)
    try:
        rows = json.loads(remote.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid TWSE listing JSON from {url}: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Empty TWSE listing from {url}")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("公司代號") or row.get("Code") or "").strip()
        if not code or not code.isdigit():
            continue
        symbol = f"{code}.TW"
        if symbol in seen:
            continue
        seen.add(symbol)
        name = str(row.get("公司簡稱") or row.get("公司名稱") or row.get("Name") or "").strip()
        industry = str(row.get("產業別") or row.get("Industry") or "").strip()
        records.append({"Symbol": symbol, "Name": name, "Industry": industry})

    if not records:
        raise RuntimeError(f"Empty TWSE listing after parse from {url}")

    combined = pd.DataFrame(records).sort_values("Symbol")
    return combined.to_csv(index=False).encode("utf-8")


# Major European equity indices covered by pytickersymbols.
EUROPE_INDICES = (
    "EURO STOXX 50",
    "DAX",
    "MDAX",
    "SDAX",
    "TecDAX",
    "CAC_40",
    "CAC Mid 60",
    "AEX",
    "BEL 20",
    "IBEX 35",
    "FTSE 100",
    "Switzerland 20",
    "OMX Helsinki 25",
    "OMX Stockholm 30",
)

# Preferred Yahoo exchange suffixes for European primary listings.
EUROPE_SUFFIXES = (
    ".DE",
    ".PA",
    ".AS",
    ".BR",
    ".MI",
    ".MC",
    ".SW",
    ".ST",
    ".HE",
    ".CO",
    ".OL",
    ".LS",
    ".VI",
    ".IR",
    ".L",
    ".F",
)


def build_europe_listing_csv() -> bytes:
    """
    Build a Symbol/Name/Index CSV for major European index constituents.

    Uses pytickersymbols Yahoo tickers, preferring local-exchange suffixes and
    skipping LSE international order-book codes (numeric-leading symbols).
    """
    import re

    import pandas as pd
    from pytickersymbols import PyTickerSymbols

    stock_data = PyTickerSymbols()

    def score(symbol: str) -> tuple:
        upper = symbol.upper()
        rank = next(
            (i for i, sfx in enumerate(EUROPE_SUFFIXES) if upper.endswith(sfx)),
            100,
        )
        return (rank, len(symbol), symbol)

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for index_name in EUROPE_INDICES:
        for stock in stock_data.get_stocks_by_index(index_name):
            name = str(stock.get("name") or stock.get("symbol") or "")
            yahoo_syms = [
                s.get("yahoo")
                for s in (stock.get("symbols") or [])
                if s.get("yahoo")
            ]
            candidates = [
                y
                for y in yahoo_syms
                if any(y.upper().endswith(sfx) for sfx in EUROPE_SUFFIXES)
                and not re.match(r"^[0-9]", y)
            ]
            if not candidates:
                continue
            chosen = sorted(candidates, key=score)[0]
            if chosen in seen:
                continue
            seen.add(chosen)
            rows.append({"Symbol": chosen, "Name": name, "Index": index_name})

    if not rows:
        raise RuntimeError("Empty European listing from pytickersymbols")

    combined = pd.DataFrame(rows).sort_values("Symbol")
    return combined.to_csv(index=False).encode("utf-8")


# Daily-updated IWM (Russell 2000 ETF) plain ticker list.
RUSSELL2000_TICKERS_URL = (
    "https://raw.githubusercontent.com/major/index-etfs/main/tickers/iwm.txt"
)

# Cash / collateral rows that appear in ETF holdings but are not equities.
_PLAIN_TICKER_SKIP = frozenset({"CASH", "USD", "XTSLA", "P5N994"})


def build_plain_ticker_listing_csv(url: str) -> bytes:
    """
    Build a Symbol CSV from a plain ticker list (one symbol per line).

    Used for ETF/index holdings mirrors such as major/index-etfs
    (IWM / MDY / SPSM). Non-equity rows (cash / collateral) are dropped;
    TradingView-style ``EXCHANGE:TICKER`` lines are normalized to the bare ticker.
    """
    import re

    import pandas as pd

    remote = download_bytes(url)
    text = remote.decode("utf-8")
    ticker_re = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

    symbols: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        raw = line.strip().upper()
        if not raw or raw.startswith("#"):
            continue
        # TradingView-style EXCHANGE:TICKER → bare ticker.
        if ":" in raw:
            raw = raw.rsplit(":", 1)[-1]
        if raw in _PLAIN_TICKER_SKIP or not ticker_re.match(raw):
            continue
        if raw in seen:
            continue
        seen.add(raw)
        symbols.append(raw)

    if not symbols:
        raise RuntimeError(f"Empty plain-ticker listing from {url}")

    combined = pd.DataFrame({"Symbol": symbols}).sort_values("Symbol")
    return combined.to_csv(index=False).encode("utf-8")


def build_russell2000_listing_csv(
    url: str = RUSSELL2000_TICKERS_URL,
) -> bytes:
    """Compatibility wrapper around :func:`build_plain_ticker_listing_csv`."""
    return build_plain_ticker_listing_csv(url=url)


def refresh_listings(config_path: Path) -> dict[str, int]:
    """
    Refresh listing CSVs declared in config.

    Supported entry fields:
      - ``url``: download remote CSV and update when content changed
      - ``source: krx``: rebuild KOSPI/KOSDAQ list via FinanceDataReader
      - ``source: tse``: rebuild Tokyo Stock Exchange list via FinanceDataReader
      - ``source: europe``: rebuild major European index constituents via pytickersymbols
      - ``source: twse``: rebuild TWSE listed-company list from OpenAPI JSON
        (optional ``url`` overrides the default directory endpoint)
      - ``source: plain_tickers`` (or ``russell2000``): rebuild Symbol CSV from a
        plain ticker list; ``url`` required for ``plain_tickers``, optional for
        ``russell2000`` (defaults to IWM holdings)

    Returns ``{"checked": n, "updated": n, "failed": n}``.
    """
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    listings = raw.get("listings") or {}
    config_dir = config_path.parent
    summary = {"checked": 0, "updated": 0, "failed": 0}

    for _asset_class, entries in listings.items():
        if not entries:
            continue
        for entry in entries:
            meta = normalize_listing_entry(entry)
            rel = Path(str(meta["path"]))
            local_path = rel if rel.is_absolute() else config_dir / rel
            source = meta["source"]
            url = meta["url"]

            if source == "krx":
                summary["checked"] += 1
                print("Checking listing update: KRX (KOSPI+KOSDAQ via FinanceDataReader)", flush=True)
                try:
                    remote = build_krx_listing_csv()
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED to build KRX listing — {exc} (keeping local copy)", flush=True)
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if source == "tse":
                summary["checked"] += 1
                print("Checking listing update: TSE (Tokyo via FinanceDataReader)", flush=True)
                try:
                    remote = build_tse_listing_csv()
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED to build TSE listing — {exc} (keeping local copy)", flush=True)
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if source == "europe":
                summary["checked"] += 1
                print(
                    "Checking listing update: Europe (STOXX/DAX/CAC/FTSE/… via pytickersymbols)",
                    flush=True,
                )
                try:
                    remote = build_europe_listing_csv()
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED to build Europe listing — {exc} (keeping local copy)", flush=True)
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if source == "twse":
                summary["checked"] += 1
                listing_url = url or TWSE_LISTED_URL
                print(f"Checking listing update: {listing_url}", flush=True)
                try:
                    remote = build_twse_listing_csv(url=listing_url)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  FAILED to build TWSE listing — {exc} (keeping local copy)",
                        flush=True,
                    )
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if source in ("plain_tickers", "russell2000"):
                summary["checked"] += 1
                listing_url = url or (RUSSELL2000_TICKERS_URL if source == "russell2000" else None)
                if not listing_url:
                    print(
                        f"  FAILED — source {source!r} requires url for {local_path.name}",
                        flush=True,
                    )
                    summary["failed"] += 1
                    continue
                print(f"Checking listing update: {listing_url}", flush=True)
                try:
                    remote = build_plain_ticker_listing_csv(url=listing_url)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  FAILED to build plain-ticker listing — {exc} (keeping local copy)",
                        flush=True,
                    )
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if not url:
                continue

            summary["checked"] += 1
            print(f"Checking listing update: {url}", flush=True)
            try:
                remote = download_bytes(url)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"  FAILED to download — {exc} (keeping local copy)", flush=True)
                summary["failed"] += 1
                continue

            if not remote.strip():
                print("  FAILED — remote file is empty (keeping local copy)", flush=True)
                summary["failed"] += 1
                continue

            if _write_if_changed(local_path, remote):
                summary["updated"] += 1

    logger.info(
        "Listings refresh: checked=%d updated=%d failed=%d",
        summary["checked"],
        summary["updated"],
        summary["failed"],
    )
    return summary
