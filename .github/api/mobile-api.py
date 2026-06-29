#!/usr/bin/env python3
"""
iPhone 16 <-> GitHub Mobile API Bridge
========================================
Handles three operations used by the elite-orchestrator workflow:

  broadcast        -- send a webhook event payload to registered mobile clients
  push-notify      -- send an APNs push notification to an iPhone 16
  dashboard-update -- persist the latest workflow status for the mobile dashboard

All secrets (APNS_KEY_ID, APNS_TEAM_ID, APNS_AUTH_KEY, MOBILE_DEVICE_TOKEN,
WEBHOOK_SECRET) must be stored as GitHub Actions secrets. This script will
gracefully skip any operation for which secrets are absent.

Usage:
    python3 .github/api/mobile-api.py \
        --action push-notify \
        --event push \
        --ref refs/heads/main \
        --sha <sha> \
        --status success
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# -- Constants -----------------------------------------------------------------

APNS_HOST_PROD = "api.push.apple.com"
APNS_HOST_SAND = "api.sandbox.push.apple.com"
DASHBOARD_STATE_FILE = Path("/tmp/elite-dashboard-state.json")

# Human-readable labels for GitHub event types
EVENT_LABELS: dict[str, str] = {
    "push": "Code pushed",
    "pull_request": "Pull request event",
    "workflow_run": "Workflow completed",
    "check_run": "Check run updated",
    "schedule": "Scheduled run",
    "workflow_dispatch": "Manual trigger",
}

STATUS_EMOJI: dict[str, str] = {
    "success": "OK",
    "failure": "FAILED",
    "warning": "WARN",
    "running": "RUNNING",
    "unknown": "?",
}


# -- Helpers -------------------------------------------------------------------

def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def _sign_payload(payload: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for a webhook payload."""
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _build_event_payload(
    event: str,
    ref: str,
    sha: str,
    status: str,
    repo: str,
) -> dict[str, Any]:
    return {
        "source": "github",
        "event": event,
        "ref": ref,
        "sha": sha[:12] if sha else "",
        "status": status,
        "repository": repo,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": EVENT_LABELS.get(event, event),
        "status_label": STATUS_EMOJI.get(status, "?"),
    }


# -- Operations ----------------------------------------------------------------

def op_broadcast(args: argparse.Namespace) -> int:
    """
    POST a signed event payload to the MOBILE_WEBHOOK_URL endpoint.
    Skipped gracefully when no secret / URL is configured.
    """
    secret = _env("WEBHOOK_SECRET")
    webhook_url = _env("MOBILE_WEBHOOK_URL")

    if not secret:
        print("  broadcast: WEBHOOK_SECRET not set -- skipping")
        return 0
    if not webhook_url:
        print("  broadcast: MOBILE_WEBHOOK_URL not set -- skipping")
        return 0

    repo = _env("GITHUB_REPOSITORY") or "unknown/unknown"
    payload = _build_event_payload(args.event, args.ref, args.sha, args.status, repo)
    body = json.dumps(payload).encode()
    signature = _sign_payload(body, secret)

    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": args.event,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_code = resp.getcode()
            print(f"Broadcast sent -> HTTP {status_code}")
            return 0
    except urllib.error.URLError as exc:
        print(f"  broadcast: request failed -- {exc}")
        return 1


