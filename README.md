# Finance Dataset Pipeline

Automated pipeline that pulls historical and intraday market data from [Yahoo Finance](https://finance.yahoo.com/) via [`yfinance`](https://github.com/ranaroussi/yfinance) and stores it as partitioned CSV files in this repository. A GitHub Actions workflow runs daily and commits updates back to `data/`.

## Asset classes

| Config key   | Examples                         | Path prefix          |
|--------------|----------------------------------|----------------------|
| `stocks_us`  | NASDAQ listings ∪ S&P 500        | `data/stocks_us/`    |
| `stocks_kr`  | KOSPI + KOSDAQ (`.KS` / `.KQ`)   | `data/stocks_kr/`    |
| `stocks_jp`  | Tokyo Stock Exchange (`.T`)      | `data/stocks_jp/`    |
| `stocks_eu`  | STOXX/DAX/CAC/FTSE/AEX/IBEX/…    | `data/stocks_eu/`    |
| `stocks_hk`  | 0700.HK, 9988.HK                 | `data/stocks_hk/`    |
| `indices`    | ^GSPC, ^RUT, ^STOXX50E, ^KS11, … | `data/indices/`      |
| `rates`      | ^IRX, ^FVX, ^TNX, ^TYX           | `data/rates/`        |
| `futures`    | CL=F, ES=F                       | `data/futures/`      |
| `crypto`     | BTC-USD, ETH-USD                 | `data/crypto/`       |
| `currencies` | EURUSD=X, GBPUSD=X, KRW=X, …     | `data/currencies/`   |

Edit the lists in [`config/tickers.yaml`](config/tickers.yaml) to add or remove symbols.

**US stocks** are loaded as the union of Symbol columns from:

- [`config/listings/nasdaq-listed-symbols.csv`](config/listings/nasdaq-listed-symbols.csv) ([source](https://raw.githubusercontent.com/datasets/nasdaq-listings/master/data/nasdaq-listed-symbols.csv))
- [`config/listings/sp500-constituents.csv`](config/listings/sp500-constituents.csv) ([source](https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv))

Share classes like `BRK.B` are rewritten to Yahoo form (`BRK-B`). NASDAQ rows with `Test Issue = Y` are skipped.

Each pipeline run (including the scheduled GitHub Action) checks those remote URLs first, compares SHA-256 hashes, and updates the local CSVs when content changed. Listing updates are committed alongside `data/`. Use `--skip-listings-refresh` to skip the check, or `--listings-only` to refresh listings without fetching market data.

**Korean stocks** (`stocks_kr`) are rebuilt each run from KOSPI + KOSDAQ via [FinanceDataReader](https://github.com/FinanceData/FinanceDataReader) into [`config/listings/krx-listed-symbols.csv`](config/listings/krx-listed-symbols.csv), with Yahoo suffixes `.KS` (KOSPI) and `.KQ` (KOSDAQ).

**Japanese stocks** (`stocks_jp`) are rebuilt each run from the Tokyo Stock Exchange via FinanceDataReader into [`config/listings/tse-listed-symbols.csv`](config/listings/tse-listed-symbols.csv), with Yahoo suffix `.T` (e.g. `7203.T`).

**European stocks** (`stocks_eu`) are rebuilt each run from major European indices (EURO STOXX 50, DAX/MDAX/SDAX/TecDAX, CAC 40/Mid 60, AEX, BEL 20, IBEX 35, FTSE 100, Switzerland 20, OMX Helsinki 25, OMX Stockholm 30) via [pytickersymbols](https://github.com/portfolioplus/pytickersymbols) into [`config/listings/europe-listed-symbols.csv`](config/listings/europe-listed-symbols.csv).

## Storage layout

```
data/
├── stocks_us/
│   ├── 1d/
│   │   └── AAPL.csv              # cumulative daily bars (incremental)
│   └── 1m/
│       ├── AAPL_2026-07-09.csv   # dated 1-minute snapshots
│       └── AAPL_2026-07-10.csv
├── crypto/
│   ├── 1d/
│   │   └── BTC-USD.csv
│   └── 1m/
│       └── BTC-USD_2026-07-10.csv
└── ...
```

### Interval strategy

- **`1d`** — Cumulative file per ticker. New bars are appended; duplicate timestamps are dropped (last write wins), so re-runs refresh the latest candle safely.
- **`1m`** — Yahoo only keeps ~7 days of 1-minute history. Each run writes **dated snapshot files** (`TICKER_YYYY-MM-DD.csv`) for every calendar day in the returned window. Recent days are overwritten on subsequent runs; older snapshots remain, so history accumulates without one giant rolling file.

CSV index column is `Datetime` (UTC, ISO-8601). Columns: Open, High, Low, Close, Adj Close, Volume (plus Dividends / Stock Splits when present).

## Repository layout

```
├── .github/workflows/data_fetch.yml
├── config/tickers.yaml
├── data/
├── src/
│   ├── __init__.py
│   ├── fetcher.py      # download + CSV merge logic
│   └── main.py         # CLI entry point
├── requirements.txt
└── README.md
```

## Local setup

Requires **Python 3.10+**.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Run the pipeline

```bash
# Both intervals (default)
python src/main.py

# Daily bars only
python src/main.py --intervals 1d

# Custom config / output / delay
python src/main.py --config config/tickers.yaml --data-dir data --sleep 1.5 -v
```

| Flag           | Default                 | Description                          |
|----------------|-------------------------|--------------------------------------|
| `--config`     | `config/tickers.yaml`   | Ticker lists                         |
| `--data-dir`   | `data`                  | CSV root                             |
| `--intervals`  | `1d 1m`                 | One or both of `1d`, `1m`            |
| `--sleep`      | `1.0`                   | Seconds between ticker requests      |
| `-v`           | off                     | Debug logging                        |

Progress is printed per job, e.g. `Fetching AAPL [1d]... Success — 2 new/updated row(s)`.

## GitHub Actions

Workflow: [`.github/workflows/data_fetch.yml`](.github/workflows/data_fetch.yml)

| Trigger            | When                                      |
|--------------------|-------------------------------------------|
| `schedule`         | Cron `0 23 * * *` (23:00 UTC daily)       |
| `workflow_dispatch`| Manual run from the Actions tab           |

Steps: checkout → Python 3.10 → `pip install -r requirements.txt` → `python src/main.py` → commit & push changes under `data/` as `github-actions[bot]` when the tree changed.

### First-time notes

1. Push this repo to GitHub and enable Actions.
2. Ensure the default branch allows the `GITHUB_TOKEN` to write contents (Settings → Actions → General → Workflow permissions → **Read and write**), or use a PAT with `contents: write` if your org restricts the default token.
3. Trigger **Fetch Financial Data** via *Run workflow* for an initial backfill (first `1d` run downloads full history and may take several minutes).

## Robustness

- Per-ticker `try/except` — one failure does not abort the run.
- Configurable sleep between requests (default 1s) to ease Yahoo rate limits.
- Corrupt or unreadable existing CSVs trigger a full refetch for that ticker rather than a crash.
- Exit code `2` only if **every** fetch failed; partial success exits `0`.

## License

Use and modify freely for your own datasets and research.
