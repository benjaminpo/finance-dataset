"""Unit tests for src.listings."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import pandas as pd
import pytest

from src.listings import (
    ASX_DIRECTORY_URL,
    EUROPE_SUFFIXES,
    NSE_EQUITY_URL,
    _sha256,
    _write_if_changed,
    build_asx_listing_csv,
    build_cn_listing_csv,
    build_europe_listing_csv,
    build_krx_listing_csv,
    build_nse_listing_csv,
    build_plain_ticker_listing_csv,
    build_russell2000_listing_csv,
    build_tse_listing_csv,
    build_tsx_listing_csv,
    build_twse_listing_csv,
    download_bytes,
    normalize_listing_entry,
    refresh_listings,
)


def test_sha256_stable() -> None:
    assert _sha256(b"abc") == _sha256(b"abc")
    assert _sha256(b"abc") != _sha256(b"abd")


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("listings/foo.csv", {"path": "listings/foo.csv", "url": None, "source": None}),
        (
            {"path": "a.csv", "url": "https://example.com/a.csv"},
            {"path": "a.csv", "url": "https://example.com/a.csv", "source": None},
        ),
        (
            {"path": "krx.csv", "source": "krx"},
            {"path": "krx.csv", "url": None, "source": "krx"},
        ),
    ],
)
def test_normalize_listing_entry(entry, expected) -> None:
    assert normalize_listing_entry(entry) == expected


def test_normalize_listing_entry_errors() -> None:
    with pytest.raises(ValueError, match="path"):
        normalize_listing_entry({"url": "https://x"})
    with pytest.raises(ValueError, match="Invalid"):
        normalize_listing_entry(42)


def test_write_if_changed(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    assert _write_if_changed(path, b"hello") is True
    assert path.read_bytes() == b"hello"
    assert _write_if_changed(path, b"hello") is False
    assert _write_if_changed(path, b"world") is True
    assert path.read_bytes() == b"world"


def test_download_bytes() -> None:
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"data"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("src.listings.urllib.request.urlopen", return_value=mock_resp) as mock_open:
        assert download_bytes("https://example.com/x.csv") == b"data"
        req = mock_open.call_args[0][0]
        assert req.get_header("User-agent") or req.headers.get("User-agent")


def test_download_bytes_extra_headers() -> None:
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"data"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("src.listings.urllib.request.urlopen", return_value=mock_resp) as mock_open:
        download_bytes(
            "https://example.com/x.csv",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"},
        )
    req = mock_open.call_args[0][0]
    assert req.get_header("User-agent") == "Mozilla/5.0"
    assert req.get_header("Accept") == "text/csv"


def test_build_krx_listing_csv() -> None:
    kospi = pd.DataFrame({"Code": ["5930", "123"], "Name": ["Samsung", "Tiny"]})
    kosdaq = pd.DataFrame({"Code": ["35420"], "Name": ["NAVER"]})

    def fake_listing(market):
        return kospi if market == "KOSPI" else kosdaq

    fake_fdr = MagicMock(StockListing=fake_listing)
    with patch.dict("sys.modules", {"FinanceDataReader": fake_fdr}):
        raw = build_krx_listing_csv()
    text = raw.decode("utf-8")
    assert "005930.KS" in text
    assert "000123.KS" in text
    assert "035420.KQ" in text


def test_build_krx_listing_csv_empty_raises() -> None:
    fake_fdr = MagicMock(StockListing=lambda _m: pd.DataFrame())
    with patch.dict("sys.modules", {"FinanceDataReader": fake_fdr}):
        with pytest.raises(RuntimeError, match="Empty KRX"):
            build_krx_listing_csv()


def test_build_tse_listing_csv() -> None:
    df = pd.DataFrame(
        {"Code": ["7203", "6758"], "Name": ["Toyota", "Sony"], "Industry": ["Auto", "Elec"]}
    )
    fake_fdr = MagicMock(StockListing=lambda _m: df)
    with patch.dict("sys.modules", {"FinanceDataReader": fake_fdr}):
        raw = build_tse_listing_csv()
    text = raw.decode("utf-8")
    assert "7203.T" in text
    assert "6758.T" in text
    assert "Industry" in text


def test_build_europe_listing_csv() -> None:
    stocks = [
        {
            "name": "SAP",
            "symbols": [
                {"yahoo": "SAP.DE"},
                {"yahoo": "0A2W.L"},  # numeric-leading LSE — skipped by preference
            ],
        },
        {
            "name": "ASML",
            "symbols": [{"yahoo": "ASML.AS"}],
        },
        {
            "name": "NoMatch",
            "symbols": [{"yahoo": "XYZ.US"}],
        },
    ]

    class FakePTS:
        def get_stocks_by_index(self, _name):
            return stocks

    fake_mod = MagicMock(PyTickerSymbols=FakePTS)
    with patch.dict("sys.modules", {"pytickersymbols": fake_mod}):
        raw = build_europe_listing_csv()
    text = raw.decode("utf-8")
    assert "SAP.DE" in text
    assert "ASML.AS" in text
    assert "XYZ.US" not in text
    assert all(sfx in EUROPE_SUFFIXES for sfx in (".DE", ".AS"))


def test_build_twse_listing_csv() -> None:
    import json

    payload = json.dumps(
        [
            {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24"},
            {"公司代號": "1101", "公司簡稱": "台泥", "產業別": "01"},
            {"公司代號": "skip", "公司簡稱": "bad"},
        ],
        ensure_ascii=False,
    ).encode("utf-8")
    with patch("src.listings.download_bytes", return_value=payload) as mock_dl:
        raw = build_twse_listing_csv(url="https://example.com/twse.json")
    mock_dl.assert_called_once_with("https://example.com/twse.json")
    text = raw.decode("utf-8")
    assert text.startswith("Symbol,Name,Industry\n")
    assert "2330.TW" in text
    assert "1101.TW" in text
    assert "skip.TW" not in text
    assert text.index("1101.TW") < text.index("2330.TW")


def test_build_twse_listing_csv_empty_raises() -> None:
    with patch("src.listings.download_bytes", return_value=b"[]"):
        with pytest.raises(RuntimeError, match="Empty TWSE listing"):
            build_twse_listing_csv()


def test_build_tsx_listing_csv() -> None:
    import json

    tsx = {
        "results": [
            {
                "symbol": "SHOP",
                "name": "Shopify Inc.",
                "instruments": [{"symbol": "SHOP", "name": "Shopify"}],
            },
            {
                "symbol": "RY",
                "name": "Royal Bank of Canada",
                "instruments": [
                    {"symbol": "RY", "name": "Royal Bank"},
                    {"symbol": "RY.PR.A", "name": "pref"},
                    {"symbol": "RY.U", "name": "usd"},
                    {"symbol": "RY", "name": "dup"},
                    "skip-me",
                ],
            },
            {
                "symbol": "REI.UN",
                "name": "RioCan REIT",
                "instruments": [{"symbol": "REI.UN", "name": "RioCan Un"}],
            },
            {
                "symbol": "CTC",
                "name": "Canadian Tire Corporation",
                "instruments": [
                    {"symbol": "CTC", "name": "CTC"},
                    {"symbol": "CTC.A", "name": "CTC A"},
                ],
            },
            {
                "symbol": "ZWC",
                "name": "BMO Covered Call ETF",
                "instruments": [
                    {"symbol": "ZWC", "name": "ETF"},
                    {"symbol": "ZWC.F", "name": "series f"},
                    {"symbol": "ZWC.T", "name": "series t"},
                ],
            },
            {
                "symbol": "HOT.DB.V",
                "name": "",
                "instruments": [
                    {"symbol": "HOT.DB.V", "name": "debenture"},
                    {"symbol": "HOT.WT", "name": "warrant"},
                    {"symbol": "HOT.UN", "name": "American Hotel units"},
                ],
            },
            "not-a-row",
            {"symbol": "NOINST", "name": "No Instruments Co"},
        ]
    }
    tsxv = {
        "results": [
            {
                "symbol": "ONE",
                "name": "01 Quantum Inc.",
                "instruments": [{"symbol": "ONE", "name": "01 Quantum"}],
            },
            {
                "symbol": "FIVD.P",
                "name": "5D Acquisition Corp.",
                "instruments": [{"symbol": "FIVD.P", "name": "5D"}],
            },
            {
                "symbol": "BAD",
                "name": "Bad",
                "instruments": [{"symbol": "1BAD", "name": "Bad"}],
            },
        ]
    }
    payloads = [
        json.dumps(tsx).encode("utf-8"),
        json.dumps(tsxv).encode("utf-8"),
    ]
    with patch("src.listings.download_bytes", side_effect=payloads) as mock_dl:
        raw = build_tsx_listing_csv(tsx_url="https://example.com/tsx", tsxv_url="https://example.com/tsxv")
    assert mock_dl.call_args_list[0].args == ("https://example.com/tsx",)
    assert mock_dl.call_args_list[1].args == ("https://example.com/tsxv",)
    text = raw.decode("utf-8")
    assert text.startswith("Symbol,Name,Market\n")
    for symbol in (
        "SHOP.TO",
        "RY.TO",
        "REI-UN.TO",
        "CTC.TO",
        "CTC-A.TO",
        "ZWC.TO",
        "HOT-UN.TO",
        "NOINST.TO",
        "ONE.V",
        "FIVD-P.V",
    ):
        assert symbol in text
    for skipped in ("RY-PR-A", "RY-U.TO", "ZWC-F", "ZWC-T", "HOT-WT", "1BAD"):
        assert skipped not in text
    assert text.count("RY.TO") == 1
    assert "American Hotel units" in text
    assert "TSXV" in text
    assert text.index("CTC-A.TO") < text.index("SHOP.TO")


def test_build_tsx_listing_csv_empty_raises() -> None:
    with patch("src.listings.download_bytes", return_value=b'{"results": []}'):
        with pytest.raises(RuntimeError, match="Empty TMX listing"):
            build_tsx_listing_csv()


def test_build_tsx_listing_csv_bad_json() -> None:
    with patch("src.listings.download_bytes", return_value=b"not-json"):
        with pytest.raises(RuntimeError, match="Invalid TMX listing JSON"):
            build_tsx_listing_csv()


def test_build_asx_listing_csv() -> None:
    payload = (
        '"ASX code","Company name","GICs industry group"\n'
        '"BHP","BHP GROUP LIMITED","Materials"\n'
        '"bhp","duplicate","DupInd"\n'
        '"","blank",""\n'
        '"CBA","COMMONWEALTH BANK","Banks"\n'
        '"***","bad",""\n'
        '"14D","1414 DEGREES LIMITED","Capital Goods"\n'
    ).encode("utf-8")
    with patch("src.listings.download_bytes", return_value=payload) as mock_dl:
        raw = build_asx_listing_csv(url="https://example.com/asx.csv")
    mock_dl.assert_called_once_with("https://example.com/asx.csv")
    text = raw.decode("utf-8")
    assert text.startswith("Symbol,Name,Industry\n")
    assert "BHP.AX" in text
    assert "CBA.AX" in text
    assert "14D.AX" in text
    assert text.count("BHP.AX") == 1
    assert "BHP GROUP LIMITED" in text
    assert "duplicate" not in text
    assert "***" not in text
    assert text.index("14D.AX") < text.index("BHP.AX") < text.index("CBA.AX")


def test_build_asx_listing_csv_empty_raises() -> None:
    payload = b'"ASX code","Company name"\n"",""\n'
    with patch("src.listings.download_bytes", return_value=payload):
        with pytest.raises(RuntimeError, match="Empty ASX listing"):
            build_asx_listing_csv()


def test_build_asx_listing_csv_missing_column() -> None:
    with patch("src.listings.download_bytes", return_value=b"Foo\n1\n"):
        with pytest.raises(RuntimeError, match="ASX code"):
            build_asx_listing_csv()


def test_build_nse_listing_csv() -> None:
    payload = (
        "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING\n"
        "RELIANCE,Reliance Industries Limited,EQ,01-JAN-1995\n"
        "TCS,Tata Consultancy Services,BE,01-JAN-2004\n"
        "DEAD,Dead Co,BZ,01-JAN-2000\n"
        "M&M,Mahindra & Mahindra,EQ,01-JAN-1995\n"
        "BAJAJ-AUTO,Bajaj Auto,EQ,01-JAN-2008\n"
        ",blank name,EQ,01-JAN-2000\n"
        "RELIANCE,dup,EQ,01-JAN-1995\n"
    ).encode("utf-8")
    with patch("src.listings.download_bytes", return_value=payload) as mock_dl:
        raw = build_nse_listing_csv(url="https://example.com/equity.csv")
    mock_dl.assert_called_once_with(
        "https://example.com/equity.csv",
        headers=mock_dl.call_args.kwargs["headers"],
    )
    assert "Referer" in mock_dl.call_args.kwargs["headers"]
    text = raw.decode("utf-8")
    assert text.startswith("Symbol,Name,Series\n")
    assert "RELIANCE.NS" in text
    assert "TCS.NS" in text
    assert "M&M.NS" in text
    assert "BAJAJ-AUTO.NS" in text
    assert "DEAD.NS" not in text
    assert text.count("RELIANCE.NS") == 1
    assert ",EQ," in text or text.endswith(",EQ\n") or ",EQ\n" in text
    assert ",BE," in text or ",BE\n" in text
    assert text.index("BAJAJ-AUTO.NS") < text.index("RELIANCE.NS")


def test_build_nse_listing_csv_empty_raises() -> None:
    payload = b"SYMBOL,NAME OF COMPANY,SERIES\nDEAD,Dead,BZ\n"
    with patch("src.listings.download_bytes", return_value=payload):
        with pytest.raises(RuntimeError, match="Empty NSE listing"):
            build_nse_listing_csv()


def test_build_nse_listing_csv_missing_column() -> None:
    with patch("src.listings.download_bytes", return_value=b"SYMBOL\nRELIANCE\n"):
        with pytest.raises(RuntimeError, match="SYMBOL/SERIES"):
            build_nse_listing_csv()


def test_build_cn_listing_csv() -> None:
    sse = pd.DataFrame(
        {
            "Symbol": ["601288", "519", "600519.0", "ABC"],
            "Name": ["AgBank", "Tiny", "Moutai", "Nope"],
            "Industry": ["Bank", "X", "Drinks", "Y"],
        }
    )
    szse = pd.DataFrame({"Code": ["300750"], "Label": ["CATL"]})

    def fake_listing(market):
        if market == "SSE":
            return sse
        if market == "SZSE":
            return szse
        return pd.DataFrame()

    fake_fdr = MagicMock(StockListing=fake_listing)
    with patch.dict("sys.modules", {"FinanceDataReader": fake_fdr}):
        raw = build_cn_listing_csv()
    text = raw.decode("utf-8")
    assert "601288.SS" in text
    assert "000519.SS" in text
    assert "600519.SS" in text
    assert "300750.SZ" in text
    assert "ABC" not in text
    assert "SSE" in text
    assert "SZSE" in text
    assert "Industry" in text
    assert text.index("000519.SS") < text.index("300750.SZ") < text.index("601288.SS")


def test_build_cn_listing_csv_empty_raises() -> None:
    fake_fdr = MagicMock(StockListing=lambda _m: pd.DataFrame())
    with patch.dict("sys.modules", {"FinanceDataReader": fake_fdr}):
        with pytest.raises(RuntimeError, match="Empty China"):
            build_cn_listing_csv()


def test_build_cn_listing_csv_no_numeric_codes_raises() -> None:
    fake_fdr = MagicMock(
        StockListing=lambda _m: pd.DataFrame({"Symbol": ["ABC"], "Name": ["Nope"]})
    )
    with patch.dict("sys.modules", {"FinanceDataReader": fake_fdr}):
        with pytest.raises(RuntimeError, match="Empty China"):
            build_cn_listing_csv()


def test_build_plain_ticker_listing_csv() -> None:
    payload = b"AAPL\naap\n# comment\nNYSE:BRK.B\nCASH\nUSD\nXTSLA\nP5N994\n1BAD\nAAPL\n"
    with patch("src.listings.download_bytes", return_value=payload) as mock_dl:
        raw = build_plain_ticker_listing_csv(url="https://example.com/mdy.txt")
    mock_dl.assert_called_once_with("https://example.com/mdy.txt")
    text = raw.decode("utf-8")
    assert text.startswith("Symbol\n")
    assert "AAPL" in text
    assert "BRK.B" in text
    assert "CASH" not in text
    assert "USD" not in text
    assert "XTSLA" not in text
    assert "P5N994" not in text
    assert "1BAD" not in text
    # Deduped + sorted
    assert text.count("AAPL") == 1
    assert text.index("AAPL") < text.index("BRK.B")


def test_build_russell2000_listing_csv_alias() -> None:
    with patch(
        "src.listings.build_plain_ticker_listing_csv",
        return_value=b"Symbol\nAAPL\n",
    ) as mock_build:
        raw = build_russell2000_listing_csv(url="https://example.com/iwm.txt")
    mock_build.assert_called_once_with(url="https://example.com/iwm.txt")
    assert raw == b"Symbol\nAAPL\n"


def test_build_plain_ticker_listing_csv_empty_raises() -> None:
    with patch("src.listings.download_bytes", return_value=b"CASH\nUSD\n"):
        with pytest.raises(RuntimeError, match="Empty plain-ticker listing"):
            build_plain_ticker_listing_csv(url="https://example.com/empty.txt")


def test_refresh_listings_url_create_and_unchanged(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    listing_rel = "listings/nasdaq.csv"
    (tmp_path / "listings").mkdir()
    config.write_text(
        "listings:\n"
        "  stocks_us:\n"
        "    - path: listings/nasdaq.csv\n"
        "      url: https://example.com/nasdaq.csv\n",
        encoding="utf-8",
    )
    with patch("src.listings.download_bytes", return_value=b"Symbol\nAAPL\n"):
        summary = refresh_listings(config)
    assert summary == {"checked": 1, "updated": 1, "failed": 0}
    assert (tmp_path / listing_rel).read_bytes() == b"Symbol\nAAPL\n"

    with patch("src.listings.download_bytes", return_value=b"Symbol\nAAPL\n"):
        summary2 = refresh_listings(config)
    assert summary2 == {"checked": 1, "updated": 0, "failed": 0}


def test_refresh_listings_download_failure(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "listings:\n"
        "  stocks_us:\n"
        "    - path: listings/x.csv\n"
        "      url: https://example.com/x.csv\n",
        encoding="utf-8",
    )
    with patch(
        "src.listings.download_bytes",
        side_effect=urllib.error.URLError("down"),
    ):
        summary = refresh_listings(config)
    assert summary["failed"] == 1
    assert summary["updated"] == 0


def test_refresh_listings_empty_remote(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "listings:\n"
        "  stocks_us:\n"
        "    - path: listings/x.csv\n"
        "      url: https://example.com/x.csv\n",
        encoding="utf-8",
    )
    with patch("src.listings.download_bytes", return_value=b"   \n"):
        summary = refresh_listings(config)
    assert summary["failed"] == 1


def test_refresh_listings_krx_tse_europe_twse_plain_tickers(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    (tmp_path / "listings").mkdir()
    config.write_text(
        "listings:\n"
        "  stocks_kr:\n"
        "    - path: listings/krx.csv\n"
        "      source: krx\n"
        "  stocks_jp:\n"
        "    - path: listings/tse.csv\n"
        "      source: tse\n"
        "  stocks_eu:\n"
        "    - path: listings/eu.csv\n"
        "      source: europe\n"
        "  stocks_tw:\n"
        "    - path: listings/twse.csv\n"
        "      url: https://example.com/twse.json\n"
        "      source: twse\n"
        "  stocks_us:\n"
        "    - path: listings/sp400.csv\n"
        "      url: https://example.com/mdy.txt\n"
        "      source: plain_tickers\n"
        "    - path: listings/r2k.csv\n"
        "      url: https://example.com/iwm.txt\n"
        "      source: russell2000\n",
        encoding="utf-8",
    )
    with (
        patch("src.listings.build_krx_listing_csv", return_value=b"Symbol\nA.KS\n"),
        patch("src.listings.build_tse_listing_csv", return_value=b"Symbol\n1.T\n"),
        patch("src.listings.build_europe_listing_csv", return_value=b"Symbol\nX.DE\n"),
        patch(
            "src.listings.build_twse_listing_csv",
            return_value=b"Symbol\n2330.TW\n",
        ) as mock_twse,
        patch(
            "src.listings.build_plain_ticker_listing_csv",
            return_value=b"Symbol\nMDY\n",
        ) as mock_plain,
    ):
        summary = refresh_listings(config)
    assert summary == {"checked": 6, "updated": 6, "failed": 0}
    mock_twse.assert_called_once_with(url="https://example.com/twse.json")
    assert mock_plain.call_count == 2
    mock_plain.assert_any_call(url="https://example.com/mdy.txt")
    mock_plain.assert_any_call(url="https://example.com/iwm.txt")


def test_refresh_listings_plain_tickers_requires_url(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "listings:\n"
        "  stocks_us:\n"
        "    - path: listings/sp400.csv\n"
        "      source: plain_tickers\n",
        encoding="utf-8",
    )
    summary = refresh_listings(config)
    assert summary == {"checked": 1, "updated": 0, "failed": 1}


def test_refresh_listings_source_failures(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "listings:\n"
        "  stocks_kr:\n"
        "    - path: listings/krx.csv\n"
        "      source: krx\n"
        "  stocks_jp:\n"
        "    - path: listings/tse.csv\n"
        "      source: tse\n"
        "  stocks_eu:\n"
        "    - path: listings/eu.csv\n"
        "      source: europe\n"
        "  stocks_tw:\n"
        "    - path: listings/twse.csv\n"
        "      source: twse\n"
        "  stocks_us:\n"
        "    - path: listings/r2k.csv\n"
        "      source: russell2000\n",
        encoding="utf-8",
    )
    with (
        patch("src.listings.build_krx_listing_csv", side_effect=RuntimeError("krx")),
        patch("src.listings.build_tse_listing_csv", side_effect=RuntimeError("tse")),
        patch("src.listings.build_europe_listing_csv", side_effect=RuntimeError("eu")),
        patch("src.listings.build_twse_listing_csv", side_effect=RuntimeError("twse")),
        patch(
            "src.listings.build_plain_ticker_listing_csv",
            side_effect=RuntimeError("r2k"),
        ),
    ):
        summary = refresh_listings(config)
    assert summary == {"checked": 5, "updated": 0, "failed": 5}


def test_refresh_listings_tsx_asx_nse_china(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "listings:\n"
        "  stocks_ca:\n"
        "    - path: listings/tsx.csv\n"
        "      source: tsx\n"
        "  stocks_au:\n"
        "    - path: listings/asx.csv\n"
        "      url: https://example.com/asx.csv\n"
        "      source: asx\n"
        "  stocks_in:\n"
        "    - path: listings/nse.csv\n"
        "      url: https://example.com/equity.csv\n"
        "      source: nse\n"
        "  stocks_cn:\n"
        "    - path: listings/cn.csv\n"
        "      source: china\n",
        encoding="utf-8",
    )
    with (
        patch("src.listings.build_tsx_listing_csv", return_value=b"Symbol\nSHOP.TO\n") as mock_tsx,
        patch("src.listings.build_asx_listing_csv", return_value=b"Symbol\nBHP.AX\n") as mock_asx,
        patch("src.listings.build_nse_listing_csv", return_value=b"Symbol\nRELIANCE.NS\n") as mock_nse,
        patch("src.listings.build_cn_listing_csv", return_value=b"Symbol\n601288.SS\n") as mock_cn,
    ):
        summary = refresh_listings(config)
    assert summary == {"checked": 4, "updated": 4, "failed": 0}
    mock_tsx.assert_called_once_with()
    mock_asx.assert_called_once_with(url="https://example.com/asx.csv")
    mock_nse.assert_called_once_with(url="https://example.com/equity.csv")
    mock_cn.assert_called_once_with()
    assert (tmp_path / "listings" / "tsx.csv").read_bytes() == b"Symbol\nSHOP.TO\n"


def test_refresh_listings_tsx_asx_nse_china_failures(tmp_path: Path) -> None:
    config = tmp_path / "tickers.yaml"
    config.write_text(
        "listings:\n"
        "  stocks_ca:\n"
        "    - path: listings/tsx.csv\n"
        "      source: tsx\n"
        "  stocks_au:\n"
        "    - path: listings/asx.csv\n"
        "      source: asx\n"
        "  stocks_in:\n"
        "    - path: listings/nse.csv\n"
        "      source: nse\n"
        "  stocks_cn:\n"
        "    - path: listings/cn.csv\n"
        "      source: china\n",
        encoding="utf-8",
    )
    with (
        patch("src.listings.build_tsx_listing_csv", side_effect=RuntimeError("tsx")),
        patch("src.listings.build_asx_listing_csv", side_effect=RuntimeError("asx")) as mock_asx,
        patch("src.listings.build_nse_listing_csv", side_effect=RuntimeError("nse")) as mock_nse,
        patch("src.listings.build_cn_listing_csv", side_effect=RuntimeError("cn")),
    ):
        summary = refresh_listings(config)
    assert summary == {"checked": 4, "updated": 0, "failed": 4}
    mock_asx.assert_called_once_with(url=ASX_DIRECTORY_URL)
    mock_nse.assert_called_once_with(url=NSE_EQUITY_URL)