def _build_apns_jwt(key_id: str, team_id: str, auth_key_b64: str) -> str | None:
    """
    Build an APNs provider authentication token (JWT, ES256).

    Returns the JWT string, or None when the 'cryptography' package is absent.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        print(
            "  push-notify: 'cryptography' package not installed; "
            "APNs JWT signing skipped. Install with: pip install cryptography"
        )
        return None

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "ES256", "kid": key_id}).encode()
    ).rstrip(b"=")
    claims = base64.urlsafe_b64encode(
        json.dumps({"iss": team_id, "iat": int(time.time())}).encode()
    ).rstrip(b"=")

    key_pem = base64.b64decode(auth_key_b64)
    private_key = serialization.load_pem_private_key(key_pem, None)
    signing_input = header + b"." + claims
    signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    jwt_token = (
        signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")
    ).decode()
    return jwt_token


def op_push_notify(args: argparse.Namespace) -> int:
    """
    Send an APNs push notification to the registered iPhone 16.

    Requires secrets: APNS_KEY_ID, APNS_TEAM_ID, APNS_AUTH_KEY,
    MOBILE_DEVICE_TOKEN. Gracefully skipped when any are absent.
    """
    key_id = _env("APNS_KEY_ID")
    team_id = _env("APNS_TEAM_ID")
    auth_key = _env("APNS_AUTH_KEY")
    device_token = _env("MOBILE_DEVICE_TOKEN")
    bundle_id = _env("APNS_BUNDLE_ID") or "com.agentreach.mobile"

    # Build the list of missing secret *names* without coupling secret values
    # to the names list (avoids logging sensitive data).
    missing: list[str] = []
    if not key_id:
        missing.append("APNS_KEY_ID")
    if not team_id:
        missing.append("APNS_TEAM_ID")
    if not auth_key:
        missing.append("APNS_AUTH_KEY")
    if not device_token:
        missing.append("MOBILE_DEVICE_TOKEN")
    if missing:
        print(
            f"  push-notify: secrets not configured ({', '.join(missing)}) -- skipping"
        )
        return 0

    repo = _env("GITHUB_REPOSITORY") or "unknown/unknown"
    status_label = STATUS_EMOJI.get(args.status, "?")
    ref_short = args.ref.replace("refs/heads/", "").replace("refs/tags/", "")

    notification_payload = {
        "aps": {
            "alert": {
                "title": f"[{status_label}] Agent-Reach",
                "body": (
                    f"{EVENT_LABELS.get(args.event, args.event)} on {ref_short} "
                    f"({repo})"
                ),
                "subtitle": f"SHA {args.sha[:8] if args.sha else 'unknown'}",
            },
            "sound": "default" if args.status == "failure" else "chime.caf",
            "badge": 1 if args.status == "failure" else 0,
            "thread-id": f"agent-reach-{repo}",
        },
        "elite": {
            "event": args.event,
            "ref": args.ref,
            "sha": args.sha,
            "status": args.status,
        },
    }

    jwt_token = _build_apns_jwt(key_id, team_id, auth_key)
    if jwt_token is None:
        return 0

    body = json.dumps(notification_payload).encode()
    url = f"https://{APNS_HOST_PROD}/3/device/{device_token}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "authorization": f"bearer {jwt_token}",
            "apns-topic": bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"APNs push sent -> HTTP {resp.getcode()}")
            return 0
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        print(f"  push-notify: APNs error {exc.code} -- {err_body}")
        return 1
    except urllib.error.URLError as exc:
        print(f"  push-notify: network error -- {exc}")
        return 1


def op_dashboard_update(args: argparse.Namespace) -> int:
    """
    Persist a lightweight JSON state file that the mobile dashboard can poll.
    Written to /tmp so it is ephemeral per CI run (in production, push to a
    persistent store such as a backend API or GitHub Gist).
    """
    repo = _env("GITHUB_REPOSITORY") or "unknown/unknown"
    state: dict[str, Any] = {}

    if DASHBOARD_STATE_FILE.exists():
        try:
            state = json.loads(DASHBOARD_STATE_FILE.read_text())
        except json.JSONDecodeError:
            state = {}

    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    state["repository"] = repo
    state["latest_run"] = _build_event_payload(
        args.event, args.ref, args.sha, args.status, repo
    )
    state.setdefault("history", []).insert(0, state["latest_run"])
    state["history"] = state["history"][:20]  # keep last 20

    DASHBOARD_STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"Dashboard state updated -> {DASHBOARD_STATE_FILE}")
    return 0


# -- CLI -----------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="iPhone 16 <-> GitHub Mobile API Bridge")
    p.add_argument(
        "--action",
        required=True,
        choices=["broadcast", "push-notify", "dashboard-update"],
        help="Operation to perform",
    )
    p.add_argument("--event", default="push", help="GitHub event name")
    p.add_argument("--ref", default="", help="Git ref")
    p.add_argument("--sha", default="", help="Commit SHA")
    p.add_argument("--status", default="unknown", help="Workflow status")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    dispatch = {
        "broadcast": op_broadcast,
        "push-notify": op_push_notify,
        "dashboard-update": op_dashboard_update,
    }
    return dispatch[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
