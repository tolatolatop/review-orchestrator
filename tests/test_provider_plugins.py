import json
from pathlib import Path

from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app
from review_orchestrator.provider_plugins import (
    ProviderBuildContext,
    create_provider_registry,
)
from review_orchestrator.providers import (
    ParsedProviderWebhook,
    ProviderDescriptor,
    ProviderRegistry,
    ProviderRuntime,
    ProviderWebhookEvent,
    PullRequestSnapshot,
    WebhookCapability,
)


class ForgeAdapter:
    provider = "forge"

    def parse_webhook(self, *, headers, raw_body, settings):
        del headers, settings
        payload = json.loads(raw_body)
        snapshot = PullRequestSnapshot(
            repository=payload["repository"],
            number=payload["number"],
            base_sha=payload["base_sha"],
            head_sha=payload["head_sha"],
            title=payload["title"],
            author_login=payload["author"],
            base_ref="main",
            head_ref="feature",
            base_repo_full_name=payload["repository"],
            head_repo_full_name=payload["repository"],
            status="open",
            html_url="https://forge.example/acme/repo/pulls/7",
        )
        return ParsedProviderWebhook(
            delivery_id=payload["delivery_id"],
            provider_event=ProviderWebhookEvent(
                provider=self.provider,
                provider_event="pull_request",
                provider_action="opened",
                internal_event="pr_opened",
                repository=snapshot.repository,
                pull_request_number=snapshot.number,
                head_sha=snapshot.head_sha,
                should_update_context=True,
                should_create_review_run=True,
                should_create_agent_task=False,
                status="received",
                pull_request=snapshot,
            ),
            payload=payload,
            raw_body=raw_body,
        )


class ForgePlugin:
    provider = "forge"
    kind = "forgejo"
    display_name = "Internal Forge"

    def __init__(self) -> None:
        self.closed = False

    def build(self, context: ProviderBuildContext) -> ProviderRuntime:
        assert context.settings.providers_enabled == "forge"

        async def close() -> None:
            self.closed = True

        return ProviderRuntime(
            adapter=ForgeAdapter(),
            descriptor=ProviderDescriptor(
                key=self.provider,
                kind=self.kind,
                display_name=self.display_name,
            ),
            close=close,
        )


async def test_external_plugin_builds_from_configuration_and_owns_lifecycle() -> None:
    plugin = ForgePlugin()
    registry = create_provider_registry(
        Settings(_env_file=None, providers_enabled="forge"),
        plugins=[plugin],
    )

    assert registry.require("forge").provider == "forge"
    assert registry.capability("forge", WebhookCapability) is not None
    assert registry.capabilities("forge") == frozenset({"webhook"})
    assert registry.descriptors() == [
        ProviderDescriptor(
            key="forge",
            kind="forgejo",
            display_name="Internal Forge",
        )
    ]

    await registry.aclose()
    await registry.aclose()
    assert plugin.closed is True


def test_gitlab_only_application_does_not_construct_github(
    monkeypatch,
    tmp_path,
) -> None:
    from review_orchestrator.integrations import provider_plugins

    def fail_if_github_is_constructed(settings):
        del settings
        raise AssertionError("GitHub must not be initialized in GitLab-only mode")

    monkeypatch.setattr(
        provider_plugins,
        "create_github_client",
        fail_if_github_is_constructed,
    )
    settings = Settings(
        _env_file=None,
        providers_enabled="gitlab",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/gitlab-only.db",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/providers")

    assert response.status_code == 200
    assert response.json()["items"][0]["key"] == "gitlab"
    assert [item["key"] for item in response.json()["items"]] == ["gitlab"]


def test_third_party_webhook_needs_no_application_branch(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/forge.db",
    )
    payload = {
        "delivery_id": "forge-delivery-1",
        "repository": "acme/repo",
        "number": 7,
        "title": "Add provider plugins",
        "author": "alice",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with TestClient(create_app(settings)) as client:
        client.app.state.provider_registry = ProviderRegistry([ForgeAdapter()])
        accepted = client.post(
            "/api/v1/webhooks/forge",
            content=json.dumps(payload),
        )
        runs = client.get(
            "/api/v1/review-runs",
            params={"provider": "forge"},
        )

    assert accepted.status_code == 200
    assert accepted.json()["provider"] == "forge"
    assert accepted.json()["review_run_id"] is not None
    assert runs.status_code == 200
    assert runs.json()["items"][0]["provider"] == "forge"
    assert runs.json()["items"][0]["pull_request_context"]["title"] == (
        "Add provider plugins"
    )
