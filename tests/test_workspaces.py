import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.main import create_app
from review_orchestrator.schemas import WorkspacePrepareRequest
from review_orchestrator.workspaces import (
    WorkspacePaths,
    _git_env,
    _mask_secret,
    _prepare_git_workspace,
    prepare_workspace,
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
                github_client=github_client,
            )
    finally:
        await engine.dispose()

    assert result.status == "ready"
    assert github_client.repositories == ["example/repo"]
