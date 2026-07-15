#!/usr/bin/env python3
"""Black-box verifier for the platform permission diagnostic endpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_FAILED = 2
EXIT_REQUEST_ERROR = 3
PROVIDER_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def provider_key(value: str) -> str:
    normalized = value.strip().lower()
    if not PROVIDER_KEY_PATTERN.fullmatch(normalized):
        raise argparse.ArgumentTypeError(
            "provider must match ^[a-z0-9][a-z0-9_-]{0,63}$"
        )
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Call a deployed Review Orchestrator and verify its configured "
            "provider permissions without mutating the provider."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("REVIEW_ORCHESTRATOR_URL", "http://localhost:8000"),
        help=(
            "Review Orchestrator base URL (default: REVIEW_ORCHESTRATOR_URL or "
            "http://localhost:8000)."
        ),
    )
    parser.add_argument(
        "--provider",
        required=True,
        type=provider_key,
        help="Registered provider key, for example github, gitlab, or gitmilk.",
    )
    parser.add_argument(
        "--repository",
        required=True,
        help="Provider repository path, for example owner/repo or group/project.",
    )
    parser.add_argument(
        "--pull-request",
        type=int,
        help="Optional GitHub PR number or GitLab merge request IID.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Request timeout in seconds (default: 15).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the unmodified JSON response instead of a human summary.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.pull_request is not None and args.pull_request <= 0:
        print("error: --pull-request must be greater than zero", file=sys.stderr)
        return EXIT_REQUEST_ERROR
    if args.timeout <= 0:
        print("error: --timeout must be greater than zero", file=sys.stderr)
        return EXIT_REQUEST_ERROR

    payload: dict[str, Any] = {
        "provider": args.provider,
        "repo_full_name": args.repository,
    }
    if args.pull_request is not None:
        payload["pull_request_number"] = args.pull_request

    url = f"{args.base_url.rstrip('/')}/api/v1/diagnostics/platform-permissions"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "review-orchestrator-permission-check/1.0",
    }
    operator_token = os.getenv("REVIEW_PROXY_TOKEN")
    if operator_token:
        headers["X-Review-Token"] = operator_token

    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=args.timeout) as response:  # noqa: S310
            result = json.loads(response.read())
    except HTTPError as exc:
        print(
            f"error: diagnostic endpoint returned HTTP {exc.code}",
            file=sys.stderr,
        )
        return EXIT_REQUEST_ERROR
    except URLError as exc:
        reason = getattr(exc, "reason", "connection failed")
        print(f"error: cannot reach diagnostic endpoint: {reason}", file=sys.stderr)
        return EXIT_REQUEST_ERROR
    except (TimeoutError, json.JSONDecodeError) as exc:
        print(f"error: invalid diagnostic response: {exc}", file=sys.stderr)
        return EXIT_REQUEST_ERROR

    if not isinstance(result, dict) or result.get("status") not in {
        "healthy",
        "degraded",
        "failed",
    }:
        print("error: response does not match the diagnostic contract", file=sys.stderr)
        return EXIT_REQUEST_ERROR

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_human_summary(result)
    return {
        "healthy": EXIT_HEALTHY,
        "degraded": EXIT_DEGRADED,
        "failed": EXIT_FAILED,
    }[result["status"]]


def print_human_summary(result: dict[str, Any]) -> None:
    print(f"Provider: {result.get('provider', '-')}")
    print(f"Repository: {result.get('repo_full_name', '-')}")
    print(f"Status: {str(result['status']).upper()}")
    print(f"Token configured: {result.get('token_configured', False)}")
    if result.get("repository_role"):
        print(f"Repository role: {result['repository_role']}")
    if result.get("reported_scopes"):
        print(f"Reported scopes: {', '.join(result['reported_scopes'])}")
    if result.get("rate_limit_remaining") is not None:
        print(f"Rate limit remaining: {result['rate_limit_remaining']}")
    print("Checks:")
    for check in result.get("checks", []):
        if not isinstance(check, dict):
            continue
        status = str(check.get("status", "unknown")).upper()
        print(f"  [{status}] {check.get('name', '-')}: {check.get('message', '')}")


if __name__ == "__main__":
    raise SystemExit(main())
