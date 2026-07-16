"""Unit tests for src.fetcher helpers and CSV persistence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from src import fetcher
from src.fetcher import (
    MIN_TRUSTED_DAILY_ROWS,
    _normalize_frame,
    _to_yahoo_symbol,
    clear_fetch_cache,
    fetch_daily_history,
    fetch_history,
    load_symbols_from_csv,
    load_tickers,
    run_pipeline,
    save_daily,
    save_intraday_snapshots,
    select_tickers,
    update_ticker_1d,
    update_ticker_1m,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_fetch_cache()
    yield
    clear_fetch_cache()


# ---------------------------------------------------------------------------
# _to_yahoo_symbol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("BRK.B", "BRK-B"),
        ("BF.B", "BF-B"),
        ("AAPL", "AAPL"),
        ("  MSFT  ", "MSFT"),
        ("005930.KS", "005930.KS"),
        ("035420.KQ", "035420.KQ"),
        ("7203.T", "7203.T"),
        ("0700.HK", "0700.HK"),
        ("EURUSD=X", "EURUSD=X"),
        ("CL=F", "CL=F"),
        ("BTC-USD", "BTC-USD"),
        ("SAP.DE", "SAP.DE"),
    ],
)
def test_to_yahoo_symbol(raw: str, expected: str) -> None:
    assert _to_yahoo_symbol(raw) == expected


# ---------------------------------------------------------------------------
# load_symbols_from_csv
# ---------------------------------------------------------------------------


def test_load_symbols_from_csv_filters_and_dedupes(tmp_path: Path) -> None:
    csv_path = tmp_path / "listings.csv"
    csv_path.write_text(
        "Symbol,Security Name,Test Issue\n"
        "AAPL,Apple Inc.,N\n"
        "BRK.B,Berkshire Hathaway Class B,N\n"
        "TEST,Fake Test Co,Y\n"
        "FOOW,Foo Warrant,N\n"
        "BARR,Bar Rights,N\n"
        "BAZU,Baz Unit,N\n"
        "AAPL,Apple duplicate,N\n"
        "MSFT,Microsoft Corporation,N\n",
        encoding="utf-8",
    )
    symbols = load_symbols_from_csv(csv_path)
    assert symbols == ["AAPL", "BRK-B", "MSFT"]


def test_load_symbols_from_csv_requires_symbol_column(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("Ticker\nAAPL\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Symbol"):
        load_symbols_from_csv(csv_path)


# ---------------------------------------------------------------------------
# load_tickers
# ---------------------------------------------------------------------------


def test_load_tickers_merges_inline_and_listings(tmp_path: Path) -> None:
    listing = tmp_path / "extra.csv"
    listing.write_text("Symbol\nZZZZ\nAAPL\n", encoding="utf-8")
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "crypto:\n"
        "  - BTC-USD\n"
        "stocks_us:\n"
        "  - AAPL\n"
        "  - MSFT\n"
        "listings:\n"
        "  stocks_us:\n"
        "    - listings/extra.csv\n"
        "  crypto:\n"
        "    - path: listings/extra.csv\n",
        encoding="utf-8",
    )
    # Path in yaml is relative to config dir; recreate under listings/
    (tmp_path / "listings").mkdir()
    (tmp_path / "listings" / "extra.csv").write_text(
        "Symbol\nZZZZ\nAAPL\n", encoding="utf-8"
    )
    config.write_text(
        "crypto:\n"
        "  - BTC-USD\n"
        "stocks_us:\n"
        "  - AAPL\n"
        "  - MSFT\n"
        "listings:\n"
        "  stocks_us:\n"
        "    - listings/extra.csv\n",
        encoding="utf-8",
    )
    tickers = load_tickers(config)
    assert tickers["stocks_us"] == ["AAPL", "MSFT", "ZZZZ"]
    assert tickers["crypto"] == ["BTC-USD"]


def test_load_tickers_skips_missing_listing_file(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "indices:\n"
        "  - ^GSPC\n"
        "listings:\n"
        "  indices:\n"
        "    - missing.csv\n",
        encoding="utf-8",
    )
    tickers = load_tickers(config)
    assert tickers["indices"] == ["^GSPC"]


def test_load_tickers_coerces_yaml_integers(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "stocks_hk:\n"
        "  - 0700.HK\n",
        encoding="utf-8",
    )
    tickers = load_tickers(config)
    assert tickers["stocks_hk"] == ["0700.HK"]


def test_select_tickers_filters_classes_and_shards() -> None:
    tickers = {
        "stocks_us": ["C", "A", "B", "D"],
        "crypto": ["BTC-USD", "ETH-USD"],
        "stocks_kr": ["005930.KS"],
    }
    assert select_tickers(tickers, asset_classes=["crypto"]) == {
        "crypto": ["BTC-USD", "ETH-USD"]
    }
    # Sorted within class: A,B,C,D → shards 0/2 = A,C ; 1/2 = B,D
    assert select_tickers(
        tickers, asset_classes=["stocks_us"], shard_index=0, shard_count=2
    ) == {"stocks_us": ["A", "C"]}
    assert select_tickers(
        tickers, asset_classes=["stocks_us"], shard_index=1, shard_count=2
    ) == {"stocks_us": ["B", "D"]}


def test_select_tickers_rejects_bad_shard_or_class() -> None:
    tickers = {"crypto": ["BTC-USD"]}
    with pytest.raises(ValueError, match="Unknown asset class"):
        select_tickers(tickers, asset_classes=["nope"])
    with pytest.raises(ValueError, match="shard_count"):
        select_tickers(tickers, shard_count=0)
    with pytest.raises(ValueError, match="shard_index"):
        select_tickers(tickers, shard_index=2, shard_count=2)


# ---------------------------------------------------------------------------
# _normalize_frame
# ---------------------------------------------------------------------------


def test_normalize_frame_empty() -> None:
    assert _normalize_frame(pd.DataFrame()).empty
    assert _normalize_frame(None).empty  # type: ignore[arg-type]


def test_normalize_frame_flattens_multiindex_and_dedupes() -> None:
    idx = pd.to_datetime(
        ["2024-01-02", "2024-01-02", "2024-01-03"], utc=True
    )
    cols = pd.MultiIndex.from_product([["Open", "Close", "Volume"], ["AAPL"]])
    raw = pd.DataFrame(
        [[1.0, 2.0, 10], [1.5, 2.5, 11], [3.0, 4.0, 12]],
        index=idx,
        columns=cols,
    )
    out = _normalize_frame(raw)
    assert list(out.columns) == ["Open", "Close", "Volume"]
    assert out.index.name == "Datetime"
    assert len(out) == 2
    assert float(out.loc[idx[0], "Open"]) == 1.5


# ---------------------------------------------------------------------------
# CSV save helpers
# ---------------------------------------------------------------------------


def _sample_daily(rows: int = 3) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=rows, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "Open": range(rows),
            "High": range(1, rows + 1),
            "Low": range(rows),
            "Close": range(1, rows + 1),
            "Adj Close": range(1, rows + 1),
            "Volume": [100 + i for i in range(rows)],
        },
        index=idx,
    ).rename_axis("Datetime")


def test_save_daily_creates_and_merges(tmp_path: Path) -> None:
    path = tmp_path / "AAPL.csv"
    first = _sample_daily(3)
    assert save_daily(first, path) == 3

    # Overlap last day + one new day
    update = _sample_daily(2)
    update.index = pd.to_datetime(
        ["2024-01-03", "2024-01-04"], utc=True
    )
    update.index.name = "Datetime"
    update["Close"] = [99.0, 100.0]
    n = save_daily(update, path)
    assert n == 1
    loaded = pd.read_csv(path, index_col="Datetime", parse_dates=True)
    assert len(loaded) == 4
    assert float(loaded.iloc[-2]["Close"]) == 99.0


def test_save_intraday_snapshots_by_day(tmp_path: Path) -> None:
    idx = pd.to_datetime(
        [
            "2024-06-01 14:30:00+00:00",
            "2024-06-01 14:31:00+00:00",
            "2024-06-02 14:30:00+00:00",
        ]
    )
    df = pd.DataFrame(
        {
            "Open": [1.0, 1.1, 2.0],
            "High": [1.2, 1.3, 2.2],
            "Low": [0.9, 1.0, 1.9],
            "Close": [1.1, 1.2, 2.1],
            "Adj Close": [1.1, 1.2, 2.1],
            "Volume": [10, 11, 20],
        },
        index=idx,
    ).rename_axis("Datetime")

    n = save_intraday_snapshots(df, tmp_path, "crypto", "BTC-USD")
    assert n == 3
    day1 = tmp_path / "crypto" / "1m" / "BTC-USD_2024-06-01.csv"
    day2 = tmp_path / "crypto" / "1m" / "BTC-USD_2024-06-02.csv"
    assert day1.exists() and day2.exists()
    assert len(pd.read_csv(day1)) == 2
    assert len(pd.read_csv(day2)) == 1


def test_save_intraday_empty_returns_zero(tmp_path: Path) -> None:
    assert save_intraday_snapshots(pd.DataFrame(), tmp_path, "crypto", "BTC-USD") == 0


# ---------------------------------------------------------------------------
# fetch_history / cache / singleflight
# ---------------------------------------------------------------------------


def test_fetch_history_caches_and_copies() -> None:
    raw = _sample_daily(5)
    with patch("src.fetcher.yf.download", return_value=raw) as mock_dl:
        a = fetch_history("AAPL", "1d", period="max")
        b = fetch_history("AAPL", "1d", period="max")
        assert mock_dl.call_count == 1
        assert a.equals(b)
        a.iloc[0, 0] = -1
        c = fetch_history("AAPL", "1d", period="max")
        assert float(c.iloc[0, 0]) != -1


def test_fetch_history_singleflight() -> None:
    raw = _sample_daily(2)
    call_count = 0

    def slow_download(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return raw

    with patch("src.fetcher.yf.download", side_effect=slow_download):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(fetch_history, "AAPL", "1d", period="max")
                for _ in range(8)
            ]
            results = [f.result() for f in futures]
    assert call_count == 1
    assert all(len(r) == 2 for r in results)


def test_fetch_daily_history_falls_back_periods() -> None:
    good = _sample_daily(3)

    def fake_fetch(ticker, interval, start=None, period=None):
        if period == "max":
            return pd.DataFrame()
        if period == "5d":
            return good
        return pd.DataFrame()

    with patch("src.fetcher.fetch_history", side_effect=fake_fetch) as mock_fh:
        out = fetch_daily_history("THIN")
        assert len(out) == 3
        periods = [c.kwargs.get("period") for c in mock_fh.call_args_list]
        assert periods[:2] == ["max", "5d"]


def test_fetch_daily_history_with_start_skips_fallback() -> None:
    with patch("src.fetcher.fetch_history", return_value=_sample_daily(1)) as mock_fh:
        fetch_daily_history("AAPL", start="2024-01-01")
        mock_fh.assert_called_once_with("AAPL", "1d", start="2024-01-01")


# ---------------------------------------------------------------------------
# update_ticker_1d / 1m
# ---------------------------------------------------------------------------


def test_update_ticker_1d_full_and_incremental(tmp_path: Path) -> None:
    data = _sample_daily(MIN_TRUSTED_DAILY_ROWS + 5)
    with patch("src.fetcher.fetch_cumulative_history", return_value=data):
        ok, msg = update_ticker_1d("AAPL", "stocks_us", tmp_path)
        assert ok
        assert "new/updated" in msg

    # Short CSV triggers full refetch (not trusted)
    short_path = tmp_path / "stocks_us" / "1d" / "MSFT.csv"
    short_path.parent.mkdir(parents=True, exist_ok=True)
    save_daily(_sample_daily(5), short_path)
    with patch("src.fetcher.fetch_cumulative_history", return_value=data) as mock_fc:
        ok, _ = update_ticker_1d("MSFT", "stocks_us", tmp_path)
        assert ok
        assert mock_fc.call_args.kwargs.get("start") is None


def test_update_ticker_1d_skip_existing(tmp_path: Path) -> None:
    data = _sample_daily(MIN_TRUSTED_DAILY_ROWS + 1)
    path = tmp_path / "stocks_us" / "1d" / "AAPL.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_daily(data, path)
    with patch("src.fetcher.fetch_cumulative_history") as mock_fc:
        ok, msg = update_ticker_1d(
            "AAPL", "stocks_us", tmp_path, skip_existing=True
        )
        assert ok and "skipped" in msg
        mock_fc.assert_not_called()


def test_update_ticker_1d_empty_and_error(tmp_path: Path) -> None:
    with patch("src.fetcher.fetch_cumulative_history", return_value=pd.DataFrame()):
        ok, msg = update_ticker_1d("NONE", "stocks_us", tmp_path)
        assert not ok
        assert "No 1d data" in msg

    with patch(
        "src.fetcher.fetch_cumulative_history", side_effect=RuntimeError("boom")
    ):
        ok, msg = update_ticker_1d("BAD", "stocks_us", tmp_path)
        assert not ok
        assert "boom" in msg


def test_update_ticker_1m_writes_snapshots(tmp_path: Path) -> None:
    idx = pd.to_datetime(["2024-06-01 14:30:00+00:00", "2024-06-01 14:31:00+00:00"])
    df = pd.DataFrame(
        {
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
            "Adj Close": [1.1, 1.2],
            "Volume": [10, 11],
        },
        index=idx,
    ).rename_axis("Datetime")
    with patch("src.fetcher.fetch_history", return_value=df):
        ok, msg = update_ticker_1m("BTC-USD", "crypto", tmp_path)
        assert ok
        assert "row(s)" in msg
        assert (tmp_path / "crypto" / "1m" / "BTC-USD_2024-06-01.csv").exists()


def test_update_ticker_1m_skip_existing(tmp_path: Path) -> None:
    today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    path = tmp_path / "crypto" / "1m" / f"BTC-USD_{today}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Datetime,Open\n", encoding="utf-8")
    with patch("src.fetcher.fetch_history") as mock_fh:
        ok, msg = update_ticker_1m(
            "BTC-USD", "crypto", tmp_path, skip_existing=True
        )
        assert ok and "skipped" in msg
        mock_fh.assert_not_called()


def test_update_ticker_1m_empty(tmp_path: Path) -> None:
    with patch("src.fetcher.fetch_history", return_value=pd.DataFrame()):
        ok, msg = update_ticker_1m("ILLIQ", "crypto", tmp_path)
        assert not ok
        assert "No 1m data" in msg


# ---------------------------------------------------------------------------
# run_pipeline
# ---------------------------------------------------------------------------


def test_run_pipeline_summary(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("crypto:\n  - BTC-USD\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    def fake_run_one(**kwargs):
        if kwargs["interval"] == "1d":
            return {
                "status": "success",
                "ticker": kwargs["ticker"],
                "asset_class": kwargs["asset_class"],
                "interval": kwargs["interval"],
                "message": "ok",
            }
        return {
            "status": "failed",
            "ticker": kwargs["ticker"],
            "asset_class": kwargs["asset_class"],
            "interval": kwargs["interval"],
            "message": "no data",
        }

    with patch("src.fetcher._run_one_job", side_effect=fake_run_one):
        summary = run_pipeline(
            config,
            data_dir,
            intervals=["1d", "1m"],
            workers=1,
            sleep_seconds=0,
        )
    assert summary["success"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 0
    assert summary["total"] == 2
    assert summary["attempted"] == 2
    assert summary["failure_rate"] == 0.5
    assert summary["failures"] == [
        {
            "ticker": "BTC-USD",
            "asset_class": "crypto",
            "interval": "1m",
            "message": "no data",
        }
    ]
    assert summary["by_interval"]["1d"]["success"] == 1
    assert summary["by_interval"]["1m"]["failed"] == 1
    assert summary["by_asset_class"]["crypto"]["failed"] == 1


def test_run_pipeline_parallel_workers(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("indices:\n  - ^GSPC\n  - ^DJI\n", encoding="utf-8")

    def ok_job(**kwargs):
        return {
            "status": "success",
            "ticker": kwargs["ticker"],
            "asset_class": kwargs["asset_class"],
            "interval": kwargs["interval"],
            "message": "ok",
        }

    with patch("src.fetcher._run_one_job", side_effect=ok_job) as mock_job:
        summary = run_pipeline(
            config,
            tmp_path / "data",
            intervals=["1d"],
            workers=2,
            sleep_seconds=0,
        )
    assert summary["success"] == 2
    assert summary["failure_rate"] == 0.0
    assert mock_job.call_count == 2


def test_last_timestamp_corrupt_csv(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("not,a,valid,csv\n{{{", encoding="utf-8")
    assert fetcher._last_timestamp(path) is None


def test_csv_path_sanitization() -> None:
    p = fetcher._csv_path_1d(Path("data"), "indices", "^GSPC")
    assert p.name == "GSPC.csv"
    p2 = fetcher._csv_path_1m(Path("data"), "futures", "CL=F", "2024-01-01")
    assert p2.name == "CL_F_2024-01-01.csv"


def test_update_ticker_1d_incremental_start(tmp_path: Path) -> None:
    existing = _sample_daily(MIN_TRUSTED_DAILY_ROWS + 2)
    path = tmp_path / "stocks_us" / "1d" / "AAPL.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_daily(existing, path)

    last = existing.index.max()
    update = _sample_daily(2)
    update.index = pd.to_datetime([last, last + pd.Timedelta(days=1)], utc=True)
    update.index.name = "Datetime"

    with patch("src.fetcher.fetch_cumulative_history", return_value=update) as mock_fc:
        ok, msg = update_ticker_1d("AAPL", "stocks_us", tmp_path)
        assert ok
        assert mock_fc.call_args.kwargs.get("start") is not None
        assert "new/updated" in msg


def test_update_ticker_1d_incremental_empty(tmp_path: Path) -> None:
    existing = _sample_daily(MIN_TRUSTED_DAILY_ROWS + 2)
    path = tmp_path / "stocks_us" / "1d" / "AAPL.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_daily(existing, path)
    with patch("src.fetcher.fetch_cumulative_history", return_value=pd.DataFrame()):
        ok, msg = update_ticker_1d("AAPL", "stocks_us", tmp_path)
        assert ok
        assert "0 new/updated" in msg


def test_fetch_history_propagates_errors() -> None:
    with patch("src.fetcher.yf.download", side_effect=RuntimeError("network")):
        with pytest.raises(RuntimeError, match="network"):
            fetch_history("AAPL", "1d", period="max")


def test_fetch_daily_history_all_empty() -> None:
    with patch("src.fetcher.fetch_history", return_value=pd.DataFrame()):
        assert fetch_daily_history("EMPTY").empty


def test_run_one_job_success_paths(tmp_path: Path) -> None:
    with patch("src.fetcher.update_ticker_cumulative", return_value=(True, "ok")):
        result = fetcher._run_one_job(
            done=1,
            total=1,
            ticker="AAPL",
            asset_class="stocks_us",
            interval="1d",
            data_dir=tmp_path,
            sleep_seconds=0,
            skip_existing=False,
        )
        assert result["status"] == "success"
        assert result["ticker"] == "AAPL"
        assert result["message"] == "ok"
    with patch("src.fetcher.update_ticker_snapshot", return_value=(False, "nope")):
        result = fetcher._run_one_job(
            done=1,
            total=1,
            ticker="AAPL",
            asset_class="stocks_us",
            interval="1m",
            data_dir=tmp_path,
            sleep_seconds=0,
            skip_existing=False,
        )
        assert result["status"] == "failed"
        assert result["message"] == "nope"
    with patch("src.fetcher.update_ticker_snapshot", return_value=(True, "ok")):
        assert (
            fetcher._run_one_job(
                done=1,
                total=1,
                ticker="AAPL",
                asset_class="stocks_us",
                interval="15m",
                data_dir=tmp_path,
                sleep_seconds=0,
                skip_existing=False,
            )["status"]
            == "success"
        )
    with patch("src.fetcher.update_ticker_cumulative", return_value=(True, "ok")):
        assert (
            fetcher._run_one_job(
                done=1,
                total=1,
                ticker="AAPL",
                asset_class="stocks_us",
                interval="1mo",
                data_dir=tmp_path,
                sleep_seconds=0,
                skip_existing=False,
            )["status"]
            == "success"
        )


def test_run_pipeline_worker_crash(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text("crypto:\n  - BTC-USD\n", encoding="utf-8")
    with patch("src.fetcher._run_one_job", side_effect=RuntimeError("crash")):
        summary = run_pipeline(
            config,
            tmp_path / "data",
            intervals=["1d"],
            workers=2,
            sleep_seconds=0,
        )
    assert summary["failed"] == 1
    assert summary["failures"][0]["ticker"] == "BTC-USD"
    assert "crash" in summary["failures"][0]["message"]
    assert summary["failure_rate"] == 1.0


def test_normalize_retains_dividends() -> None:
    idx = pd.to_datetime(["2024-01-02"], utc=True)
    raw = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.0],
            "Low": [1.0],
            "Close": [1.0],
            "Adj Close": [1.0],
            "Volume": [1],
            "Dividends": [0.5],
            "Stock Splits": [0.0],
        },
        index=idx,
    )
    out = _normalize_frame(raw)
    assert "Dividends" in out.columns and "Stock Splits" in out.columns


def test_load_tickers_empty_listings_entry(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "crypto:\n"
        "  - BTC-USD\n"
        "orphan: null\n"
        "listings:\n"
        "  crypto: []\n",
        encoding="utf-8",
    )
    tickers = load_tickers(config)
    assert tickers["crypto"] == ["BTC-USD"]
    assert "orphan" not in tickers


def test_last_timestamp_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("Datetime,Open,High,Low,Close,Adj Close,Volume\n", encoding="utf-8")
    assert fetcher._last_timestamp(path) is None
    assert fetcher._last_timestamp(tmp_path / "missing.csv") is None


def test_fetch_history_with_start() -> None:
    with patch("src.fetcher.yf.download", return_value=_sample_daily(1)) as mock_dl:
        fetch_history("AAPL", "1d", start="2024-01-01")
        assert mock_dl.call_args.kwargs["start"] == "2024-01-01"


def test_run_one_job_unsupported_interval(tmp_path: Path) -> None:
    result = fetcher._run_one_job(
        done=1,
        total=1,
        ticker="AAPL",
        asset_class="stocks_us",
        interval="4h",
        data_dir=tmp_path,
        sleep_seconds=0,
        skip_existing=False,
    )
    assert result["status"] == "skipped"
    assert "unsupported" in result["message"]


def test_update_ticker_snapshot_5m(tmp_path: Path) -> None:
    idx = pd.to_datetime(["2024-06-01 14:30:00+00:00", "2024-06-01 14:35:00+00:00"])
    df = pd.DataFrame(
        {
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
            "Adj Close": [1.1, 1.2],
            "Volume": [10, 11],
        },
        index=idx,
    ).rename_axis("Datetime")
    with patch("src.fetcher.fetch_history", return_value=df) as mock_fh:
        ok, msg = fetcher.update_ticker_snapshot(
            "BTC-USD", "crypto", "5m", tmp_path
        )
        assert ok
        assert "row(s)" in msg
        mock_fh.assert_called_once_with("BTC-USD", "5m", period="60d")
        assert (tmp_path / "crypto" / "5m" / "BTC-USD_2024-06-01.csv").exists()


def test_update_ticker_cumulative_1wk(tmp_path: Path) -> None:
    data = _sample_daily(MIN_TRUSTED_DAILY_ROWS + 2)
    with patch("src.fetcher.fetch_cumulative_history", return_value=data) as mock_fc:
        ok, msg = fetcher.update_ticker_cumulative(
            "AAPL", "stocks_us", "1wk", tmp_path
        )
        assert ok
        assert "new/updated" in msg
        mock_fc.assert_called_once()
        assert mock_fc.call_args.args[:2] == ("AAPL", "1wk")
        assert (tmp_path / "stocks_us" / "1wk" / "AAPL.csv").exists()
