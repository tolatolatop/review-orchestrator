import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app


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
