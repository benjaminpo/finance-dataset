"""Unit tests for fetch summary reporting."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.summary import (
    MARKDOWN_FAILURE_LIMIT,
    failure_rate_pct,
    format_fetch_summary_markdown,
    write_fetch_summary,
)


def _sample_summary(**overrides):
    base = {
        "success": 8,
        "failed": 2,
        "skipped": 1,
        "total": 11,
        "attempted": 10,
        "failure_rate": 0.2,
        "by_interval": {
            "1d": {"success": 5, "failed": 1, "skipped": 0},
            "1wk": {"success": 3, "failed": 1, "skipped": 1},
        },
        "by_asset_class": {
            "crypto": {"success": 4, "failed": 0, "skipped": 0},
            "stocks_us": {"success": 4, "failed": 2, "skipped": 1},
        },
        "failures": [
            {
                "ticker": "BAD1",
                "asset_class": "stocks_us",
                "interval": "1d",
                "message": "No 1d data returned",
            },
            {
                "ticker": "BAD2",
                "asset_class": "stocks_us",
                "interval": "1wk",
                "message": "timeout",
            },
        ],
    }
    base.update(overrides)
    return base


def test_failure_rate_pct() -> None:
    assert failure_rate_pct({"failure_rate": 0.25}) == 25.0
    assert failure_rate_pct({"success": 3, "failed": 1}) == 25.0
    assert failure_rate_pct({"success": 0, "failed": 0}) == 0.0


def test_format_fetch_summary_markdown_includes_breakdowns() -> None:
    md = format_fetch_summary_markdown(_sample_summary())
    assert "## Fetch summary" in md
    assert "Failure rate | 20.00%" in md
    assert "### By interval" in md
    assert "`1d`" in md
    assert "### By asset class" in md
    assert "`BAD1`" in md
    assert "No 1d data returned" in md


def test_format_truncates_long_failure_list() -> None:
    failures = [
        {
            "ticker": f"T{i}",
            "asset_class": "crypto",
            "interval": "1d",
            "message": "blank",
        }
        for i in range(MARKDOWN_FAILURE_LIMIT + 25)
    ]
    md = format_fetch_summary_markdown(
        _sample_summary(failed=len(failures), failures=failures, failure_rate=1.0)
    )
    assert f"showing first {MARKDOWN_FAILURE_LIMIT}" in md
    assert "`T0`" in md
    assert f"`T{MARKDOWN_FAILURE_LIMIT}`" not in md


def test_write_fetch_summary_json_and_md(tmp_path: Path) -> None:
    out = tmp_path / "reports" / "fetch-summary.json"
    written = write_fetch_summary(_sample_summary(), out)
    assert written == out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["success"] == 8
    assert payload["failed"] == 2
    assert payload["failure_rate_pct"] == 20.0
    assert len(payload["failures"]) == 2
    assert "generated_at" in payload
    md_path = out.with_suffix(".md")
    assert md_path.is_file()
    assert "Fetch summary" in md_path.read_text(encoding="utf-8")


def test_write_fetch_summary_appends_github_step_summary(
    tmp_path: Path, monkeypatch
) -> None:
    step = tmp_path / "step_summary.md"
    step.write_text("# prior\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step))
    write_fetch_summary(_sample_summary(failures=[]), tmp_path / "fetch-summary.json")
    text = step.read_text(encoding="utf-8")
    assert text.startswith("# prior\n")
    assert "## Fetch summary" in text
    assert "_None._" in text
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert "GITHUB_STEP_SUMMARY" not in os.environ
