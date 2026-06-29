#!/usr/bin/env python3
"""
check-watchdog.py – CI/CD watchdog daemon for Agent-Reach.

Responsibilities
----------------
* Fetch recent workflow runs from the GitHub API.
* Detect slow, hanging, or repeatedly-failing checks.
* Implement a simple in-process circuit-breaker.
* Retry GitHub API calls with exponential back-off.
* Emit structured warnings / errors to GitHub Actions annotations
  and (optionally) to GITHUB_STEP_SUMMARY.

Exit codes
----------
0 – healthy (or only warnings, depending on config ``alerts.fail_on_alert``)
1 – one or more ERROR-severity alerts found and ``fail_on_alert`` is true
2 – unexpected runtime error
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # PyYAML is already a transitive dep via several packages


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent.parent / "configs" / "watchdog-config.yml"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load YAML config, falling back to safe defaults if the file is absent."""
    if path.exists():
        with path.open() as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _cfg(config: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Traverse nested dict with dotted key path and return default if missing."""
    node: Any = config
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
        if node is default:
            return default
    return node


# ---------------------------------------------------------------------------
# GitHub API client with retry / back-off
# ---------------------------------------------------------------------------

class GitHubAPIError(Exception):
    """Raised when the GitHub API returns a non-2xx status."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"GitHub API error {status}: {body[:200]}")


def _github_request(
    path: str,
    *,
    token: str,
    method: str = "GET",
    params: dict[str, str] | None = None,
) -> Any:
    """Make a single authenticated GitHub REST API request."""
    base = "https://api.github.com"
    url = f"{base}{path}"
    if params:
        query = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{query}"

    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-reach-watchdog/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise GitHubAPIError(exc.code, exc.read().decode(errors="replace")) from exc


def github_get_with_retry(
    path: str,
    *,
    token: str,
    params: dict[str, str] | None = None,
    max_attempts: int = 3,
    initial_delay: float = 10.0,
    backoff: float = 2.0,
    max_delay: float = 60.0,
    sleeper: Any = time.sleep,
) -> Any:
    """GET the GitHub API with exponential back-off retry."""
    delay = initial_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _github_request(path, token=token, params=params)
        except GitHubAPIError as exc:
            # 4xx (except 429) are not transient — don't retry
            if 400 <= exc.status < 500 and exc.status != 429:
                raise
            last_exc = exc
        except (OSError, TimeoutError) as exc:
            last_exc = exc

        if attempt < max_attempts:
            _warn(f"GitHub API attempt {attempt} failed ({last_exc}); retrying in {delay:.0f}s")
            sleeper(delay)
            delay = min(delay * backoff, max_delay)

    raise RuntimeError(f"GitHub API failed after {max_attempts} attempts") from last_exc


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """Simple three-state circuit breaker (CLOSED → OPEN → HALF-OPEN → CLOSED)."""

    failure_threshold: int = 5
    recovery_timeout: float = 300.0
    success_threshold: int = 2

    _state: str = field(default="CLOSED", init=False, repr=False)
    _failures: int = field(default=0, init=False, repr=False)
    _successes: int = field(default=0, init=False, repr=False)
    _opened_at: float = field(default=0.0, init=False, repr=False)

    @property
    def state(self) -> str:
        if self._state == "OPEN":
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = "HALF-OPEN"
                self._successes = 0
        return self._state

    def record_success(self) -> None:
        if self.state == "HALF-OPEN":
            self._successes += 1
            if self._successes >= self.success_threshold:
                self._state = "CLOSED"
                self._failures = 0
        else:
            self._failures = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self.state in ("CLOSED", "HALF-OPEN") and self._failures >= self.failure_threshold:
            self._state = "OPEN"
            self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        return self.state != "OPEN"


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    severity: str   # "info" | "warning" | "error"
    workflow: str
    message: str
    details: str = ""

    def emit(self) -> None:
        """Print as a GitHub Actions annotation."""
        annotation = self.severity if self.severity != "info" else "notice"
        detail = f" – {self.details}" if self.details else ""
        print(f"::{annotation}::[{self.workflow}] {self.message}{detail}")


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

_SEV_ORDER = {"info": 0, "warning": 1, "error": 2}

SEVERITY_MIN_DEFAULT = "warning"


def _sev_ge(a: str, b: str) -> bool:
    return _SEV_ORDER.get(a, 0) >= _SEV_ORDER.get(b, 0)


# ---------------------------------------------------------------------------
# GitHub Actions logging helpers
# ---------------------------------------------------------------------------

def _warn(msg: str) -> None:
    print(f"::warning::{msg}")


def _notice(msg: str) -> None:
    print(f"::notice::{msg}")


def _step_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    # Python <3.11 doesn't handle trailing 'Z'; normalise it.
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def _run_duration(run: dict[str, Any]) -> float | None:
    """Return run wall-clock seconds, or None if the run hasn't finished."""
    started = _parse_iso(run.get("run_started_at") or run.get("created_at"))
    updated = _parse_iso(run.get("updated_at"))
    if not started or not updated:
        return None
    return max(0.0, (updated - started).total_seconds())


# ---------------------------------------------------------------------------
# Core watchdog logic
# ---------------------------------------------------------------------------

def fetch_recent_runs(
    repo: str,
    *,
    token: str,
    lookback_hours: float = 2.0,
    breaker: CircuitBreaker,
    retry_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return workflow runs from the last ``lookback_hours``."""
    if not breaker.allow_request():
        _warn("Circuit breaker OPEN – skipping GitHub API call")
        return []

    now = datetime.now(tz=timezone.utc)
    created_filter = now.strftime("%Y-%m-%dT%H:%M:%SZ")  # runs after this
    # GitHub's `created` filter uses `>=` when given a single timestamp via `>`
    # so we subtract the lookback window instead.
    from datetime import timedelta

    since = now - timedelta(hours=lookback_hours)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        data = github_get_with_retry(
            f"/repos/{repo}/actions/runs",
            token=token,
            params={"per_page": "100", "created": f">={since_str}"},
            **{k: v for k, v in retry_cfg.items()},
        )
        breaker.record_success()
        return data.get("workflow_runs", [])
    except Exception as exc:
        breaker.record_failure()
        _warn(f"Failed to fetch workflow runs: {exc}")
        return []


def analyse_runs(
    runs: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[Alert]:
    """Inspect runs and return a list of alerts."""
    alerts: list[Alert] = []

    # Group runs by workflow name
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        name = run.get("name") or run.get("workflow_id") or "unknown"
        by_workflow.setdefault(name, []).append(run)

    global_sla = _cfg(config, "sla") or {}
    global_max = float(global_sla.get("max_run_duration_seconds", 600))
    global_hard = float(global_sla.get("hard_timeout_seconds", 1200))
    global_max_fail = float(global_sla.get("max_failure_rate", 0.5))
    global_min_runs = int(global_sla.get("min_runs_for_failure_rate", 3))
    slow_multiplier = float(_cfg(config, "performance", "slow_run_multiplier", default=2.0))

    for workflow_name, wf_runs in by_workflow.items():
        wf_sla = (_cfg(config, "workflow_sla") or {}).get(workflow_name, {})
        max_dur = float(wf_sla.get("max_run_duration_seconds", global_max))
        hard_dur = float(wf_sla.get("hard_timeout_seconds", global_hard))

        durations: list[float] = []
        failed_count = 0
        total_completed = 0

        for run in wf_runs:
            status = run.get("status", "")
            conclusion = run.get("conclusion", "")
            dur = _run_duration(run)

            if status == "completed":
                total_completed += 1
                if conclusion in ("failure", "timed_out", "startup_failure"):
                    failed_count += 1

            if dur is not None:
                durations.append(dur)

            # Hard timeout: still-running run that already exceeds hard_dur
            if status == "in_progress" and dur is not None and dur > hard_dur:
                alerts.append(Alert(
                    severity="error",
                    workflow=workflow_name,
                    message=f"Run #{run.get('run_number')} has been running for "
                            f"{dur:.0f}s (hard timeout {hard_dur:.0f}s)",
                    details=run.get("html_url", ""),
                ))

            # SLA warning: completed run exceeded the soft limit
            elif status == "completed" and dur is not None and dur > max_dur:
                alerts.append(Alert(
                    severity="warning",
                    workflow=workflow_name,
                    message=f"Run #{run.get('run_number')} took {dur:.0f}s "
                            f"(SLA {max_dur:.0f}s)",
                    details=run.get("html_url", ""),
                ))

        # Failure-rate alert
        if total_completed >= global_min_runs:
            failure_rate = failed_count / total_completed
            if failure_rate > global_max_fail:
                alerts.append(Alert(
                    severity="error",
                    workflow=workflow_name,
                    message=f"High failure rate: {failure_rate:.0%} "
                            f"({failed_count}/{total_completed} runs failed)",
                ))

        # Slow-run detection based on rolling median
        if len(durations) >= 3:
            sorted_d = sorted(durations)
            median = sorted_d[len(sorted_d) // 2]
            for run in wf_runs:
                dur = _run_duration(run)
                if dur is not None and dur > median * slow_multiplier and dur > max_dur:
                    alerts.append(Alert(
                        severity="warning",
                        workflow=workflow_name,
                        message=f"Run #{run.get('run_number')} is "
                                f"{dur / median:.1f}× slower than the median "
                                f"({dur:.0f}s vs {median:.0f}s median)",
                        details=run.get("html_url", ""),
                    ))

    return alerts


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def build_summary(
    alerts: list[Alert],
    runs: list[dict[str, Any]],
    circuit_state: str,
) -> list[str]:
    lines = [
        "## 🐕 Watchdog Report",
        "",
        f"**Circuit breaker:** `{circuit_state}`  ",
        f"**Runs analysed:** {len(runs)}  ",
        f"**Alerts:** {len(alerts)}",
        "",
    ]
    if not alerts:
        lines.append("✅ All checks healthy.")
    else:
        lines += [
            "| Severity | Workflow | Message |",
            "|----------|----------|---------|",
        ]
        for a in alerts:
            icon = {"info": "ℹ️", "warning": "⚠️", "error": "🔴"}.get(a.severity, "")
            detail = f" ([details]({a.details}))" if a.details else ""
            lines.append(f"| {icon} {a.severity} | {a.workflow} | {a.message}{detail} |")

    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    config = load_config()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("::error::GITHUB_TOKEN environment variable is not set")
        return 2

    repo = _cfg(config, "watchdog", "repository", default="tooshotti909/Agent-Reach")
    lookback = float(_cfg(config, "watchdog", "lookback_hours", default=2.0))
    min_sev = _cfg(config, "alerts", "min_severity", default=SEVERITY_MIN_DEFAULT)
    fail_on_alert = bool(_cfg(config, "alerts", "fail_on_alert", default=False))
    enable_summary = bool(_cfg(config, "alerts", "github_step_summary", default=True))

    retry_cfg_raw = _cfg(config, "retry") or {}
    retry_cfg = {
        "max_attempts": int(retry_cfg_raw.get("max_attempts", 3)),
        "initial_delay": float(retry_cfg_raw.get("initial_delay_seconds", 10)),
        "backoff": float(retry_cfg_raw.get("backoff_multiplier", 2.0)),
        "max_delay": float(retry_cfg_raw.get("max_delay_seconds", 60)),
    }

    cb_cfg = _cfg(config, "circuit_breaker") or {}
    breaker = CircuitBreaker(
        failure_threshold=int(cb_cfg.get("failure_threshold", 5)),
        recovery_timeout=float(cb_cfg.get("recovery_timeout_seconds", 300)),
        success_threshold=int(cb_cfg.get("success_threshold", 2)),
    )

    _notice(f"Watchdog starting – repo={repo}, lookback={lookback}h")

    runs = fetch_recent_runs(
        repo,
        token=token,
        lookback_hours=lookback,
        breaker=breaker,
        retry_cfg=retry_cfg,
    )

    all_alerts = analyse_runs(runs, config)
    visible = [a for a in all_alerts if _sev_ge(a.severity, min_sev)]

    for alert in visible:
        alert.emit()

    if enable_summary:
        _step_summary(build_summary(visible, runs, breaker.state))

    error_count = sum(1 for a in visible if a.severity == "error")
    if error_count:
        print(f"::error::Watchdog found {error_count} ERROR alert(s)")
        if fail_on_alert:
            return 1

    if visible:
        print(f"::warning::Watchdog found {len(visible)} alert(s) total")
    else:
        _notice("Watchdog: all checks healthy ✅")

    return 0


if __name__ == "__main__":
    sys.exit(main())
