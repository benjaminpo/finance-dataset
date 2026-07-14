"""Unit tests for src.listings."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import pandas as pd
import pytest

from src.listings import (
    EUROPE_SUFFIXES,
    _sha256,
    _write_if_changed,
    build_europe_listing_csv,
    build_krx_listing_csv,
    build_tse_listing_csv,
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


def test_refresh_listings_krx_tse_europe(tmp_path: Path) -> None:
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
        "      source: europe\n",
        encoding="utf-8",
    )
    with (
        patch("src.listings.build_krx_listing_csv", return_value=b"Symbol\nA.KS\n"),
        patch("src.listings.build_tse_listing_csv", return_value=b"Symbol\n1.T\n"),
        patch("src.listings.build_europe_listing_csv", return_value=b"Symbol\nX.DE\n"),
    ):
        summary = refresh_listings(config)
    assert summary == {"checked": 3, "updated": 3, "failed": 0}


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
        "      source: europe\n",
        encoding="utf-8",
    )
    with (
        patch("src.listings.build_krx_listing_csv", side_effect=RuntimeError("krx")),
        patch("src.listings.build_tse_listing_csv", side_effect=RuntimeError("tse")),
        patch("src.listings.build_europe_listing_csv", side_effect=RuntimeError("eu")),
    ):
        summary = refresh_listings(config)
    assert summary == {"checked": 3, "updated": 0, "failed": 3}
