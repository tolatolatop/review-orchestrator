from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app
from review_orchestrator.platform_diagnostics import diagnose_platform_permissions
from review_orchestrator.schemas import PlatformPermissionDiagnosticRequest


class FakeGitHubAppClient:
    async def get_token(self, repo_full_name: str) -> str:
        assert repo_full_name == "example/repo"
        return "installation-secret"

    async def get_permissions(self, repo_full_name: str) -> dict[str, str]:
        assert repo_full_name == "example/repo"
        return {
            "contents": "read",
            "issues": "write",
            "metadata": "read",
            "pull_requests": "write",
        }


async def test_github_diagnostic_verifies_read_and_classic_scope_writes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer github-secret"
        if request.url.path == "/repos/example/repo":
            return httpx.Response(
                200,
                headers={
                    "X-OAuth-Scopes": "repo, read:org",
                    "X-RateLimit-Remaining": "4999",
                },
                json={"permissions": {"push": True, "pull": True}},
            )
        if request.url.path == "/repos/example/repo/pulls/42":
            return httpx.Response(200, json={"number": 42})
        raise AssertionError(f"Unexpected request: {request.url}")

    result = await diagnose_platform_permissions(
        Settings(github_installation_token="github-secret"),
        PlatformPermissionDiagnosticRequest(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
        ),
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "healthy"
    assert result.reported_scopes == ["read:org", "repo"]
    assert result.repository_role == "push"
    assert result.rate_limit_remaining == 4999
    assert {check.status for check in result.checks} == {"passed"}


async def test_github_fine_grained_write_permissions_are_not_overstated() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"permissions": {"pull": True}},
        )
    )
    result = await diagnose_platform_permissions(
        Settings(github_installation_token="fine-grained-secret"),
        PlatformPermissionDiagnosticRequest(
            provider="github",
            repo_full_name="example/repo",
        ),
        transport=transport,
    )

    checks = {check.name: check for check in result.checks}
    assert result.status == "degraded"
    assert checks["repository_read"].status == "passed"
    assert checks["pull_request_read"].status == "skipped"
    assert checks["summary_comment_write"].status == "unknown"
    assert checks["line_comment_write"].status == "unknown"


async def test_github_app_diagnostic_uses_dynamic_token_and_permissions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer installation-secret"
        if request.url.path == "/repos/example/repo":
            return httpx.Response(
                200,
                headers={"X-RateLimit-Remaining": "4998"},
                json={"permissions": {"pull": False}},
            )
        if request.url.path == "/repos/example/repo/pulls/42":
            return httpx.Response(200, json={"number": 42})
        raise AssertionError(f"Unexpected request: {request.url}")

    result = await diagnose_platform_permissions(
        Settings(github_installation_token=None),
        PlatformPermissionDiagnosticRequest(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
        ),
        transport=httpx.MockTransport(handler),
        github_client=FakeGitHubAppClient(),
    )

    checks = {check.name: check for check in result.checks}
    assert result.status == "healthy"
    assert result.repository_role == "installation"
    assert result.reported_scopes == [
        "contents:read",
        "issues:write",
        "metadata:read",
        "pull_requests:write",
    ]
    assert checks["contents_read"].status == "passed"
    assert checks["summary_comment_write"].status == "passed"
    assert checks["line_comment_write"].status == "passed"


async def test_gitlab_diagnostic_uses_project_role_and_api_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["private-token"] == "gitlab-secret"
        path = request.url.raw_path.decode()
        if path == "/api/v4/projects/group%2Frepo":
            return httpx.Response(
                200,
                headers={"X-OAuth-Scopes": "api"},
                json={
                    "permissions": {
                        "project_access": {"access_level": 30},
                    }
                },
            )
        if path == "/api/v4/projects/group%2Frepo/merge_requests/7":
            return httpx.Response(200, json={"iid": 7})
        raise AssertionError(f"Unexpected request: {request.url}")

    result = await diagnose_platform_permissions(
        Settings(gitlab_api_token="gitlab-secret"),
        PlatformPermissionDiagnosticRequest(
            provider="gitlab",
            repo_full_name="group/repo",
            pull_request_number=7,
        ),
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "healthy"
    assert result.repository_role == "developer"
    assert all(check.status == "passed" for check in result.checks)


async def test_diagnostic_failure_does_not_expose_upstream_body_or_token() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            401,
            json={"message": "bad token github-secret"},
        )
    )
    result = await diagnose_platform_permissions(
        Settings(github_installation_token="github-secret"),
        PlatformPermissionDiagnosticRequest(
            provider="github",
            repo_full_name="example/repo",
        ),
        transport=transport,
    )

    serialized = result.model_dump_json()
    assert result.status == "failed"
    assert "github-secret" not in serialized
    assert "bad token" not in serialized
    assert "HTTP 401" in serialized


def test_platform_permission_diagnostic_endpoint_supports_injected_probe(
    tmp_path: Path,
) -> None:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    app = create_app(settings)

    async def probe(
        probe_settings: Settings,
        payload: PlatformPermissionDiagnosticRequest,
    ):
        assert probe_settings is settings
        return await diagnose_platform_permissions(probe_settings, payload)

    with TestClient(app) as client:
        app.state.platform_permission_probe = probe
        response = client.post(
            "/api/v1/diagnostics/platform-permissions",
            json={"provider": "github", "repo_full_name": "example/repo"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["token_configured"] is False
    assert response.json()["checks"][0]["name"] == "token_configured"


def test_platform_permission_diagnostic_rejects_unsafe_repository_path(
    tmp_path: Path,
) -> None:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/diagnostics/platform-permissions",
            json={"provider": "github", "repo_full_name": "../rate_limit"},
        )

    assert response.status_code == 422
