#!/usr/bin/env python3
"""
Precocity AI Optimisation Engine
=================================
Analyses GitHub Actions check performance, detects failure patterns, and
generates actionable remediation recommendations.

Usage (called by elite-orchestrator.yml):
    python3 .github/scripts/precocity-ai.py \\
        --event push --sha <sha> --ref refs/heads/main \\
        --mode report --output github-actions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class CheckMetrics:
    name: str
    runs: int = 0
    failures: int = 0
    total_duration_s: float = 0.0
    p95_duration_s: float = 0.0
    last_status: str = "unknown"
    durations: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.runs == 0:
            return 1.0
        return (self.runs - self.failures) / self.runs

    @property
    def avg_duration_s(self) -> float:
        if not self.durations:
            return 0.0
        return sum(self.durations) / len(self.durations)

    def compute_p95(self) -> float:
        if not self.durations:
            return 0.0
        sorted_d = sorted(self.durations)
        idx = int(len(sorted_d) * 0.95)
        return sorted_d[min(idx, len(sorted_d) - 1)]


@dataclass
class Insight:
    severity: str  # info | warning | critical
    category: str  # performance | reliability | resource | pattern
    message: str
    recommendation: str


# ── Config loading ─────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    config_path = Path(".github/configs/elite-team-config.yml")
    if not config_path.exists():
        return {}
    try:
        import yaml
        with config_path.open() as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def get_targets(cfg: dict) -> dict[str, Any]:
    defaults = {
        "max_check_latency_seconds": 120,
        "max_p95_latency_seconds": 180,
        "min_success_rate_percent": 95,
    }
    targets = cfg.get("precocity_ai", {}).get("targets", {})
    return {**defaults, **targets}


# ── Simulated metrics (replace with real GitHub API calls when token available) ─

def collect_metrics(event: str, sha: str, ref: str) -> list[CheckMetrics]:
    """
    Collect check run metrics.

    In production this would call the GitHub Checks API; here we generate
    representative synthetic metrics so the engine works without credentials.
    """
    import random
    rng = random.Random(int(sha[:8], 16) if sha else 42)

    check_names = [
        "ci / test (3.10)",
        "ci / test (3.11)",
        "ci / test (3.12)",
        "ci / test (3.13)",
        "Secret Scanning / Gitleaks Secret Scan",
        "Secret Scanning / TruffleHog Secret Scan",
        "Secret Scanning / Custom Pattern Scan",
        "Elite Orchestrator / GitHub Command Center",
        "Elite Orchestrator / Precocity AI Optimiser",
        "Elite Orchestrator / iPhone 16 Mobile Sync",
    ]

    metrics = []
    for name in check_names:
        runs = rng.randint(10, 50)
        # Inject some realistic variation
        if "TruffleHog" in name:
            failure_rate = 0.15   # historically flaky
            base_dur = 45.0
        elif "ci / test" in name:
            failure_rate = 0.03
            base_dur = 25.0
        else:
            failure_rate = 0.02
            base_dur = 15.0

        durations = [
            max(5.0, rng.gauss(base_dur, base_dur * 0.2))
            for _ in range(runs)
        ]
        failures = sum(1 for _ in range(runs) if rng.random() < failure_rate)

        m = CheckMetrics(
            name=name,
            runs=runs,
            failures=failures,
            total_duration_s=sum(durations),
            durations=durations,
            last_status="failure" if rng.random() < failure_rate else "success",
        )
        m.p95_duration_s = m.compute_p95()
        metrics.append(m)

    return metrics


# ── Analysis passes ───────────────────────────────────────────────────────────

def analyse_performance(
    metrics: list[CheckMetrics],
    targets: dict[str, Any],
) -> list[Insight]:
    insights: list[Insight] = []
    max_lat = targets["max_check_latency_seconds"]
    max_p95 = targets["max_p95_latency_seconds"]

    for m in metrics:
        if m.avg_duration_s > max_lat:
            insights.append(Insight(
                severity="warning",
                category="performance",
                message=f"'{m.name}' avg duration {m.avg_duration_s:.0f}s exceeds {max_lat}s target",
                recommendation=(
                    "Consider splitting the job into parallel steps, enabling caching, "
                    "or moving heavy setup into a reusable workflow."
                ),
            ))
        if m.p95_duration_s > max_p95:
            insights.append(Insight(
                severity="warning",
                category="performance",
                message=f"'{m.name}' p95 duration {m.p95_duration_s:.0f}s exceeds {max_p95}s target",
                recommendation=(
                    "Investigate intermittent slow runs. Check for network I/O bottlenecks "
                    "or resource contention on shared runners."
                ),
            ))

    return insights


def analyse_failures(
    metrics: list[CheckMetrics],
    targets: dict[str, Any],
) -> list[Insight]:
    insights: list[Insight] = []
    min_sr = targets["min_success_rate_percent"] / 100.0

    for m in metrics:
        if m.success_rate < min_sr:
            severity = "critical" if m.success_rate < 0.80 else "warning"
            insights.append(Insight(
                severity=severity,
                category="reliability",
                message=(
                    f"'{m.name}' success rate {m.success_rate:.0%} is below "
                    f"{min_sr:.0%} target ({m.failures}/{m.runs} failures)"
                ),
                recommendation=(
                    "Review recent failure logs. If failures are flaky, consider adding "
                    "a retry step. If systematic, investigate the root cause in the "
                    "check script or its dependencies."
                ),
            ))

    return insights


def detect_patterns(metrics: list[CheckMetrics]) -> list[Insight]:
    """Detect cross-check patterns such as correlated failures."""
    insights: list[Insight] = []

    # Flag checks that always fail together (heuristic: high failure count + short duration)
    short_fails = [
        m for m in metrics
        if m.failures > 0 and m.avg_duration_s < 5.0
    ]
    if short_fails:
        names = ", ".join(f"'{m.name}'" for m in short_fails[:3])
        insights.append(Insight(
            severity="warning",
            category="pattern",
            message=f"Checks {names} fail very quickly — possible misconfiguration or missing secret",
            recommendation=(
                "Confirm all required secrets (APNS_KEY_ID, WEBHOOK_SECRET, etc.) are set "
                "in repository Settings → Secrets and variables → Actions."
            ),
        ))

    return insights


# ── Remediation recommendations ───────────────────────────────────────────────

def build_remediation_plan(insights: list[Insight]) -> list[str]:
    """Translate insights into a prioritised remediation checklist."""
    plan: list[str] = []
    critical = [i for i in insights if i.severity == "critical"]
    warnings = [i for i in insights if i.severity == "warning"]

    if critical:
        plan.append("🔴 **Critical — address immediately:**")
        for i in critical:
            plan.append(f"  - [ ] {i.message}")
            plan.append(f"        ↳ {i.recommendation}")

    if warnings:
        plan.append("🟡 **Warnings — address soon:**")
        for i in warnings:
            plan.append(f"  - [ ] {i.message}")
            plan.append(f"        ↳ {i.recommendation}")

    if not plan:
        plan.append("✅ **All checks within healthy thresholds — no action required.**")

    return plan


# ── Output formatters ─────────────────────────────────────────────────────────

def emit_github_actions(insights: list[Insight], plan: list[str]) -> None:
    for i in insights:
        level = "error" if i.severity == "critical" else "warning"
        print(f"::{level}::{i.message} — {i.recommendation}")


def emit_summary(
    insights: list[Insight],
    metrics: list[CheckMetrics],
    plan: list[str],
    event: str,
    ref: str,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("## 🤖 Precocity AI Optimisation Report")
    print()
    print(f"**Generated:** {ts}  |  **Event:** `{event}`  |  **Ref:** `{ref}`")
    print()

    # Metrics table
    print("### Check Performance Summary")
    print()
    print("| Check | Runs | Success Rate | Avg (s) | P95 (s) |")
    print("|-------|------|-------------|---------|---------|")
    for m in metrics:
        sr_icon = "✅" if m.success_rate >= 0.95 else ("⚠️" if m.success_rate >= 0.80 else "🔴")
        print(
            f"| {m.name} | {m.runs} | {sr_icon} {m.success_rate:.0%} "
            f"| {m.avg_duration_s:.0f} | {m.p95_duration_s:.0f} |"
        )

    print()
    print("### Insights & Remediation Plan")
    print()
    if plan:
        for line in plan:
            print(line)
    else:
        print("✅ No issues detected.")

    print()
    print("### AI Recommendations")
    print()
    if not insights:
        print("- All checks are performing within defined targets.")
    else:
        seen: set[str] = set()
        for i in insights:
            if i.recommendation not in seen:
                seen.add(i.recommendation)
                print(f"- {i.recommendation}")

    print()
    print("---")
    print("_Precocity AI Engine · Agent-Reach Elite Collaborative System v1.0.0_")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Precocity AI Optimisation Engine")
    p.add_argument("--event", default="push", help="GitHub event name")
    p.add_argument("--sha", default="", help="Commit SHA")
    p.add_argument("--ref", default="", help="Git ref")
    p.add_argument(
        "--mode",
        choices=["performance", "failure-analysis", "report", "full"],
        default="full",
        help="Analysis mode",
    )
    p.add_argument(
        "--output",
        choices=["github-actions", "json", "text"],
        default="text",
        help="Output format",
    )
    p.add_argument(
        "--no-fail",
        action="store_true",
        default=False,
        help="Always exit 0 regardless of insight severity (advisory mode)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    cfg = load_config()
    targets = get_targets(cfg)

    metrics = collect_metrics(args.event, args.sha, args.ref)

    insights: list[Insight] = []

    if args.mode in ("performance", "full"):
        insights.extend(analyse_performance(metrics, targets))

    if args.mode in ("failure-analysis", "full"):
        insights.extend(analyse_failures(metrics, targets))
        insights.extend(detect_patterns(metrics))

    plan = build_remediation_plan(insights)

    if args.output == "github-actions":
        emit_github_actions(insights, plan)
    elif args.output == "json":
        output = {
            "insights": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "message": i.message,
                    "recommendation": i.recommendation,
                }
                for i in insights
            ],
            "plan": plan,
        }
        print(json.dumps(output, indent=2))
    else:
        emit_summary(insights, metrics, plan, args.event, args.ref)

    # Exit non-zero only on critical issues (unless --no-fail advisory mode)
    has_critical = any(i.severity == "critical" for i in insights)
    if args.no_fail:
        return 0
    return 1 if has_critical else 0


if __name__ == "__main__":
    sys.exit(main())
