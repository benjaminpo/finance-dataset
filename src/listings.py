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


def download_bytes(
    url: str,
    timeout: float = 60.0,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Fetch remote content as bytes."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, headers=req_headers)
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


# TMX company directory (JSON). ``^*`` matches every symbol.
TSX_DIRECTORY_URL = "https://www.tsx.com/json/company-directory/search/tsx/%5E*"
TSXV_DIRECTORY_URL = "https://www.tsx.com/json/company-directory/search/tsxv/%5E*"

# Alternate lines that are not a separate Yahoo equity (USD quote, ETF series,
# preferreds, debentures, warrants, notes).
_CA_SKIP_EXACT = frozenset({"U", "F", "T"})
_CA_SKIP_PREFIXES = ("PR", "DB", "WT", "NT")

# ASX listed-company directory (CSV). Public endpoint used by asx.com.au.
ASX_DIRECTORY_URL = (
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"
)

# NSE cash-market equity master. EQ is the normal series; BE is trade-for-trade.
# BZ names are long-suspended and are dropped.
NSE_EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_EQUITY_SERIES = frozenset({"EQ", "BE"})
# nsearchives ignores the pipeline UA and stalls; a browser UA succeeds.
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}


def _canadian_equity_symbol(raw: str, yahoo_suffix: str) -> str | None:
    """Map a TMX instrument to Yahoo form, or None when it is not an equity line.

    Dots become hyphens before the exchange suffix (``REI.UN`` → ``REI-UN.TO``,
    ``CTC.A`` → ``CTC-A.TO``). Trust units (``.UN``), single-letter share
    classes, and TSXV capital-pool ``.P`` lines are kept.
    """
    import re

    symbol = raw.strip().upper()
    if not symbol or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)*", symbol):
        return None
    if "." in symbol:
        rest = symbol.split(".", 1)[1]
        if rest in _CA_SKIP_EXACT or rest.startswith(_CA_SKIP_PREFIXES):
            return None
        if rest != "UN" and not (len(rest) == 1 and rest.isalpha()):
            return None
    return symbol.replace(".", "-") + yahoo_suffix


