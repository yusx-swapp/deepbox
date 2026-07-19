"""Post-restart smoke check for the DeepBox server.

After a deploy or a process restart an operator wants a single command that says
"is it actually up and healthy?" without hand-crafting curl calls. This script
hits the unauthenticated liveness/readiness/version endpoints and reports a
pass/fail summary with a non-zero exit code on failure so it can gate a
deployment pipeline.

The check logic (:func:`evaluate_smoke`) is separated from the HTTP transport so
it can be unit tested without a live server.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


# A fetcher returns (status_code, body_text). Injected for testability.
Fetcher = Callable[[str], "tuple[int, str]"]


def _http_fetch(url: str, timeout: float) -> "tuple[int, str]":
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check_health(fetch: Fetcher, base_url: str) -> CheckResult:
    try:
        status, body = fetch(f"{base_url}/api/health")
    except Exception as exc:  # noqa: BLE001 - report any transport failure
        return CheckResult("health", False, f"request failed: {exc}")
    if status != 200:
        return CheckResult("health", False, f"HTTP {status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult("health", False, "response was not JSON")
    if data.get("status") != "ok":
        return CheckResult("health", False, f"status={data.get('status')!r}")
    return CheckResult("health", True, "ok")


def check_ready(fetch: Fetcher, base_url: str) -> CheckResult:
    try:
        status, body = fetch(f"{base_url}/api/ready")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("ready", False, f"request failed: {exc}")
    if status != 200:
        return CheckResult("ready", False, f"HTTP {status}: {body[:120]}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult("ready", False, "response was not JSON")
    if data.get("status") != "ready":
        return CheckResult("ready", False, f"status={data.get('status')!r}")
    return CheckResult("ready", True, "ok")


def check_version(fetch: Fetcher, base_url: str) -> CheckResult:
    try:
        status, body = fetch(f"{base_url}/api/version")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("version", False, f"request failed: {exc}")
    if status != 200:
        return CheckResult("version", False, f"HTTP {status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult("version", False, "response was not JSON")
    if not data.get("version"):
        return CheckResult("version", False, "no version field")
    return CheckResult("version", True, f"version={data['version']} commit={data.get('commit')}")


def evaluate_smoke(fetch: Fetcher, base_url: str) -> list[CheckResult]:
    """Run every check against ``base_url`` and return the results."""

    base_url = base_url.rstrip("/")
    return [
        check_health(fetch, base_url),
        check_ready(fetch, base_url),
        check_version(fetch, base_url),
    ]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DeepBox post-restart smoke check")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8077",
        help="server base URL (default: http://127.0.0.1:8077)",
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="per-request timeout seconds"
    )
    args = parser.parse_args(argv)

    def fetch(url: str) -> "tuple[int, str]":
        return _http_fetch(url, args.timeout)

    results = evaluate_smoke(fetch, args.base_url)
    all_ok = True
    for r in results:
        marker = "PASS" if r.ok else "FAIL"
        print(f"[{marker}] {r.name}: {r.detail}")
        all_ok = all_ok and r.ok

    print("smoke check passed" if all_ok else "smoke check FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
