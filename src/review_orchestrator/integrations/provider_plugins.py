"""Provider plugin discovery and the shared application composition root."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol

from review_orchestrator.infrastructure.config import Settings
from review_orchestrator.integrations.github import GitHubAdapter, create_github_client
from review_orchestrator.integrations.gitlab import GitLabAdapter, GitLabClient
from review_orchestrator.integrations.providers import (
    ProviderDescriptor,
    ProviderRegistry,
    ProviderRuntime,
)

PROVIDER_ENTRY_POINT_GROUP = "review_orchestrator.providers"


@dataclass(frozen=True)
class ProviderBuildContext:
    settings: Settings
    client_overrides: Mapping[str, Any] = field(default_factory=dict)
    diagnostics_transport: Any | None = None


class ProviderPlugin(Protocol):
    provider: str
    kind: str
    display_name: str

    def build(self, context: ProviderBuildContext) -> ProviderRuntime: ...


class GitHubProviderPlugin:
    provider = "github"
    kind = "github"
    display_name = "GitHub"

    def build(self, context: ProviderBuildContext) -> ProviderRuntime:
        injected = context.client_overrides.get(self.provider)
        client = injected or create_github_client(context.settings)
        adapter = GitHubAdapter(
            client,
            settings=context.settings,
            diagnostics_transport=context.diagnostics_transport,
        )
        return ProviderRuntime(
            adapter=adapter,
            descriptor=ProviderDescriptor(
                key=self.provider,
                kind=self.kind,
                display_name=self.display_name,
            ),
            close=None if injected is not None else client.aclose,
        )


class GitLabProviderPlugin:
    provider = "gitlab"
    kind = "gitlab"
    display_name = "GitLab"

    def build(self, context: ProviderBuildContext) -> ProviderRuntime:
        client = context.client_overrides.get(self.provider) or GitLabClient(
            api_base_url=context.settings.gitlab_api_base_url,
            token=context.settings.gitlab_api_token,
            timeout=context.settings.provider_api_timeout_seconds,
        )
        return ProviderRuntime(
            adapter=GitLabAdapter(
                client,
                settings=context.settings,
                diagnostics_transport=context.diagnostics_transport,
            ),
            descriptor=ProviderDescriptor(
                key=self.provider,
                kind=self.kind,
                display_name=self.display_name,
            ),
        )


def builtin_provider_plugins() -> tuple[ProviderPlugin, ...]:
    return (GitHubProviderPlugin(), GitLabProviderPlugin())


def discover_provider_plugins(
    enabled: Iterable[str] | None = None,
) -> tuple[ProviderPlugin, ...]:
    """Load installed third-party plugins without changing application code."""

    selected = set(enabled) if enabled is not None else None
    discovered: list[ProviderPlugin] = []
    for entry_point in entry_points().select(group=PROVIDER_ENTRY_POINT_GROUP):
        if selected is not None and entry_point.name.lower() not in selected:
            continue
        loaded = entry_point.load()
        plugin = loaded() if isinstance(loaded, type) else loaded
        if plugin.provider.lower() != entry_point.name.lower():
            raise ValueError(
                f"Provider entry point {entry_point.name!r} loaded plugin "
                f"{plugin.provider!r}; the keys must match."
            )
        discovered.append(plugin)
    return tuple(discovered)


def enabled_provider_keys(settings: Settings) -> tuple[str, ...]:
    keys = tuple(
        dict.fromkeys(
            key.strip().lower()
            for key in settings.providers_enabled.split(",")
            if key.strip()
        )
    )
    return keys


def create_provider_registry(
    settings: Settings,
    *,
    plugins: Iterable[ProviderPlugin] | None = None,
    client_overrides: Mapping[str, Any] | None = None,
    diagnostics_transport: Any | None = None,
) -> ProviderRegistry:
    enabled = enabled_provider_keys(settings)
    available_plugins = (
        tuple(plugins)
        if plugins is not None
        else (*builtin_provider_plugins(), *discover_provider_plugins(enabled))
    )
    catalog: dict[str, ProviderPlugin] = {}
    for plugin in available_plugins:
        key = plugin.provider.lower()
        if key in catalog:
            raise ValueError(f"Provider plugin {key!r} is registered more than once.")
        catalog[key] = plugin

    unknown = [key for key in enabled if key not in catalog]
    if unknown:
        raise ValueError(
            "Enabled provider plugins are not installed: " + ", ".join(unknown)
        )

    context = ProviderBuildContext(
        settings=settings,
        client_overrides=client_overrides or {},
        diagnostics_transport=diagnostics_transport,
    )
    return ProviderRegistry(runtimes=[catalog[key].build(context) for key in enabled])
