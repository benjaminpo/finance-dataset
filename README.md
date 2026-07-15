# Finance Dataset Pipeline

Automated pipeline that pulls historical and intraday market data from [Yahoo Finance](https://finance.yahoo.com/) via [`yfinance`](https://github.com/ranaroussi/yfinance) and writes partitioned CSV files under `data/`. GitHub Actions run on a **split schedule** (daily bars for the full universe; intraday for a smaller liquid set), **publish OHLCV to [Kaggle](https://www.kaggle.com/datasets/benjaminpo/finance-dataset)**, and commit listing CSV updates (not the bulk price files) back to this repository.

## Asset classes

| Config key   | Examples                         | Path prefix          |
|--------------|----------------------------------|----------------------|
| `stocks_us`  | NASDAQ listings ∪ S&P 500        | `data/stocks_us/`    |
| `stocks_kr`  | KOSPI + KOSDAQ (`.KS` / `.KQ`)   | `data/stocks_kr/`    |
| `stocks_jp`  | Tokyo Stock Exchange (`.T`)      | `data/stocks_jp/`    |
| `stocks_eu`  | STOXX/DAX/CAC/FTSE/AEX/IBEX/…    | `data/stocks_eu/`    |
| `stocks_hk`  | Hang Seng / China internet (HK)  | `data/stocks_hk/`    |
| `indices`    | ^GSPC, ^RUT, ^STOXX50E, ^KS11, … | `data/indices/`      |
| `rates`      | ^IRX, ^FVX, ^TNX, ^TYX           | `data/rates/`        |
| `futures`    | CL=F, ES=F                       | `data/futures/`      |
| `crypto`     | BTC-USD, ETH-USD, SOL-USD, …     | `data/crypto/`       |
| `currencies` | EURUSD=X, GBPUSD=X, KRW=X, …     | `data/currencies/`   |

Edit the lists in [`config/tickers.yaml`](config/tickers.yaml) to add or remove symbols.

**US stocks** are loaded as the union of Symbol columns from:

- [`config/listings/nasdaq-listed-symbols.csv`](config/listings/nasdaq-listed-symbols.csv) ([source](https://raw.githubusercontent.com/datasets/nasdaq-listings/master/data/nasdaq-listed-symbols.csv))
- [`config/listings/sp500-constituents.csv`](config/listings/sp500-constituents.csv) ([source](https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv))

Share classes like `BRK.B` are rewritten to Yahoo form (`BRK-B`). NASDAQ rows with `Test Issue = Y` are skipped, as are warrants / rights / units (limited Yahoo history).

Each pipeline run (including the scheduled GitHub Action) checks those remote URLs first, compares SHA-256 hashes, and updates the local CSVs when content changed. Listing updates are committed to git; OHLCV under `data/` is uploaded to Kaggle. Use `--skip-listings-refresh` to skip the check, or `--listings-only` to refresh listings without fetching market data.

**Korean stocks** (`stocks_kr`) are rebuilt each run from KOSPI + KOSDAQ via [FinanceDataReader](https://github.com/FinanceData/FinanceDataReader) into [`config/listings/krx-listed-symbols.csv`](config/listings/krx-listed-symbols.csv), with Yahoo suffixes `.KS` (KOSPI) and `.KQ` (KOSDAQ).

**Japanese stocks** (`stocks_jp`) are rebuilt each run from the Tokyo Stock Exchange via FinanceDataReader into [`config/listings/tse-listed-symbols.csv`](config/listings/tse-listed-symbols.csv), with Yahoo suffix `.T` (e.g. `7203.T`).

**European stocks** (`stocks_eu`) are rebuilt each run from major European indices (EURO STOXX 50, DAX/MDAX/SDAX/TecDAX, CAC 40/Mid 60, AEX, BEL 20, IBEX 35, FTSE 100, Switzerland 20, OMX Helsinki 25, OMX Stockholm 30) via [pytickersymbols](https://github.com/portfolioplus/pytickersymbols) into [`config/listings/europe-listed-symbols.csv`](config/listings/europe-listed-symbols.csv).

## Storage layout

```
data/
├── stocks_us/
│   ├── 1d/
│   │   └── AAPL.csv              # cumulative daily bars (incremental)
│   ├── 1wk/
│   │   └── AAPL.csv              # cumulative weekly bars
│   ├── 1m/
│   │   ├── AAPL_2026-07-09.csv   # dated 1-minute snapshots
│   │   └── AAPL_2026-07-10.csv
│   └── 5m/
│       └── AAPL_2026-07-10.csv   # dated 5-minute snapshots
├── crypto/
│   ├── 1d/
│   │   └── BTC-USD.csv
│   └── 1m/
│       └── BTC-USD_2026-07-10.csv
└── ...
```

### Interval strategy

Supported Yahoo intervals: `1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `90m`, `1h`, `1d`, `5d`, `1wk`, `1mo`, `3mo`.

- **Cumulative** (`1d`, `5d`, `1wk`, `1mo`, `3mo`) — One file per ticker. New bars are appended; duplicate timestamps are dropped (last write wins), so re-runs refresh the latest candle safely.
- **Intraday snapshots** (`1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `90m`, `1h`) — Yahoo only keeps a rolling window (`1m` ≈ 7 days; other intraday ≈ 60 days). Each run writes **dated snapshot files** (`TICKER_YYYY-MM-DD.csv`) for every calendar day in the returned window. Recent days are overwritten on subsequent runs; older snapshots remain, so history accumulates without one giant rolling file.

CSV index column is `Datetime` (UTC, ISO-8601). Columns: Open, High, Low, Close, Adj Close, Volume (plus Dividends / Stock Splits when present).

## Repository layout

```
├── .github/workflows/
│   ├── data_fetch_daily.yml
│   ├── data_fetch_intraday.yml
│   └── tests.yml
├── config/
│   ├── tickers.yaml            # full universe (daily/weekly)
│   ├── tickers_intraday.yaml   # S&P 500 + liquid (intraday)
│   └── kaggle/
│       └── dataset-metadata.json
├── data/                 # local OHLCV (gitignored; published to Kaggle)
├── scripts/
│   ├── batch_commit.py   # commit listing CSV updates
│   ├── pull_kaggle.py    # download latest Kaggle version into data/
│   └── publish_kaggle.py # upload data/ as a Kaggle dataset version
├── src/
│   ├── __init__.py
│   ├── fetcher.py      # download + CSV merge logic
│   ├── listings.py     # remote/KRX/TSE/Europe listing refresh
│   ├── main.py         # CLI entry point
│   └── summary.py      # fetch failure-rate report (CI artifact)
├── tests/
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
└── README.md
```

## Local setup

Requires **Python 3.11+** (`pytickersymbols` and related deps no longer support 3.10).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Runs the unit suite under `tests/` with coverage on `src/` and `scripts/` (fail under 80%). CI workflow: [`.github/workflows/tests.yml`](.github/workflows/tests.yml).

### Run the pipeline

```bash
# All intervals (default) — 8 parallel workers; full universe
python src/main.py

# Match CI daily job (full universe, 1d + 1wk)
python src/main.py --intervals 1d 1wk

# Match CI intraday job (S&P 500 + liquid)
python src/main.py --config config/tickers_intraday.yaml \
  --intervals 1m 2m 5m 15m 30m 60m 90m 1h

# Faster first backfill: daily only, then resume if interrupted
python src/main.py --intervals 1d --skip-existing

# Custom config / workers / delay
python src/main.py --config config/tickers.yaml --data-dir data --workers 12 --sleep 0.25 -v
```

| Flag              | Default                 | Description                                      |
|-------------------|-------------------------|--------------------------------------------------|
| `--config`        | `config/tickers.yaml`   | Ticker lists                                     |
| `--data-dir`      | `data`                  | CSV root                                         |
| `--intervals`     | all 13 Yahoo intervals  | Any of `1m`/`2m`/`5m`/`15m`/`30m`/`60m`/`90m`/`1h`/`1d`/`5d`/`1wk`/`1mo`/`3mo` |
| `--workers`       | `8`                     | Parallel Yahoo fetch threads                     |
| `--sleep`         | `0.25`                  | Seconds to pause after each request              |
| `--skip-existing` | off                     | Skip tickers that already have data (resume)     |
| `--summary-path`  | off                     | Write JSON (+ sibling `.md`) fetch failure report |
| `-v`              | off                     | Debug logging                                    |

CI splits the work so Actions stays practical: **daily** refreshes `1d` + `1wk` for the full listing universe (~12k symbols); **intraday** refreshes `1m`…`1h` only for S&P 500 + liquid crypto/indices/futures/FX ([`config/tickers_intraday.yaml`](config/tickers_intraday.yaml)). Prefer `--intervals 1d` for the first local backfill, then publish. Use `--skip-existing` to resume after an interrupt.

Progress is printed per job, e.g. `Fetching AAPL [1d]... Success — 2 new/updated row(s)`.

## GitHub Actions

| Workflow | File | Schedule | Config | Intervals |
|----------|------|----------|--------|-----------|
| **Fetch Daily Bars** | [`data_fetch_daily.yml`](.github/workflows/data_fetch_daily.yml) | `0 23 * * *` (23:00 UTC daily) | `tickers.yaml` | `1d` `1wk` |
| **Fetch Intraday Bars** | [`data_fetch_intraday.yml`](.github/workflows/data_fetch_intraday.yml) | `15 15,18,21 * * 1-5` (weekdays) | `tickers_intraday.yaml` | `1m`…`1h` |

Both also support `workflow_dispatch`. They share concurrency group `finance-dataset-kaggle` so pull/publish cannot race.

Steps (each workflow): checkout → install → [`pull_kaggle.py --optional`](scripts/pull_kaggle.py) (merge previous Kaggle version into `data/`) → `python src/main.py … --summary-path artifacts/fetch-summary.json` → upload **fetch summary** artifact (JSON + Markdown; also written to the job summary) → [`publish_kaggle.py`](scripts/publish_kaggle.py) → [`batch_commit.py`](scripts/batch_commit.py) for listing CSV updates.

The summary includes success/fail/skip counts, **failure rate** (failed ÷ attempted), breakdowns by interval and asset class, and per-ticker failure messages so Yahoo blanks / rate-limit gaps are visible without digging through the full log. Exit behavior is unchanged: the job only fails the fetch step when *every* ticker fails.

### Kaggle publish

Dataset: [benjaminpo/finance-dataset](https://www.kaggle.com/datasets/benjaminpo/finance-dataset)

Each workflow **pulls the current Kaggle version first**, updates its slice, then re-uploads the full tree so the other slice is not wiped.

```bash
export KAGGLE_API_TOKEN=...          # from https://www.kaggle.com/settings/api
python scripts/pull_kaggle.py --optional
python src/main.py --intervals 1d 1wk
python scripts/publish_kaggle.py

# Or validate without uploading:
python scripts/publish_kaggle.py --dry-run
```

| Flag / env                 | Default                         | Description                                      |
|----------------------------|---------------------------------|--------------------------------------------------|
| `--handle` / `KAGGLE_DATASET_HANDLE` | `benjaminpo/finance-dataset` | Kaggle dataset slug                          |
| `--data-dir`               | `data`                          | Local OHLCV root                                 |
| `--metadata`               | `config/kaggle/dataset-metadata.json` | Dataset title/id/license metadata      |
| `--version-notes`          | dated file-count summary        | Notes shown on the new Kaggle version            |
| `--dry-run` / `--optional` | off                             | Plan-only publish / soft-fail pull               |

Auth: set `KAGGLE_API_TOKEN`, or legacy `KAGGLE_USERNAME` + `KAGGLE_KEY`, or `~/.kaggle/kaggle.json`.

### First-time notes

1. Push this repo to GitHub and enable Actions.
2. Add repository secret `KAGGLE_API_TOKEN` (Settings → Secrets and variables → Actions) from [Kaggle API settings](https://www.kaggle.com/settings/api) → **Generate New Token**.
3. Ensure the default branch allows the `GITHUB_TOKEN` to write contents (Settings → Actions → General → Workflow permissions → **Read and write**) so listing commits can push.
4. The Kaggle target must be a **normal file dataset** (CSV tree under `data/`), not a “Create from GitHub” / repo-synced dataset. A GitHub-synced slug returns `Incompatible Dataset Type` on upload — delete it (or use a new `--handle`) before the first publish. After create, set the public title/description on the Kaggle UI if needed (`kagglehub` creates private datasets titled with the slug).
5. Run **Fetch Daily Bars** first (or locally: `--intervals 1d 1wk`, then `publish_kaggle.py`). Then enable/run **Fetch Intraday Bars**.

## Robustness

- Per-ticker `try/except` — one failure does not abort the run.
- Parallel workers (default 8) plus a short per-request sleep to ease Yahoo rate limits.
- In-process Yahoo response cache — identical ticker/interval requests are not downloaded twice in one run.
- `--skip-existing` resumes long backfills without re-downloading completed tickers.
- Corrupt or unreadable existing CSVs trigger a full refetch for that ticker rather than a crash.
- Exit code `2` only if **every** fetch failed; partial success exits `0`.
