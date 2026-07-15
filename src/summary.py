"""Fetch-run summary report helpers (JSON + Markdown for CI artifacts)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Cap the failure table in Markdown so the GitHub step summary stays readable.
MARKDOWN_FAILURE_LIMIT = 100


def failure_rate_pct(summary: dict[str, Any]) -> float:
    """Return failure rate as a percentage of attempted (non-skipped) jobs."""
    rate = summary.get("failure_rate")
    if rate is not None:
        return 100.0 * float(rate)
    attempted = int(summary.get("attempted", 0))
    if attempted <= 0:
        attempted = int(summary.get("success", 0)) + int(summary.get("failed", 0))
    if attempted <= 0:
        return 0.0
    return 100.0 * int(summary.get("failed", 0)) / attempted


def format_fetch_summary_markdown(summary: dict[str, Any]) -> str:
    """Build a Markdown report for logs / GitHub Actions step summary."""
    success = int(summary.get("success", 0))
    failed = int(summary.get("failed", 0))
    skipped = int(summary.get("skipped", 0))
    total = int(summary.get("total", success + failed + skipped))
    attempted = int(summary.get("attempted", success + failed))
    rate = failure_rate_pct(summary)
    failures = list(summary.get("failures") or [])

    lines = [
        "## Fetch summary",
        "",
        f"| Metric | Value |",
        f"| --- | --- |",
        f"| Total jobs | {total} |",
        f"| Success | {success} |",
        f"| Failed | {failed} |",
        f"| Skipped | {skipped} |",
        f"| Attempted (excl. skipped) | {attempted} |",
        f"| Failure rate | {rate:.2f}% |",
        "",
    ]

    by_interval = summary.get("by_interval") or {}
    if by_interval:
        lines.extend(
            [
                "### By interval",
                "",
                "| Interval | Success | Failed | Skipped | Failure rate |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for interval in sorted(by_interval):
            bucket = by_interval[interval]
            s = int(bucket.get("success", 0))
            f = int(bucket.get("failed", 0))
            k = int(bucket.get("skipped", 0))
            att = s + f
            irate = (100.0 * f / att) if att else 0.0
            lines.append(f"| `{interval}` | {s} | {f} | {k} | {irate:.2f}% |")
        lines.append("")

    by_asset = summary.get("by_asset_class") or {}
    if by_asset:
        lines.extend(
            [
                "### By asset class",
                "",
                "| Asset class | Success | Failed | Skipped | Failure rate |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for asset_class in sorted(by_asset):
            bucket = by_asset[asset_class]
            s = int(bucket.get("success", 0))
            f = int(bucket.get("failed", 0))
            k = int(bucket.get("skipped", 0))
            att = s + f
            arate = (100.0 * f / att) if att else 0.0
            lines.append(f"| `{asset_class}` | {s} | {f} | {k} | {arate:.2f}% |")
        lines.append("")

    if failures:
        shown = failures[:MARKDOWN_FAILURE_LIMIT]
        lines.extend(
            [
                f"### Failures ({len(failures)} total"
                + (
                    f", showing first {len(shown)}"
                    if len(failures) > MARKDOWN_FAILURE_LIMIT
                    else ""
                )
                + ")",
                "",
                "| Ticker | Class | Interval | Message |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in shown:
            ticker = str(item.get("ticker", "")).replace("|", "\\|")
            asset_class = str(item.get("asset_class", "")).replace("|", "\\|")
            interval = str(item.get("interval", "")).replace("|", "\\|")
            message = str(item.get("message", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| `{ticker}` | `{asset_class}` | `{interval}` | {message} |"
            )
        lines.append("")
    else:
        lines.extend(["### Failures", "", "_None._", ""])

    return "\n".join(lines)


def write_fetch_summary(summary: dict[str, Any], path: Path) -> Path:
    """
    Write ``path`` as JSON and a sibling ``.md`` file.

    When running under GitHub Actions, also append the Markdown to
    ``$GITHUB_STEP_SUMMARY`` so it appears on the workflow run page.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "success": int(summary.get("success", 0)),
        "failed": int(summary.get("failed", 0)),
        "skipped": int(summary.get("skipped", 0)),
        "total": int(
            summary.get(
                "total",
                int(summary.get("success", 0))
                + int(summary.get("failed", 0))
                + int(summary.get("skipped", 0)),
            )
        ),
        "attempted": int(
            summary.get(
                "attempted",
                int(summary.get("success", 0)) + int(summary.get("failed", 0)),
            )
        ),
        "failure_rate": float(summary.get("failure_rate", 0.0)),
        "failure_rate_pct": round(failure_rate_pct(summary), 4),
        "by_interval": summary.get("by_interval") or {},
        "by_asset_class": summary.get("by_asset_class") or {},
        "failures": list(summary.get("failures") or []),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    md = format_fetch_summary_markdown(payload)
    md_path = path.with_suffix(".md")
    md_path.write_text(md if md.endswith("\n") else md + "\n", encoding="utf-8")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(md if md.endswith("\n") else md + "\n")

    return path
