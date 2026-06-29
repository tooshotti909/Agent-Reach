#!/usr/bin/env python3
"""
performance-tracker.py – Analyse Agent-Reach workflow run metrics.

Responsibilities
----------------
* Pull the last N workflow runs from the GitHub API.
* Compute latency percentiles per workflow.
* Detect trends (improving / degrading / stable).
* Identify the slowest individual runs.
* Write a Markdown report to GITHUB_STEP_SUMMARY and stdout.

Usage
-----
    python performance-tracker.py [--lookback-days DAYS] [--depth N]

Environment variables
---------------------
    GITHUB_TOKEN   – required for GitHub API access
    GITHUB_STEP_SUMMARY – path written by the Actions runner (optional)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Config helpers (reuse watchdog config where possible)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent.parent / "configs" / "watchdog-config.yml"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if path.exists():
        with path.open() as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _cfg(config: dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = config
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
        if node is default:
            return default
    return node


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _github_get(path: str, *, token: str, params: dict[str, str] | None = None) -> Any:
    base = "https://api.github.com"
    url = f"{base}{path}"
    if params:
        query = "&".join(
            f"{k}={urllib.request.quote(str(v))}" for k, v in params.items()
        )
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-reach-perf-tracker/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def fetch_runs(
    repo: str,
    *,
    token: str,
    since: datetime,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Return all workflow runs since *since* (may issue multiple pages)."""
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    all_runs: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            data = _github_get(
                f"/repos/{repo}/actions/runs",
                token=token,
                params={
                    "per_page": str(per_page),
                    "page": str(page),
                    "created": f">={since_str}",
                },
            )
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"::warning::GitHub API error on page {page}: {exc}")
            break

        runs = data.get("workflow_runs", [])
        if not runs:
            break
        all_runs.extend(runs)
        if len(runs) < per_page:
            break
        page += 1
        time.sleep(0.3)   # stay well under rate limits

    return all_runs


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[float], p: float) -> float:
    """Return the p-th percentile (0–100) of a pre-sorted list."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    rank = (p / 100) * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    frac = rank - lower
    return sorted_values[lower] * (1 - frac) + sorted_values[upper] * frac


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _trend(durations_oldest_first: list[float]) -> str:
    """Return 'improving', 'degrading', or 'stable' based on a simple linear fit."""
    n = len(durations_oldest_first)
    if n < 4:
        return "stable"
    xs = list(range(n))
    mx = _mean(xs)
    my = _mean(durations_oldest_first)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, durations_oldest_first))
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = num / den
    # Normalise slope as fraction of mean duration
    rel = slope / (my or 1.0)
    if rel < -0.05:
        return "📈 improving"
    if rel > 0.05:
        return "📉 degrading"
    return "→ stable"


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _run_duration(run: dict[str, Any]) -> float | None:
    started = _parse_iso(run.get("run_started_at") or run.get("created_at"))
    updated = _parse_iso(run.get("updated_at"))
    if started and updated:
        return max(0.0, (updated - started).total_seconds())
    return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyse(
    runs: list[dict[str, Any]],
    percentiles: list[int],
    history_depth: int,
) -> dict[str, Any]:
    """Group runs by workflow and compute per-workflow stats."""
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        name = run.get("name") or "unknown"
        by_workflow.setdefault(name, []).append(run)

    results: dict[str, Any] = {}

    for wf_name, wf_runs in sorted(by_workflow.items()):
        # Sort oldest → newest for trend analysis
        wf_runs_sorted = sorted(
            wf_runs,
            key=lambda r: r.get("run_started_at") or r.get("created_at") or "",
        )
        recent = wf_runs_sorted[-history_depth:]

        durations = [d for r in recent if (d := _run_duration(r)) is not None]
        s_dur = sorted(durations)

        total = len(wf_runs)
        completed = [r for r in wf_runs if r.get("status") == "completed"]
        failed = [
            r for r in completed
            if r.get("conclusion") in ("failure", "timed_out", "startup_failure")
        ]
        success_rate = (len(completed) - len(failed)) / len(completed) if completed else None

        pct_values = {f"p{p}": _percentile(s_dur, p) for p in percentiles}

        # Find the single slowest run
        slowest: dict[str, Any] | None = None
        slowest_dur: float = 0.0
        for run in recent:
            d = _run_duration(run)
            if d is not None and d > slowest_dur:
                slowest_dur = d
                slowest = run

        results[wf_name] = {
            "total_runs": total,
            "completed": len(completed),
            "failed": len(failed),
            "success_rate": success_rate,
            "durations": durations,
            "percentiles": pct_values,
            "mean_seconds": _mean(durations),
            "trend": _trend(durations),
            "slowest_run": slowest,
            "slowest_seconds": slowest_dur,
        }

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _fmt(seconds: float) -> str:
    """Human-friendly duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def build_report(
    stats: dict[str, Any],
    percentiles: list[int],
    since: datetime,
    repo: str,
) -> list[str]:
    lines = [
        "## 📊 CI Performance Report",
        "",
        f"**Repository:** `{repo}`  ",
        f"**Window:** since {since.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    if not stats:
        lines.append("_No workflow runs found in the analysis window._")
        return lines

    pct_headers = " | ".join(f"p{p}" for p in percentiles)
    lines += [
        f"| Workflow | Runs | Success | Mean | {pct_headers} | Trend |",
        f"|----------|------|---------|------|{'|'.join('---' for _ in percentiles)}|-------|",
    ]

    for wf_name, s in stats.items():
        sr = f"{s['success_rate']:.0%}" if s["success_rate"] is not None else "—"
        pcts = " | ".join(_fmt(s["percentiles"].get(f"p{p}", 0)) for p in percentiles)
        lines.append(
            f"| {wf_name} | {s['total_runs']} | {sr} | "
            f"{_fmt(s['mean_seconds'])} | {pcts} | {s['trend']} |"
        )

    lines.append("")

    # Slowest runs
    slowest_entries = [
        (wf, s["slowest_run"], s["slowest_seconds"])
        for wf, s in stats.items()
        if s["slowest_run"]
    ]
    slowest_entries.sort(key=lambda x: x[2], reverse=True)
    if slowest_entries:
        lines += ["### 🐢 Top Slowest Runs", ""]
        for wf, run, dur in slowest_entries[:5]:
            url = run.get("html_url", "")
            num = run.get("run_number", "?")
            link = f"[#{num}]({url})" if url else f"#{num}"
            lines.append(f"- **{wf}** – run {link} took {_fmt(dur)}")
        lines.append("")

    return lines


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent-Reach CI performance tracker")
    parser.add_argument(
        "--lookback-days",
        type=float,
        default=7.0,
        help="How many days back to look for runs (default: 7)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=50,
        help="Max recent runs per workflow for trend analysis (default: 50)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("::error::GITHUB_TOKEN environment variable is not set")
        return 2

    repo = _cfg(config, "watchdog", "repository", default="tooshotti909/Agent-Reach")
    percentiles: list[int] = _cfg(
        config, "performance", "latency_percentiles", default=[50, 90, 95, 99]
    )
    depth = int(
        _cfg(config, "performance", "history_depth", default=args.depth)
    )

    since = datetime.now(tz=timezone.utc) - timedelta(days=args.lookback_days)
    print(f"::notice::Performance tracker – repo={repo}, lookback={args.lookback_days}d")

    runs = fetch_runs(repo, token=token, since=since)
    if not runs:
        print("::warning::No workflow runs found – nothing to analyse")
        return 0

    stats = analyse(runs, percentiles, depth)
    report = build_report(stats, percentiles, since, repo)

    print("\n".join(report))
    write_summary(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
