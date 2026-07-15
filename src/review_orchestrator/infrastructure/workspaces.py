"""Isolated Git workspace lifecycle and credential-safe command execution."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.domain.models import Workspace, WorkspaceLease, utc_now
from review_orchestrator.domain.schemas import (
    CleanupSummary,
    WorkspacePrepareRequest,
    WorkspacePrepareResponse,
)
from review_orchestrator.infrastructure.config import Settings
from review_orchestrator.integrations.github import GitHubClient, GitHubClientError


class WorkspaceErrorCode:
    auth_failed = "auth_failed"
    base_missing = "base_missing"
    cleanup_failed = "cleanup_failed"
    git_error = "git_error"
    head_missing = "head_missing"
    network_error = "network_error"
    repo_not_found = "repo_not_found"
    workspace_locked = "workspace_locked"


@dataclass(frozen=True)
class GitCommandError(Exception):
    code: str
    message: str


def workspace_identity(
    *,
    provider: str,
    repository: str,
    pull_request_number: int,
    head_sha: str,
) -> str:
    hashed_repo = repo_hash(repository)
    return f"{provider}:{hashed_repo}:pr:{pull_request_number}:head:{head_sha}"


def repo_hash(repository: str) -> str:
    normalized = repository.strip().lower().encode()
    return hashlib.sha256(normalized).hexdigest()[:16]


async def prepare_workspace(
    session: AsyncSession,
    settings: Settings,
    request: WorkspacePrepareRequest,
    *,
    github_client: GitHubClient | None = None,
) -> WorkspacePrepareResponse:
    if request.provider != "github":
        return WorkspacePrepareResponse(
            workspace_id="",
            workspace_path="",
            base_sha=request.pull_request.base_sha,
            head_sha=request.pull_request.head_sha,
            status="failed",
            failure_code=WorkspaceErrorCode.git_error,
            failure_message=f"Unsupported provider: {request.provider}",
        )

    identity = workspace_identity(
        provider=request.provider,
        repository=request.repository.full_name,
        pull_request_number=request.pull_request.number,
        head_sha=request.pull_request.head_sha,
    )
    existing = await get_workspace(session, identity)
    if existing and existing.status == "ready" and not request.options.force_refresh:
        existing.last_used_at = utc_now()
        await session.commit()
        await session.refresh(existing)
        return _prepare_response(existing, from_cache=True)

    paths = _workspace_paths(settings, request.repository.full_name, request)
    workspace = existing
    if workspace is None:
        workspace = Workspace(
            workspace_id=identity,
            provider=request.provider,
            repository=request.repository.full_name,
            repository_clone_url=_safe_clone_url(request.repository.clone_url),
            repo_hash=paths.repo_hash,
            pull_request_number=request.pull_request.number,
            base_sha=request.pull_request.base_sha,
            head_sha=request.pull_request.head_sha,
            workspace_path=str(paths.repo_path),
            cache_path=str(paths.cache_path) if request.options.use_git_cache else None,
            status="preparing",
            expires_at=utc_now() + timedelta(hours=24),
        )
        session.add(workspace)
    else:
        workspace.status = "preparing"
        workspace.failure_code = None
        workspace.failure_message = None
        workspace.base_sha = request.pull_request.base_sha
        workspace.repository_clone_url = _safe_clone_url(request.repository.clone_url)
        workspace.cache_path = (
            str(paths.cache_path) if request.options.use_git_cache else None
        )
    await session.commit()
    await session.refresh(workspace)

    try:
        auth_token = (
            await github_client.get_token(request.repository.full_name)
            if github_client is not None
            else None
        )
        await asyncio.to_thread(_prepare_git_workspace, paths, request, auth_token)
    except GitHubClientError as exc:
        workspace.status = "failed"
        workspace.failure_code = WorkspaceErrorCode.auth_failed
        workspace.failure_message = str(exc)
        workspace.expires_at = utc_now() + timedelta(hours=6)
        await session.commit()
        await session.refresh(workspace)
        return _prepare_response(workspace, from_cache=False)
    except GitCommandError as exc:
        workspace.status = "failed"
        workspace.failure_code = exc.code
        workspace.failure_message = exc.message
        workspace.expires_at = utc_now() + timedelta(hours=6)
        await session.commit()
        await session.refresh(workspace)
        return _prepare_response(workspace, from_cache=False)

    workspace.status = "ready"
    workspace.failure_code = None
    workspace.failure_message = None
    workspace.ready_at = utc_now()
    workspace.last_used_at = workspace.ready_at
    workspace.expires_at = utc_now() + timedelta(hours=24)
    await session.commit()
    await session.refresh(workspace)
    return _prepare_response(workspace, from_cache=request.options.use_git_cache)


async def get_workspace(
    session: AsyncSession,
    workspace_id: str,
) -> Workspace | None:
    result = await session.execute(
        select(Workspace).where(Workspace.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def lease_workspace(
    session: AsyncSession,
    workspace_id: str,
    *,
    review_run_id: str | None = None,
    session_id: str | None = None,
) -> tuple[WorkspaceLease, Workspace]:
    workspace = await get_workspace(session, workspace_id)
    if workspace is None:
        raise KeyError(workspace_id)
    lease = WorkspaceLease(
        workspace_id=workspace_id,
        review_run_id=review_run_id,
        session_id=session_id,
    )
    workspace.status = "leased"
    workspace.last_used_at = utc_now()
    session.add(lease)
    await session.commit()
    await session.refresh(lease)
    await session.refresh(workspace)
    return lease, workspace


async def release_workspace(
    session: AsyncSession,
    lease_id: str,
) -> Workspace | None:
    lease = await session.get(WorkspaceLease, lease_id)
    if lease is None:
        return None
    if lease.released_at is None:
        lease.released_at = utc_now()
    workspace = await get_workspace(session, lease.workspace_id)
    if workspace and not await _has_other_active_lease(session, lease):
        workspace.status = "idle"
        workspace.last_used_at = utc_now()
    await session.commit()
    if workspace:
        await session.refresh(workspace)
    return workspace


async def cleanup_workspace(
    session: AsyncSession,
    workspace_id: str,
    *,
    force: bool = False,
) -> str:
    workspace = await get_workspace(session, workspace_id)
    if workspace is None:
        return "deleted"
    if not force and await _has_active_lease(session, workspace_id):
        workspace.failure_code = WorkspaceErrorCode.workspace_locked
        await session.commit()
        return WorkspaceErrorCode.workspace_locked

    workspace.status = "cleaning"
    await session.commit()
    try:
        shutil.rmtree(workspace.workspace_path, ignore_errors=True)
    except OSError as exc:
        workspace.status = "failed"
        workspace.failure_code = WorkspaceErrorCode.cleanup_failed
        workspace.failure_message = str(exc)
        await session.commit()
        return WorkspaceErrorCode.cleanup_failed

    workspace.status = "deleted"
    workspace.expires_at = utc_now()
    await session.commit()
    return "deleted"


async def cleanup_pull_request_workspaces(
    session: AsyncSession,
    *,
    provider: str,
    repository: str,
    pull_request_number: int,
    force: bool = False,
) -> CleanupSummary:
    result = await session.execute(
        select(Workspace).where(
            Workspace.provider == provider,
            Workspace.repository == repository,
            Workspace.pull_request_number == pull_request_number,
        )
    )
    return await _cleanup_many(session, list(result.scalars().all()), force=force)


async def cleanup_expired_workspaces(
    session: AsyncSession,
    now: datetime | None = None,
) -> CleanupSummary:
    now = now or utc_now()
    result = await session.execute(
        select(Workspace).where(
            Workspace.expires_at.is_not(None),
            Workspace.expires_at <= now,
            Workspace.status != "deleted",
        )
    )
    return await _cleanup_many(session, list(result.scalars().all()), force=False)


async def _cleanup_many(
    session: AsyncSession,
    workspaces: list[Workspace],
    *,
    force: bool,
) -> CleanupSummary:
    summary = CleanupSummary()
    for workspace in workspaces:
        result = await cleanup_workspace(session, workspace.workspace_id, force=force)
        if result == "deleted":
            summary.deleted += 1
        elif result == WorkspaceErrorCode.workspace_locked:
            summary.skipped_locked += 1
        else:
            summary.failed += 1
    return summary


async def _has_active_lease(session: AsyncSession, workspace_id: str) -> bool:
    result = await session.execute(
        select(WorkspaceLease).where(
            WorkspaceLease.workspace_id == workspace_id,
            WorkspaceLease.released_at.is_(None),
        )
    )
    return result.scalar_one_or_none() is not None


async def _has_other_active_lease(
    session: AsyncSession,
    lease: WorkspaceLease,
) -> bool:
    result = await session.execute(
        select(WorkspaceLease).where(
            WorkspaceLease.workspace_id == lease.workspace_id,
            WorkspaceLease.id != lease.id,
            WorkspaceLease.released_at.is_(None),
        )
    )
    return result.scalar_one_or_none() is not None


@dataclass(frozen=True)
class WorkspacePaths:
    repo_hash: str
    repo_path: Path
    cache_path: Path


def _workspace_paths(
    settings: Settings,
    repository: str,
    request: WorkspacePrepareRequest,
) -> WorkspacePaths:
    hashed = repo_hash(repository)
    workspace_root = Path(settings.workspace_root)
    cache_root = Path(settings.git_cache_root)
    return WorkspacePaths(
        repo_hash=hashed,
        repo_path=workspace_root
        / request.provider
        / hashed
        / f"pr-{request.pull_request.number}"
        / request.pull_request.head_sha
        / "repo",
        cache_path=cache_root / request.provider / f"{hashed}.git",
    )


def _prepare_git_workspace(
    paths: WorkspacePaths,
    request: WorkspacePrepareRequest,
    auth_token: str | None = None,
) -> None:
    paths.repo_path.parent.mkdir(parents=True, exist_ok=True)
    if request.options.force_refresh:
        shutil.rmtree(paths.repo_path, ignore_errors=True)

    env = _git_env(request, auth_token=auth_token)
    if request.options.use_git_cache:
        _ensure_cache(paths.cache_path, request.repository.clone_url, env)
        clone_source = str(paths.cache_path)
    else:
        clone_source = request.repository.clone_url

    if not paths.repo_path.exists():
        _run_git(["git", "clone", clone_source, str(paths.repo_path)], env=env)
    else:
        _run_git(["git", "-C", str(paths.repo_path), "fetch", "--all"], env=env)

    _ensure_commit(paths.repo_path, request.pull_request.base_sha, env, is_base=True)
    _ensure_commit(paths.repo_path, request.pull_request.head_sha, env, is_base=False)
    _run_git(
        [
            "git",
            "-C",
            str(paths.repo_path),
            "checkout",
            "--detach",
            request.pull_request.head_sha,
        ],
        env=env,
    )


def _ensure_cache(cache_path: Path, clone_url: str, env: dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        _run_git(["git", "-C", str(cache_path), "remote", "update", "--prune"], env=env)
        return
    _run_git(["git", "clone", "--mirror", clone_url, str(cache_path)], env=env)


def _ensure_commit(
    repo_path: Path,
    sha: str,
    env: dict[str, str],
    *,
    is_base: bool,
) -> None:
    try:
        _run_git(
            ["git", "-C", str(repo_path), "cat-file", "-e", f"{sha}^{{commit}}"],
            env=env,
        )
    except GitCommandError as exc:
        try:
            _run_git(["git", "-C", str(repo_path), "fetch", "origin", sha], env=env)
            _run_git(
                ["git", "-C", str(repo_path), "cat-file", "-e", f"{sha}^{{commit}}"],
                env=env,
            )
        except GitCommandError as fetch_exc:
            code = (
                WorkspaceErrorCode.base_missing
                if is_base
                else WorkspaceErrorCode.head_missing
            )
            message = fetch_exc.message or exc.message
            raise GitCommandError(code, message) from fetch_exc


def _run_git(command: list[str], *, env: dict[str, str]) -> None:
    try:
        result = subprocess.run(
            command,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(
            WorkspaceErrorCode.network_error,
            "git command timed out",
        ) from exc

    if result.returncode == 0:
        return

    stderr = _mask_secret(result.stderr.strip(), env)
    code = _classify_git_error(stderr)
    raise GitCommandError(code, stderr or "git command failed")


def _classify_git_error(stderr: str) -> str:
    lowered = stderr.lower()
    if "authentication failed" in lowered or "could not read username" in lowered:
        return WorkspaceErrorCode.auth_failed
    if (
        "repository not found" in lowered
        or "not appear to be a git repository" in lowered
    ):
        return WorkspaceErrorCode.repo_not_found
    if "couldn't find remote ref" in lowered or "not our ref" in lowered:
        return WorkspaceErrorCode.head_missing
    if "unable to access" in lowered or "could not resolve host" in lowered:
        return WorkspaceErrorCode.network_error
    if "not a valid object name" in lowered or "pathspec" in lowered:
        return WorkspaceErrorCode.head_missing
    return WorkspaceErrorCode.git_error


def _git_env(
    request: WorkspacePrepareRequest,
    *,
    auth_token: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    token_ref = request.auth.token_ref if request.auth else None
    token = auth_token or (os.environ.get(token_ref) if token_ref else None)
    if not token:
        if token_ref:
            raise GitCommandError(
                WorkspaceErrorCode.auth_failed,
                f"Token reference is not available: {token_ref}",
            )
        return env

    authenticated_base = _authenticated_https_base(request.repository.clone_url)
    if authenticated_base is None:
        return env
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = (
        f"url.https://x-access-token:{token}@{authenticated_base}.insteadOf"
    )
    env["GIT_CONFIG_VALUE_0"] = f"https://{authenticated_base}"
    env["REVIEW_GIT_TOKEN_MASK"] = token
    return env


def _authenticated_https_base(clone_url: str) -> str | None:
    parsed = urlsplit(clone_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.hostname}{port}/"


def _mask_secret(value: str, env: dict[str, str]) -> str:
    token = env.get("REVIEW_GIT_TOKEN_MASK")
    return value.replace(token, "***") if token else value


def _safe_clone_url(clone_url: str) -> str:
    if "@" not in clone_url:
        return clone_url
    scheme, separator, host_path = clone_url.partition("://")
    if not separator:
        return clone_url
    _, at, safe_host_path = host_path.rpartition("@")
    if not at:
        return clone_url
    return f"{scheme}://{safe_host_path}"


def _prepare_response(
    workspace: Workspace,
    *,
    from_cache: bool,
) -> WorkspacePrepareResponse:
    return WorkspacePrepareResponse(
        workspace_id=workspace.workspace_id,
        workspace_path=workspace.workspace_path,
        base_sha=workspace.base_sha,
        head_sha=workspace.head_sha,
        status=workspace.status,
        from_cache=from_cache,
        failure_code=workspace.failure_code,
        failure_message=workspace.failure_message,
    )
