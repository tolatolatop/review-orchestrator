from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from review_orchestrator.config import Settings
from review_orchestrator.schemas import (
    PlatformPermissionCheck,
    PlatformPermissionDiagnosticRequest,
    PlatformPermissionDiagnosticResponse,
)


async def diagnose_platform_permissions(
    settings: Settings,
    payload: PlatformPermissionDiagnosticRequest,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PlatformPermissionDiagnosticResponse:
    """Run non-mutating checks against the configured provider API.

    Provider APIs do not always expose fine-grained write grants. In that case
    the result deliberately reports ``unknown`` instead of creating a probe
    comment or claiming that access was verified.
    """
    if payload.provider == "github":
        return await _diagnose_github(settings, payload, transport=transport)
    return await _diagnose_gitlab(settings, payload, transport=transport)


async def _diagnose_github(
    settings: Settings,
    payload: PlatformPermissionDiagnosticRequest,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> PlatformPermissionDiagnosticResponse:
    token = settings.github_installation_token
    if not token:
        return _unconfigured(payload, "GITHUB_INSTALLATION_TOKEN is not configured.")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repo_path = quote(payload.repo_full_name, safe="/")
    async with httpx.AsyncClient(
        base_url=settings.github_api_base_url.rstrip("/"),
        headers=headers,
        timeout=settings.platform_diagnostics_timeout_seconds,
        transport=transport,
    ) as client:
        repo_response = await _safe_get(client, f"/repos/{repo_path}")
        if isinstance(repo_response, Exception):
            return _unreachable(payload, "GitHub", repo_response)

        scopes = _header_list(repo_response.headers, "x-oauth-scopes")
        rate_limit = _header_int(repo_response.headers, "x-ratelimit-remaining")
        if repo_response.status_code != 200:
            return _denied(
                payload,
                provider_name="GitHub",
                status_code=repo_response.status_code,
                scopes=scopes,
                rate_limit_remaining=rate_limit,
            )

        body = _json_object(repo_response)
        permissions = body.get("permissions")
        permissions = permissions if isinstance(permissions, dict) else {}
        role = _github_role(permissions)
        checks = [
            PlatformPermissionCheck(
                name="repository_read",
                status="passed",
                message="The configured token can read the repository.",
            )
        ]
        await _append_github_pr_check(client, payload, repo_path, checks)
        write_verified = bool({"repo", "public_repo"}.intersection(scopes))
        for name, label in (
            ("summary_comment_write", "issue comment"),
            ("line_comment_write", "pull request review comment"),
        ):
            checks.append(
                PlatformPermissionCheck(
                    name=name,
                    status="passed" if write_verified else "unknown",
                    message=(
                        f"The reported OAuth scope permits {label} writes."
                        if write_verified
                        else (
                            f"Repository role {role!r} was reported, but GitHub does "
                            f"not expose this token's fine-grained {label} grant on a "
                            "read-only request."
                        )
                    ),
                )
            )
        return _response(
            payload,
            checks=checks,
            scopes=scopes,
            repository_role=role,
            rate_limit_remaining=rate_limit,
        )


async def _append_github_pr_check(
    client: httpx.AsyncClient,
    payload: PlatformPermissionDiagnosticRequest,
    repo_path: str,
    checks: list[PlatformPermissionCheck],
) -> None:
    if payload.pull_request_number is None:
        checks.append(
            PlatformPermissionCheck(
                name="pull_request_read",
                status="skipped",
                message="No pull_request_number was supplied.",
            )
        )
        return
    response = await _safe_get(
        client,
        f"/repos/{repo_path}/pulls/{payload.pull_request_number}",
    )
    checks.append(_read_check("pull_request_read", "pull request", response))


async def _diagnose_gitlab(
    settings: Settings,
    payload: PlatformPermissionDiagnosticRequest,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> PlatformPermissionDiagnosticResponse:
    token = settings.gitlab_api_token
    if not token:
        return _unconfigured(payload, "GITLAB_API_TOKEN is not configured.")

    headers = {"Accept": "application/json", "PRIVATE-TOKEN": token}
    project_path = quote(payload.repo_full_name, safe="")
    async with httpx.AsyncClient(
        base_url=settings.gitlab_api_base_url.rstrip("/"),
        headers=headers,
        timeout=settings.platform_diagnostics_timeout_seconds,
        transport=transport,
    ) as client:
        project_response = await _safe_get(client, f"/projects/{project_path}")
        if isinstance(project_response, Exception):
            return _unreachable(payload, "GitLab", project_response)

        scopes = _header_list(project_response.headers, "x-oauth-scopes")
        rate_limit = _header_int(project_response.headers, "ratelimit-remaining")
        if project_response.status_code != 200:
            return _denied(
                payload,
                provider_name="GitLab",
                status_code=project_response.status_code,
                scopes=scopes,
                rate_limit_remaining=rate_limit,
            )

        body = _json_object(project_response)
        access_level = _gitlab_access_level(body.get("permissions"))
        role = _gitlab_role(access_level)
        checks = [
            PlatformPermissionCheck(
                name="repository_read",
                status="passed",
                message="The configured token can read the project.",
            )
        ]
        await _append_gitlab_mr_check(client, payload, project_path, checks)
        write_verified = (
            "api" in scopes or access_level is not None and access_level >= 30
        )
        checks.append(
            PlatformPermissionCheck(
                name="summary_comment_write",
                status="passed" if write_verified else "unknown",
                message=(
                    "The reported scope or project role permits merge request notes."
                    if write_verified
                    else (
                        "GitLab did not report an API scope or project role that can "
                        "prove note write access without creating a note."
                    )
                ),
            )
        )
        checks.append(
            PlatformPermissionCheck(
                name="line_comment_write",
                status="passed" if write_verified else "unknown",
                message=(
                    "The reported scope or project role permits merge request "
                    "discussions."
                    if write_verified
                    else (
                        "GitLab did not report enough information to prove discussion "
                        "write access without creating a discussion."
                    )
                ),
            )
        )
        return _response(
            payload,
            checks=checks,
            scopes=scopes,
            repository_role=role,
            rate_limit_remaining=rate_limit,
        )


async def _append_gitlab_mr_check(
    client: httpx.AsyncClient,
    payload: PlatformPermissionDiagnosticRequest,
    project_path: str,
    checks: list[PlatformPermissionCheck],
) -> None:
    if payload.pull_request_number is None:
        checks.append(
            PlatformPermissionCheck(
                name="pull_request_read",
                status="skipped",
                message="No pull_request_number was supplied.",
            )
        )
        return
    response = await _safe_get(
        client,
        f"/projects/{project_path}/merge_requests/{payload.pull_request_number}",
    )
    checks.append(_read_check("pull_request_read", "merge request", response))


async def _safe_get(
    client: httpx.AsyncClient,
    path: str,
) -> httpx.Response | httpx.RequestError:
    try:
        return await client.get(path)
    except httpx.RequestError as exc:
        return exc


def _read_check(
    name: str,
    resource: str,
    response: httpx.Response | Exception,
) -> PlatformPermissionCheck:
    if isinstance(response, Exception):
        return PlatformPermissionCheck(
            name=name,
            status="failed",
            message=f"Could not reach the provider API to read the {resource}.",
        )
    if response.status_code == 200:
        return PlatformPermissionCheck(
            name=name,
            status="passed",
            message=f"The configured token can read the {resource}.",
        )
    return PlatformPermissionCheck(
        name=name,
        status="failed",
        message=(
            f"The provider returned HTTP {response.status_code} for the {resource}."
        ),
    )


def _unconfigured(
    payload: PlatformPermissionDiagnosticRequest,
    message: str,
) -> PlatformPermissionDiagnosticResponse:
    return _response(
        payload,
        token_configured=False,
        checks=[
            PlatformPermissionCheck(
                name="token_configured",
                status="failed",
                message=message,
            )
        ],
    )


def _unreachable(
    payload: PlatformPermissionDiagnosticRequest,
    provider_name: str,
    _error: Exception,
) -> PlatformPermissionDiagnosticResponse:
    return _response(
        payload,
        checks=[
            PlatformPermissionCheck(
                name="provider_api_reachable",
                status="failed",
                message=f"Could not reach the {provider_name} API.",
            )
        ],
    )


def _denied(
    payload: PlatformPermissionDiagnosticRequest,
    *,
    provider_name: str,
    status_code: int,
    scopes: list[str],
    rate_limit_remaining: int | None,
) -> PlatformPermissionDiagnosticResponse:
    return _response(
        payload,
        scopes=scopes,
        rate_limit_remaining=rate_limit_remaining,
        checks=[
            PlatformPermissionCheck(
                name="repository_read",
                status="failed",
                message=(
                    f"{provider_name} returned HTTP {status_code}; the token is "
                    "invalid, "
                    "expired, or cannot access this repository."
                ),
            )
        ],
    )


def _response(
    payload: PlatformPermissionDiagnosticRequest,
    *,
    checks: list[PlatformPermissionCheck],
    token_configured: bool = True,
    scopes: list[str] | None = None,
    repository_role: str | None = None,
    rate_limit_remaining: int | None = None,
) -> PlatformPermissionDiagnosticResponse:
    statuses = {check.status for check in checks if check.required}
    overall_status = (
        "failed"
        if "failed" in statuses
        else "degraded"
        if statuses.intersection({"unknown", "skipped"})
        else "healthy"
    )
    return PlatformPermissionDiagnosticResponse(
        provider=payload.provider,
        repo_full_name=payload.repo_full_name,
        pull_request_number=payload.pull_request_number,
        status=overall_status,
        token_configured=token_configured,
        reported_scopes=scopes or [],
        repository_role=repository_role,
        rate_limit_remaining=rate_limit_remaining,
        checks=checks,
    )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _header_list(headers: Mapping[str, str], name: str) -> list[str]:
    return sorted(
        {
            item.strip()
            for item in headers.get(name, "").split(",")
            if item.strip()
        }
    )


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None


def _github_role(permissions: dict[str, Any]) -> str | None:
    for role in ("admin", "maintain", "push", "triage", "pull"):
        if permissions.get(role) is True:
            return role
    return None


def _gitlab_access_level(permissions: Any) -> int | None:
    if not isinstance(permissions, dict):
        return None
    levels: list[int] = []
    for key in ("project_access", "group_access"):
        access = permissions.get(key)
        if isinstance(access, dict) and isinstance(access.get("access_level"), int):
            levels.append(access["access_level"])
    return max(levels, default=None)


def _gitlab_role(access_level: int | None) -> str | None:
    if access_level is None:
        return None
    roles = (
        (50, "owner"),
        (40, "maintainer"),
        (30, "developer"),
        (20, "reporter"),
        (10, "guest"),
    )
    return next(
        (role for threshold, role in roles if access_level >= threshold),
        "minimal",
    )