def build_tsx_listing_csv(
    tsx_url: str = TSX_DIRECTORY_URL,
    tsxv_url: str = TSXV_DIRECTORY_URL,
) -> bytes:
    """
    Build a Symbol/Name/Market CSV for TSX and TSX Venture.

    Downloads the TMX company-directory JSON for each board. Yahoo suffixes are
    ``.TO`` (TSX) and ``.V`` (TSXV).
    """
    import json

    import pandas as pd

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for market, suffix, url in (
        ("TSX", ".TO", tsx_url),
        ("TSXV", ".V", tsxv_url),
    ):
        remote = download_bytes(url)
        try:
            payload = json.loads(remote.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid TMX listing JSON from {url}: {exc}") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"Empty TMX listing from {url}")

        for row in results:
            if not isinstance(row, dict):
                continue
            instruments = row.get("instruments") or [
                {"symbol": row.get("symbol"), "name": row.get("name")}
            ]
            issuer = str(row.get("name") or "").strip()
            for instrument in instruments:
                if not isinstance(instrument, dict):
                    continue
                yahoo = _canadian_equity_symbol(str(instrument.get("symbol") or ""), suffix)
                if not yahoo or yahoo in seen:
                    continue
                seen.add(yahoo)
                name = issuer or str(instrument.get("name") or "").strip()
                records.append({"Symbol": yahoo, "Name": name, "Market": market})

    if not records:
        raise RuntimeError("Empty Canadian listing after parse")

    combined = pd.DataFrame(records).sort_values("Symbol")
    return combined.to_csv(index=False).encode("utf-8")


def build_asx_listing_csv(url: str = ASX_DIRECTORY_URL) -> bytes:
    """
    Build a Symbol/Name/Industry CSV for ASX listed companies.

    Downloads the ASX company directory and maps each code to Yahoo Finance
    form with an ``.AX`` suffix (e.g. ``BHP.AX``).
    """
    import io

    import pandas as pd

    remote = download_bytes(url)
    try:
        df = pd.read_csv(io.BytesIO(remote), keep_default_na=False)
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid ASX listing CSV from {url}: {exc}") from exc
    df.columns = [str(col).strip() for col in df.columns]
    if "ASX code" not in df.columns:
        raise RuntimeError(f"ASX listing from {url} has no 'ASX code' column")

    code = df["ASX code"].astype(str).str.strip().str.upper()
    keep = code.str.fullmatch(r"[A-Z0-9]{1,6}")
    df = df.loc[keep].copy()
    code = code.loc[keep]
    if df.empty:
        raise RuntimeError(f"Empty ASX listing from {url}")

    if "Company name" in df.columns:
        names = df["Company name"].astype(str).to_numpy()
    else:
        names = code.to_numpy()
    out = pd.DataFrame({"Symbol": (code + ".AX").to_numpy(), "Name": names})
    if "GICs industry group" in df.columns:
        out["Industry"] = df["GICs industry group"].astype(str).to_numpy()

    out = out.drop_duplicates(subset=["Symbol"]).sort_values("Symbol")
    return out.to_csv(index=False).encode("utf-8")


def build_nse_listing_csv(url: str = NSE_EQUITY_URL) -> bytes:
    """
    Build a Symbol/Name/Series CSV for NSE equities.

    Downloads the NSE equity master list. Yahoo Finance uses the ``.NS`` suffix
    (e.g. ``RELIANCE.NS``, ``M&M.NS``). Only ``EQ`` and ``BE`` series are kept.
    """
    import io

    import pandas as pd

    remote = download_bytes(url, headers=_NSE_HEADERS)
    try:
        df = pd.read_csv(io.BytesIO(remote), keep_default_na=False)
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid NSE listing CSV from {url}: {exc}") from exc
    df.columns = [str(col).strip() for col in df.columns]
    if "SYMBOL" not in df.columns or "SERIES" not in df.columns:
        raise RuntimeError(f"NSE listing from {url} is missing SYMBOL/SERIES")

    df = df.copy()
    df["SERIES"] = df["SERIES"].astype(str).str.strip().str.upper()
    df = df[df["SERIES"].isin(NSE_EQUITY_SERIES)]
    symbols = df["SYMBOL"].astype(str).str.strip().str.upper()
    keep = symbols.str.fullmatch(r"[A-Z0-9][A-Z0-9&-]{0,30}")
    df = df.loc[keep].copy()
    symbols = symbols.loc[keep]
    if df.empty:
        raise RuntimeError(f"Empty NSE listing from {url}")

    if "NAME OF COMPANY" in df.columns:
        names = df["NAME OF COMPANY"].astype(str).str.strip().to_numpy()
    else:
        names = symbols.to_numpy()
    out = pd.DataFrame(
        {
            "Symbol": (symbols + ".NS").to_numpy(),
            "Name": names,
            "Series": df["SERIES"].astype(str).to_numpy(),
        }
    )
    out = out.drop_duplicates(subset=["Symbol"]).sort_values("Symbol")
    return out.to_csv(index=False).encode("utf-8")


def build_cn_listing_csv() -> bytes:
    """
    Build a Symbol/Name/Market CSV for Shanghai and Shenzhen via FinanceDataReader.

    Yahoo Finance uses ``.SS`` (SSE) and ``.SZ`` (SZSE). Numeric codes are
    zero-padded to 6 digits (e.g. ``601288.SS``, ``300750.SZ``).
    """
    import pandas as pd
    import FinanceDataReader as fdr

    frames = []
    for market, suffix in (("SSE", ".SS"), ("SZSE", ".SZ")):
        df = fdr.StockListing(market)
        if df is None or df.empty:
            raise RuntimeError(f"Empty China listing for market={market}")
        code_col = "Symbol" if "Symbol" in df.columns else "Code"
        name_col = "Name" if "Name" in df.columns else df.columns[1]
        codes = (
            df[code_col]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
        )
        keep = codes.str.fullmatch(r"\d{1,6}")
        if not bool(keep.any()):
            raise RuntimeError(f"Empty China listing for market={market}")
        kept = df.loc[keep]
        out = pd.DataFrame(
            {
                "Symbol": (codes.loc[keep].str.zfill(6) + suffix).to_numpy(),
                "Name": kept[name_col].astype(str).to_numpy(),
                "Market": market,
            }
        )
        if "Industry" in df.columns:
            out["Industry"] = kept["Industry"].astype(str).to_numpy()
        frames.append(out)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["Symbol"]).sort_values("Symbol")
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
      - ``source: tsx``: rebuild TSX + TSXV equities from the TMX company directory
      - ``source: asx``: rebuild ASX listed companies from the company-directory CSV
        (optional ``url`` overrides the default directory endpoint)
      - ``source: nse``: rebuild NSE equities from the official equity master CSV
        (optional ``url`` overrides the default directory endpoint)
      - ``source: china``: rebuild Shanghai + Shenzhen lists via FinanceDataReader
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

            if source == "tsx":
                summary["checked"] += 1
                print(
                    "Checking listing update: Canada (TSX+TSXV via TMX company directory)",
                    flush=True,
                )
                try:
                    remote = build_tsx_listing_csv()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  FAILED to build TSX listing — {exc} (keeping local copy)",
                        flush=True,
                    )
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if source == "asx":
                summary["checked"] += 1
                listing_url = url or ASX_DIRECTORY_URL
                print(f"Checking listing update: {listing_url}", flush=True)
                try:
                    remote = build_asx_listing_csv(url=listing_url)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  FAILED to build ASX listing — {exc} (keeping local copy)",
                        flush=True,
                    )
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if source == "nse":
                summary["checked"] += 1
                listing_url = url or NSE_EQUITY_URL
                print(f"Checking listing update: {listing_url}", flush=True)
                try:
                    remote = build_nse_listing_csv(url=listing_url)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  FAILED to build NSE listing — {exc} (keeping local copy)",
                        flush=True,
                    )
                    summary["failed"] += 1
                    continue
                if _write_if_changed(local_path, remote):
                    summary["updated"] += 1
                continue

            if source == "china":
                summary["checked"] += 1
                print(
                    "Checking listing update: China (SSE+SZSE via FinanceDataReader)",
                    flush=True,
                )
                try:
                    remote = build_cn_listing_csv()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  FAILED to build China listing — {exc} (keeping local copy)",
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
