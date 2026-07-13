from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from review_orchestrator.config import Settings
from review_orchestrator.github import GitHubClient, create_github_client
from review_orchestrator.github_auth import (
    GitHubAppTokenProvider,
    GitHubAuthenticationError,
    StaticGitHubTokenProvider,
)


def test_app_id_and_private_key_must_be_configured_together() -> None:
    with pytest.raises(GitHubAuthenticationError, match="must be configured together"):
        create_github_client(
            Settings(github_app_id="123", github_private_key_path=None)
        )

    with pytest.raises(GitHubAuthenticationError, match="must be configured together"):
        create_github_client(
            Settings(github_app_id=None, github_private_key_path="app.pem")
        )


def test_missing_private_key_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(GitHubAuthenticationError, match="Unable to read.*private key"):
        GitHubAppTokenProvider(
            app_id="123",
            private_key_path=str(tmp_path / "missing.pem"),
        )


def test_invalid_private_key_fails_before_service_starts(tmp_path: Path) -> None:
    private_key = tmp_path / "invalid.pem"
    private_key.write_text("not-a-private-key", encoding="utf-8")

    with pytest.raises(GitHubAuthenticationError, match="Unable to create.*JWT"):
        GitHubAppTokenProvider(
            app_id="123",
            private_key_path=str(private_key),
        )


@dataclass
class FakeInstallation:
    id: int
    permissions: dict[str, str] = field(
        default_factory=lambda: {
            "contents": "read",
            "issues": "write",
            "pull_requests": "write",
        }
    )


class FakeAppAuth:
    def __init__(self, app_id: str, private_key: str) -> None:
        self.app_id = app_id
        self.private_key = private_key

    @property
    def token(self) -> str:
        return "signed-app-jwt"

    def get_installation_auth(
        self,
        installation_id: int,
        *,
        requester: object,
    ) -> FakeInstallationAuth:
        return FakeInstallationAuth(self, installation_id, requester)


class FakeInstallationAuth:
    token_reads: list[int] = []

    def __init__(
        self,
        app_auth: FakeAppAuth,
        installation_id: int,
        requester: object,
    ) -> None:
        del app_auth, requester
        self.installation_id = installation_id

    @property
    def token(self) -> str:
        self.token_reads.append(self.installation_id)
        return f"installation-{self.installation_id}-read-{len(self.token_reads)}"


class FakeIntegration:
    instances: list[FakeIntegration] = []

    def __init__(self, *, auth: FakeAppAuth, base_url: str) -> None:
        self.auth = auth
        self.base_url = base_url
        self.requester = object()
        self.installation_calls: list[tuple[str, str]] = []
        self.installation_id_calls: list[int] = []
        self.closed = False
        self.instances.append(self)

    def get_installation(self, owner: str, repo: str) -> FakeInstallation:
        self.installation_calls.append((owner, repo))
        installations = {"alpha": 101, "beta": 202}
        return FakeInstallation(installations[repo])

    def get_app_installation(self, installation_id: int) -> FakeInstallation:
        self.installation_id_calls.append(installation_id)
        return FakeInstallation(installation_id)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sdk(monkeypatch):
    from review_orchestrator import github_auth

    FakeIntegration.instances.clear()
    FakeInstallationAuth.token_reads.clear()
    monkeypatch.setattr(github_auth.Auth, "AppAuth", FakeAppAuth)
    monkeypatch.setattr(github_auth, "GithubIntegration", FakeIntegration)


async def test_app_provider_resolves_and_caches_installations_per_repository(
    tmp_path: Path,
    fake_sdk,
) -> None:
    private_key = tmp_path / "app.pem"
    private_key.write_text("test-private-key", encoding="utf-8")
    provider = GitHubAppTokenProvider(
        app_id="123",
        private_key_path=str(private_key),
        api_base_url="https://github.example/api/v3",
    )

    first = await provider.get_token("Acme/alpha")
    refreshed = await provider.get_token("acme/alpha")
    other = await provider.get_token("acme/beta")
    permissions = await provider.get_permissions("acme/alpha")

    integration = FakeIntegration.instances[0]
    assert integration.base_url == "https://github.example/api/v3"
    assert integration.installation_calls == [("Acme", "alpha"), ("acme", "beta")]
    assert first == "installation-101-read-1"
    assert refreshed == "installation-101-read-2"
    assert other == "installation-202-read-3"
    assert permissions == {
        "contents": "read",
        "issues": "write",
        "pull_requests": "write",
    }
    assert integration.installation_id_calls == [101]
    assert FakeInstallationAuth.token_reads == [101, 101, 202]

    await provider.aclose()
    assert integration.closed is True


async def test_fixed_installation_id_skips_repository_lookup(
    tmp_path: Path,
    fake_sdk,
) -> None:
    private_key = tmp_path / "app.pem"
    private_key.write_text("test-private-key", encoding="utf-8")
    provider = GitHubAppTokenProvider(
        app_id="123",
        private_key_path=str(private_key),
        installation_id=303,
    )

    assert await provider.get_token("any/repository") == "installation-303-read-1"
    assert await provider.get_permissions("any/repository") == {
        "contents": "read",
        "issues": "write",
        "pull_requests": "write",
    }
    integration = FakeIntegration.instances[0]
    assert integration.installation_calls == []
    assert integration.installation_id_calls == [303]


class RecordingTokenProvider:
    def __init__(self) -> None:
        self.repositories: list[str] = []
        self.closed = False

    async def get_token(self, repo_full_name: str) -> str:
        self.repositories.append(repo_full_name)
        return f"token-{len(self.repositories)}"

    async def get_permissions(self, repo_full_name: str) -> None:
        del repo_full_name
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeAsyncClient:
    authorization_headers: list[str | None] = []

    def __init__(self, *, headers: dict[str, str], **kwargs) -> None:
        del kwargs
        self.headers = headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        del kwargs
        self.authorization_headers.append(self.headers.get("Authorization"))
        request = httpx.Request(method, f"https://api.github.com{path}")
        return httpx.Response(200, json={"number": 1}, request=request)


async def test_github_client_gets_fresh_provider_token_for_every_api_request(
    monkeypatch,
) -> None:
    from review_orchestrator import github

    FakeAsyncClient.authorization_headers.clear()
    monkeypatch.setattr(github.httpx, "AsyncClient", FakeAsyncClient)
    provider = RecordingTokenProvider()
    client = GitHubClient(token_provider=provider)

    await client.get_pull_request("acme/repo", 1)
    await client.get_pull_request("acme/repo", 1)
    await client.aclose()

    assert provider.repositories == ["acme/repo", "acme/repo"]
    assert FakeAsyncClient.authorization_headers == ["Bearer token-1", "Bearer token-2"]
    assert provider.closed is True


async def test_static_token_mode_remains_supported() -> None:
    provider = StaticGitHubTokenProvider("legacy-token")
    client = GitHubClient(token_provider=provider)

    assert await client.get_token("acme/repo") == "legacy-token"
