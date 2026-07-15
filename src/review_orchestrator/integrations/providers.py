"""Provider-neutral capabilities, runtime metadata, and registry."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from review_orchestrator.domain.models import (
        AgentTask,
        PullRequestContext,
        ReviewCommentRef,
        ReviewRun,
    )
    from review_orchestrator.domain.review_results import ChangedFile
    from review_orchestrator.infrastructure.config import Settings


class ProviderWebhookError(Exception):
    status_code = 400
    error_code = "provider_webhook_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProviderSignatureError(ProviderWebhookError):
    status_code = 401
    error_code = "provider_signature_invalid"


class ProviderPayloadError(ProviderWebhookError):
    error_code = "provider_payload_invalid"


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.operation = operation


class ProviderCapabilityError(ProviderError):
    """Raised when an adapter cannot perform a configured operation."""


class ProviderOperationError(ProviderError):
    """Provider-neutral boundary for failures returned by a platform client."""


@dataclass(frozen=True)
class ProviderWorkspaceCheckout:
    """Provider-resolved repository location and ephemeral Git credentials."""

    clone_url: str
    auth_token: str | None = None
    auth_username: str = "oauth2"


@dataclass(frozen=True)
class PullRequestSnapshot:
    """Complete provider-neutral pull/merge-request state from a webhook."""

    repository: str
    number: int
    head_sha: str
    provider_repo_id: str | None = None
    provider_pr_id: str | None = None
    title: str | None = None
    author_login: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    head_ref: str | None = None
    base_repo_full_name: str | None = None
    head_repo_full_name: str | None = None
    status: str = "open"
    html_url: str | None = None
    closed_at: datetime | None = None
    merged_at: datetime | None = None


@dataclass(frozen=True)
class AgentCommand:
    source_kind: str
    source_comment_id: str
    source_url: str | None
    author_login: str
    author_association: str | None
    command_text: str


@dataclass(frozen=True)
class ProviderWebhookEvent:
    provider: str
    provider_event: str
    provider_action: str | None
    internal_event: str | None
    repository: str | None
    pull_request_number: int | None
    head_sha: str | None
    should_update_context: bool
    should_create_review_run: bool
    should_create_agent_task: bool
    status: str
    agent_command: AgentCommand | None = None
    pull_request: PullRequestSnapshot | None = None


@dataclass(frozen=True)
class ParsedProviderWebhook:
    delivery_id: str
    provider_event: ProviderWebhookEvent
    payload: dict[str, Any]
    raw_body: bytes


@runtime_checkable
class ProviderAdapter(Protocol):
    """Minimum identity shared by all provider capability implementations."""

    provider: str


@runtime_checkable
class WebhookCapability(ProviderAdapter, Protocol):
    def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        settings: Settings,
    ) -> ParsedProviderWebhook: ...


@runtime_checkable
class PullRequestCapability(ProviderAdapter, Protocol):
    async def get_pull_request_context(
        self,
        task: AgentTask,
    ) -> PullRequestContext | None: ...


@runtime_checkable
class WorkspaceCheckoutCapability(ProviderAdapter, Protocol):
    async def get_workspace_checkout(
        self,
        repo_full_name: str,
        *,
        clone_url: str | None = None,
    ) -> ProviderWorkspaceCheckout: ...


@runtime_checkable
class ChangedFilesCapability(ProviderAdapter, Protocol):
    async def list_changed_files(
        self,
        review_run: ReviewRun,
    ) -> list[ChangedFile]: ...


@runtime_checkable
class ReviewSummaryCapability(ProviderAdapter, Protocol):
    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None = None,
    ) -> ReviewCommentRef | None: ...


@runtime_checkable
class LineCommentsCapability(ProviderAdapter, Protocol):
    async def publish_line_comments(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        changed_files: list[ChangedFile],
    ) -> dict[str, int]: ...


@runtime_checkable
class AgentTaskCommentsCapability(ProviderAdapter, Protocol):
    async def publish_agent_task_comment(
        self,
        session: AsyncSession,
        task: AgentTask,
        *,
        state: str,
    ) -> str: ...


@runtime_checkable
class PlatformDiagnosticsCapability(ProviderAdapter, Protocol):
    async def diagnose_permissions(self, payload: Any) -> Any: ...


@runtime_checkable
class ResourceLinksCapability(ProviderAdapter, Protocol):
    def agent_task_comment_url(self, task: AgentTask) -> str | None: ...


CAPABILITY_PROTOCOLS: dict[str, type[ProviderAdapter]] = {
    "webhook": WebhookCapability,
    "pull_request": PullRequestCapability,
    "workspace_checkout": WorkspaceCheckoutCapability,
    "changed_files": ChangedFilesCapability,
    "review_summary": ReviewSummaryCapability,
    "line_comments": LineCommentsCapability,
    "agent_task_comments": AgentTaskCommentsCapability,
    "diagnostics": PlatformDiagnosticsCapability,
    "resource_links": ResourceLinksCapability,
}


@dataclass(frozen=True)
class ProviderDescriptor:
    key: str
    kind: str
    display_name: str


ProviderCloser = Callable[[], Awaitable[None]]


@dataclass
class ProviderRuntime:
    """A configured provider instance and the resources owned by it."""

    adapter: ProviderAdapter
    descriptor: ProviderDescriptor | None = None
    close: ProviderCloser | None = None

    def __post_init__(self) -> None:
        if self.descriptor is None:
            key = self.adapter.provider
            self.descriptor = ProviderDescriptor(
                key=key,
                kind=key,
                display_name=key.replace("-", " ").title(),
            )


CapabilityT = TypeVar("CapabilityT", bound=ProviderAdapter)


class ProviderRegistry:
    def __init__(
        self,
        adapters: Iterable[ProviderAdapter] = (),
        *,
        runtimes: Iterable[ProviderRuntime] = (),
    ) -> None:
        resolved = [ProviderRuntime(adapter) for adapter in adapters]
        resolved.extend(runtimes)
        self._runtimes: dict[str, ProviderRuntime] = {}
        for runtime in resolved:
            key = runtime.adapter.provider
            if key in self._runtimes:
                raise ValueError(f"Provider {key!r} is registered more than once.")
            if runtime.descriptor is None or runtime.descriptor.key != key:
                raise ValueError("Provider descriptor key must match adapter.provider.")
            self._runtimes[key] = runtime
        self._closed = False

    def get(self, provider: str) -> ProviderAdapter | None:
        runtime = self._runtimes.get(provider)
        return runtime.adapter if runtime is not None else None

    def require(self, provider: str) -> ProviderAdapter:
        adapter = self.get(provider)
        if adapter is None:
            raise KeyError(provider)
        return adapter

    def capability(
        self,
        provider: str,
        capability: type[CapabilityT],
    ) -> CapabilityT | None:
        adapter = self.get(provider)
        return (
            adapter
            if adapter is not None and isinstance(adapter, capability)
            else None
        )

    def require_capability(
        self,
        provider: str,
        capability: type[CapabilityT],
        *,
        operation: str,
    ) -> CapabilityT:
        adapter = self.capability(provider, capability)
        if adapter is None:
            raise ProviderCapabilityError(
                f"Provider {provider!r} does not support {operation}.",
                provider=provider,
                operation=operation,
            )
        return adapter

    def capabilities(self, provider: str) -> frozenset[str]:
        adapter = self.get(provider)
        if adapter is None:
            return frozenset()
        return frozenset(
            name
            for name, protocol in CAPABILITY_PROTOCOLS.items()
            if isinstance(adapter, protocol)
        )

    def descriptors(self) -> list[ProviderDescriptor]:
        return [
            runtime.descriptor
            for runtime in self._runtimes.values()
            if runtime.descriptor is not None
        ]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for runtime in reversed(list(self._runtimes.values())):
            if runtime.close is not None:
                await runtime.close()

    async def __aenter__(self) -> ProviderRegistry:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def payload_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
