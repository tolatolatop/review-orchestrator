import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import GitHubAdapter, GitHubClientError
from review_orchestrator.main import create_app
from review_orchestrator.providers import (
    ProviderRegistry,
    ProviderWorkspaceCheckout,
)
from review_orchestrator.schemas import WorkspacePrepareRequest
from review_orchestrator.workspaces import (
    GitCommandError,
    WorkspaceErrorCode,
    WorkspacePaths,
    _authenticated_https_base,
    _classify_git_error,
    _git_env,
    _mask_secret,
    _prepare_git_workspace,
    _run_git,
    _safe_clone_url,
    cleanup_workspace,
    get_workspace,
    lease_workspace,
    prepare_workspace,
    release_workspace,
    repo_hash,
    workspace_identity,
)


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    return TestClient(create_app(settings))


def run(command: list[str], cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_source_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "source-repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "agent@example.com"], repo)
    run(["git", "config", "user.name", "Agent"], repo)

    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    run(["git", "add", "app.py"], repo)
    run(["git", "commit", "-m", "base"], repo)
    base_sha = run(["git", "rev-parse", "HEAD"], repo)

    (repo / "app.py").write_text("print('head')\n", encoding="utf-8")
    run(["git", "commit", "-am", "head"], repo)
    head_sha = run(["git", "rev-parse", "HEAD"], repo)

    return repo, base_sha, head_sha


def workspace_payload(source_repo: Path, base_sha: str, head_sha: str) -> dict:
    return {
        "provider": "github",
        "repository": {
            "full_name": "example/repo",
            "clone_url": str(source_repo),
        },
        "pull_request": {
            "number": 42,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "is_fork": False,
        },
        "options": {
            "use_git_cache": True,
            "force_refresh": False,
            "enable_submodules": False,
            "enable_lfs": False,
        },
    }


def test_prepare_workspace_checkouts_head_and_is_idempotent(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)

    with make_client(tmp_path) as client:
        first = client.post("/api/v1/workspaces/prepare", json=payload)
        second = client.post("/api/v1/workspaces/prepare", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["workspace_id"] == second.json()["workspace_id"]
    workspace_path = Path(first.json()["workspace_path"])
    assert workspace_path.exists()
    assert workspace_path.stat().st_mode & 0o777 == 0o700
    assert run(["git", "rev-parse", "HEAD"], workspace_path) == head_sha
    assert run(["git", "cat-file", "-t", base_sha], workspace_path) == "commit"


def test_prepare_workspace_can_use_no_git_cache(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)
    payload["options"]["use_git_cache"] = False

    with make_client(tmp_path) as client:
        response = client.post("/api/v1/workspaces/prepare", json=payload)
        workspace = client.get(f"/api/v1/workspaces/{response.json()['workspace_id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert workspace.status_code == 200
    assert workspace.json()["cache_path"] is None


def test_workspace_lease_blocks_cleanup_until_release(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)

    with make_client(tmp_path) as client:
        prepared = client.post("/api/v1/workspaces/prepare", json=payload).json()
        workspace_id = prepared["workspace_id"]
        lease = client.post(
            f"/api/v1/workspaces/{workspace_id}/lease",
            json={"review_run_id": "run-1", "session_id": "session-1"},
        )
        blocked = client.post(
            f"/api/v1/workspaces/{workspace_id}/cleanup",
            json={"force": False},
        )
        release = client.post(
            f"/api/v1/workspace-leases/{lease.json()['lease_id']}/release"
        )
        cleaned = client.post(
            f"/api/v1/workspaces/{workspace_id}/cleanup",
            json={"force": False},
        )

    assert lease.status_code == 200
    assert lease.json()["status"] == "leased"
    assert blocked.status_code == 409
    assert release.status_code == 200
    assert release.json()["status"] == "idle"
    assert cleaned.status_code == 200
    assert cleaned.json()["status"] == "deleted"
    assert not Path(prepared["workspace_path"]).exists()


def test_cleanup_pull_request_workspaces_deletes_matching_workspaces(
    tmp_path: Path,
) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)

    with make_client(tmp_path) as client:
        prepared = client.post("/api/v1/workspaces/prepare", json=payload).json()
        cleanup = client.post(
            "/api/v1/workspaces/cleanup/pr",
            json={
                "provider": "github",
                "repository": "example/repo",
                "pull_request_number": 42,
                "force": False,
            },
        )
        workspace = client.get(f"/api/v1/workspaces/{prepared['workspace_id']}")

    assert cleanup.status_code == 200
    assert cleanup.json()["deleted"] == 1
    assert workspace.status_code == 200
    assert workspace.json()["status"] == "deleted"


def test_prepare_workspace_returns_stable_failure_for_missing_head(
    tmp_path: Path,
) -> None:
    source_repo, base_sha, _head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, "c" * 40)

    with make_client(tmp_path) as client:
        response = client.post("/api/v1/workspaces/prepare", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure_code"] == "head_missing"


def test_dynamic_token_is_injected_only_through_temporary_git_config() -> None:
    request = WorkspacePrepareRequest.model_validate(
        {
            "repository": {
                "full_name": "acme/private-repo",
                "clone_url": "https://github.example/acme/private-repo.git",
            },
            "pull_request": {
                "number": 1,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
        }
    )

    env = _git_env(request, auth_token="installation-secret")

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_VALUE_0"] == "https://github.example/"
    assert env["GIT_CONFIG_KEY_0"] == (
        "url.https://x-access-token:installation-secret@github.example/.insteadOf"
    )
    assert env["REVIEW_GIT_TOKEN_MASK"] == "installation-secret"
    assert "installation-secret" not in request.model_dump_json()


def test_git_error_output_masks_dynamic_token() -> None:
    env = {"REVIEW_GIT_TOKEN_MASK": "installation-secret"}
    value = "fatal: authentication failed for installation-secret"

    assert _mask_secret(value, env) == "fatal: authentication failed for ***"


def test_prepare_git_workspace_passes_token_in_environment_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = WorkspacePrepareRequest.model_validate(
        {
            "repository": {
                "full_name": "acme/private-repo",
                "clone_url": "https://github.com/acme/private-repo.git",
            },
            "pull_request": {
                "number": 1,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            "options": {"use_git_cache": False},
        }
    )
    paths = WorkspacePaths(
        repo_hash="repo-hash",
        repo_path=tmp_path / "workspace" / "repo",
        cache_path=tmp_path / "cache.git",
    )
    calls: list[tuple[list[str], dict[str, str]]] = []

    def record_git(command: list[str], *, env: dict[str, str]) -> None:
        calls.append((command, env))

    monkeypatch.setattr("review_orchestrator.workspaces._run_git", record_git)
    _prepare_git_workspace(paths, request, "installation-secret")

    assert calls
    assert all("installation-secret" not in " ".join(command) for command, _ in calls)
    assert all(
        env["REVIEW_GIT_TOKEN_MASK"] == "installation-secret" for _, env in calls
    )


class FakeWorkspaceGitHubClient:
    def __init__(self) -> None:
        self.api_base_url = "https://api.github.com"
        self.repositories: list[str] = []

    async def get_token(self, repo_full_name: str) -> str:
        self.repositories.append(repo_full_name)
        return "installation-secret"


async def test_prepare_workspace_resolves_dynamic_token_by_repository(
    tmp_path: Path,
) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    request = WorkspacePrepareRequest.model_validate(
        workspace_payload(source_repo, base_sha, head_sha)
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/direct.db",
        workspace_root=str(tmp_path / "direct-workspaces"),
        git_cache_root=str(tmp_path / "direct-cache"),
    )
    engine = create_engine(settings)
    await init_models(engine)
    session_factory = create_session_factory(engine)
    github_client = FakeWorkspaceGitHubClient()
    try:
        async with session_factory() as session:
            result = await prepare_workspace(
                session,
                settings,
                request,
                provider_registry=ProviderRegistry(
                    [GitHubAdapter(github_client)]
                ),
            )
    finally:
        await engine.dispose()

    assert result.status == "ready"
    assert github_client.repositories == ["example/repo"]


class FakeWorkspaceProvider:
    provider = "forge"

    def __init__(self, clone_url: str) -> None:
        self.clone_url = clone_url
        self.repositories: list[str] = []

    async def get_workspace_checkout(
        self,
        repo_full_name: str,
        *,
        clone_url: str | None = None,
    ) -> ProviderWorkspaceCheckout:
        self.repositories.append(repo_full_name)
        return ProviderWorkspaceCheckout(
            clone_url=clone_url or self.clone_url,
            auth_token="forge-secret",
            auth_username="oauth2",
        )


async def test_prepare_workspace_uses_task_provider_checkout(
    tmp_path: Path,
) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    request = WorkspacePrepareRequest.model_validate(
        {
            "provider": "forge",
            "repository": {"full_name": "group/repo"},
            "pull_request": {
                "number": 7,
                "base_sha": base_sha,
                "head_sha": head_sha,
            },
        }
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/forge.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "cache"),
    )
    engine = create_engine(settings)
    await init_models(engine)
    session_factory = create_session_factory(engine)
    provider = FakeWorkspaceProvider(str(source_repo))
    try:
        async with session_factory() as session:
            result = await prepare_workspace(
                session,
                settings,
                request,
                provider_registry=ProviderRegistry([provider]),
            )
    finally:
        await engine.dispose()

    assert result.status == "ready"
    assert provider.repositories == ["group/repo"]
    assert "/forge/" in result.workspace_path
    assert run(["git", "rev-parse", "HEAD"], Path(result.workspace_path)) == head_sha


def test_gitlab_token_uses_provider_specific_git_username() -> None:
    request = WorkspacePrepareRequest.model_validate(
        {
            "provider": "gitlab",
            "repository": {
                "full_name": "group/private-repo",
                "clone_url": "https://gitlab.example/group/private-repo.git",
            },
            "pull_request": {
                "number": 1,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
        }
    )

    env = _git_env(
        request,
        auth_token="gitlab-secret",
        auth_username="oauth2",
    )

    assert env["GIT_CONFIG_KEY_0"] == (
        "url.https://oauth2:gitlab-secret@gitlab.example/.insteadOf"
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("fatal: Authentication failed", WorkspaceErrorCode.auth_failed),
        ("fatal: could not read Username", WorkspaceErrorCode.auth_failed),
        ("remote: Repository not found", WorkspaceErrorCode.repo_not_found),
        ("does not appear to be a git repository", WorkspaceErrorCode.repo_not_found),
        ("fatal: couldn't find remote ref head", WorkspaceErrorCode.head_missing),
        ("fatal: remote error: not our ref", WorkspaceErrorCode.head_missing),
        ("fatal: unable to access repository", WorkspaceErrorCode.network_error),
        ("fatal: Could not resolve host", WorkspaceErrorCode.network_error),
        ("fatal: Not a valid object name", WorkspaceErrorCode.head_missing),
        ("error: pathspec did not match", WorkspaceErrorCode.head_missing),
        ("unexpected git failure", WorkspaceErrorCode.git_error),
    ],
)
def test_git_errors_are_classified_stably(stderr: str, expected: str) -> None:
    assert _classify_git_error(stderr) == expected


def test_workspace_identity_normalizes_repository_case_and_whitespace() -> None:
    normalized_hash = repo_hash("Example/Repo")
    assert normalized_hash == repo_hash("  example/repo  ")
    assert workspace_identity(
        provider="github",
        repository="Example/Repo",
        pull_request_number=42,
        head_sha="b" * 40,
    ) == f"github:{normalized_hash}:pr:42:head:{'b' * 40}"


@pytest.mark.parametrize(
    ("clone_url", "base", "safe"),
    [
        (
            "https://token@github.example:8443/acme/repo.git",
            "github.example:8443/",
            "https://github.example:8443/acme/repo.git",
        ),
        ("ssh://git@github.example/acme/repo.git", None, "ssh://github.example/acme/repo.git"),
        ("/tmp/local-repo", None, "/tmp/local-repo"),
    ],
)
def test_clone_url_helpers_keep_credentials_out_of_diagnostics(
    clone_url: str,
    base: str | None,
    safe: str,
) -> None:
    assert _authenticated_https_base(clone_url) == base
    assert _safe_clone_url(clone_url) == safe


def test_git_env_rejects_missing_token_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_REVIEW_TOKEN", raising=False)
    request = WorkspacePrepareRequest.model_validate(
        {
            "repository": {
                "full_name": "acme/private-repo",
                "clone_url": "https://github.com/acme/private-repo.git",
            },
            "pull_request": {
                "number": 1,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            "auth": {"token_ref": "MISSING_REVIEW_TOKEN"},
        }
    )

    with pytest.raises(GitCommandError) as exc_info:
        _git_env(request)

    assert exc_info.value.code == WorkspaceErrorCode.auth_failed
    assert "MISSING_REVIEW_TOKEN" in exc_info.value.message


def test_run_git_converts_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["git", "fetch"], timeout=120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(GitCommandError) as exc_info:
        _run_git(["git", "fetch"], env={})

    assert exc_info.value.code == WorkspaceErrorCode.network_error
    assert exc_info.value.message == "git command timed out"


def test_workspace_endpoints_cover_missing_and_expired_cleanup(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        missing_get = client.get("/api/v1/workspaces/missing")
        missing_lease = client.post("/api/v1/workspaces/missing/lease", json={})
        missing_release = client.post("/api/v1/workspace-leases/missing/release")
        missing_cleanup = client.post(
            "/api/v1/workspaces/missing/cleanup", json={"force": False}
        )
        expired = client.post("/api/v1/workspaces/cleanup/expired")

    assert missing_get.status_code == 404
    assert missing_lease.status_code == 404
    assert missing_release.status_code == 404
    assert missing_cleanup.status_code == 404
    assert expired.status_code == 200
    assert expired.json() == {"deleted": 0, "skipped_locked": 0, "failed": 0}


def test_prepare_workspace_rejects_unsupported_provider(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)
    payload["provider"] = "unsupported"

    with make_client(tmp_path) as client:
        response = client.post("/api/v1/workspaces/prepare", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure_message"] == (
        "Provider 'unsupported' does not support get_workspace_checkout."
    )


def test_force_refresh_reuses_record_and_refreshes_git_cache(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)

    with make_client(tmp_path) as client:
        first = client.post("/api/v1/workspaces/prepare", json=payload).json()
        refreshed = client.post(
            "/api/v1/workspaces/prepare",
            json={
                **payload,
                "options": {**payload["options"], "force_refresh": True},
            },
        )

    assert refreshed.status_code == 200
    assert refreshed.json()["workspace_id"] == first["workspace_id"]
    assert refreshed.json()["status"] == "ready"
    assert refreshed.json()["from_cache"] is True


def test_releasing_one_of_two_leases_keeps_workspace_leased(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)

    with make_client(tmp_path) as client:
        workspace = client.post("/api/v1/workspaces/prepare", json=payload).json()
        workspace_id = workspace["workspace_id"]
        first = client.post(
            f"/api/v1/workspaces/{workspace_id}/lease",
            json={"session_id": "session-1"},
        ).json()
        second = client.post(
            f"/api/v1/workspaces/{workspace_id}/lease",
            json={"session_id": "session-2"},
        ).json()
        first_release = client.post(
            f"/api/v1/workspace-leases/{first['lease_id']}/release"
        )
        second_release = client.post(
            f"/api/v1/workspace-leases/{second['lease_id']}/release"
        )

    assert first_release.status_code == 200
    assert first_release.json()["status"] == "leased"
    assert second_release.status_code == 200
    assert second_release.json()["status"] == "idle"


def test_cleanup_failure_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("filesystem is read-only")

    with make_client(tmp_path) as client:
        workspace = client.post("/api/v1/workspaces/prepare", json=payload).json()
        monkeypatch.setattr(
            "review_orchestrator.workspaces.shutil.rmtree",
            fail_cleanup,
        )
        response = client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/cleanup",
            json={"force": True},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure_code"] == WorkspaceErrorCode.cleanup_failed
    assert response.json()["failure_message"] == "filesystem is read-only"


def test_expired_cleanup_deletes_only_expired_workspace(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    payload = workspace_payload(source_repo, base_sha, head_sha)

    async def expire_workspace(client: TestClient, workspace_id: str) -> None:
        from review_orchestrator.models import utc_now

        async with client.app.state.session_factory() as session:
            result = await get_workspace(session, workspace_id)
            assert result is not None
            result.status = "idle"
            result.expires_at = utc_now()
            await session.commit()

    with make_client(tmp_path) as client:
        workspace = client.post("/api/v1/workspaces/prepare", json=payload).json()
        client.portal.call(
            expire_workspace,
            client,
            workspace["workspace_id"],
        )
        response = client.post("/api/v1/workspaces/cleanup/expired")

    assert response.status_code == 200
    assert response.json() == {"deleted": 1, "skipped_locked": 0, "failed": 0}
    assert not Path(workspace["workspace_path"]).exists()


class FailingWorkspaceGitHubClient:
    api_base_url = "https://api.github.com"

    async def get_token(self, _repo_full_name: str) -> str:
        raise GitHubClientError("installation token unavailable")


async def test_prepare_workspace_persists_auth_failure(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    request = WorkspacePrepareRequest.model_validate(
        workspace_payload(source_repo, base_sha, head_sha)
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/auth-failure.db",
        workspace_root=str(tmp_path / "auth-workspaces"),
        git_cache_root=str(tmp_path / "auth-cache"),
    )
    engine = create_engine(settings)
    await init_models(engine)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            result = await prepare_workspace(
                session,
                settings,
                request,
                provider_registry=ProviderRegistry(
                    [GitHubAdapter(FailingWorkspaceGitHubClient())]  # type: ignore[arg-type]
                ),
            )
    finally:
        await engine.dispose()

    assert result.status == "failed"
    assert result.failure_code == WorkspaceErrorCode.auth_failed
    assert result.failure_message == "installation token unavailable"


async def test_workspace_service_lifecycle_with_multiple_leases(tmp_path: Path) -> None:
    source_repo, base_sha, head_sha = make_source_repo(tmp_path)
    request = WorkspacePrepareRequest.model_validate(
        workspace_payload(source_repo, base_sha, head_sha)
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/lifecycle.db",
        workspace_root=str(tmp_path / "lifecycle-workspaces"),
        git_cache_root=str(tmp_path / "lifecycle-cache"),
    )
    engine = create_engine(settings)
    await init_models(engine)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            prepared = await prepare_workspace(session, settings, request)
            first, workspace = await lease_workspace(
                session,
                prepared.workspace_id,
                session_id="session-1",
            )
            second, _ = await lease_workspace(
                session,
                prepared.workspace_id,
                session_id="session-2",
            )
            leased_status = workspace.status
            after_first = await release_workspace(session, first.id)
            after_first_status = after_first.status if after_first else None
            locked = await cleanup_workspace(
                session,
                prepared.workspace_id,
                force=False,
            )
            after_second = await release_workspace(session, second.id)
            after_second_status = after_second.status if after_second else None
            deleted = await cleanup_workspace(
                session,
                prepared.workspace_id,
                force=False,
            )
    finally:
        await engine.dispose()

    assert leased_status == "leased"
    assert after_first_status == "leased"
    assert locked == WorkspaceErrorCode.workspace_locked
    assert after_second_status == "idle"
    assert deleted == "deleted"
