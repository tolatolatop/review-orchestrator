"""Provider-neutral capabilities, runtime metadata, and registry."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
class Credential:
    """A platform-owned credential returned for one narrow operation scope."""

    value: str
    username: str | None = None
    expires_at: datetime | None = None


@runtime_checkable
class Platform(Protocol):
    """Lowest-level platform API, credential, and client lifecycle boundary."""

    key: str

    async def get_credential(self, target: str, scope: str) -> Credential: ...

    async def aclose(self) -> None: ...


class ProviderContractModel(BaseModel):
    """Strict transport model used by Provider Core and in-process callers."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GitCheckoutRequest(ProviderContractModel):
    repository: str = Field(min_length=1, max_length=512)
    clone_url: str | None = Field(default=None, max_length=2048)


class GitCheckoutTarget(ProviderContractModel):
    remote_url: str
    username: str | None = None
    password: str | None = None
    expires_at: datetime | None = None


CommentKind = Literal["summary", "line", "agent"]


class CommentPublishItem(ProviderContractModel):
    kind: CommentKind
    body: str = Field(min_length=1, max_length=100_000)
    comment_id: str | None = None
    thread_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    path: str | None = Field(default=None, max_length=4096)
    line: int | None = Field(default=None, gt=0)
    commit_sha: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_line_comment(self) -> CommentPublishItem:
        if self.kind == "line" and (
            self.path is None or self.line is None or self.commit_sha is None
        ):
            raise ValueError("line comments require path, line, and commit_sha")
        return self


class CommentPublishRequest(ProviderContractModel):
    repository: str = Field(min_length=1, max_length=512)
    pull_request_number: int = Field(gt=0)
    comments: tuple[CommentPublishItem, ...] = Field(
        default=(),
        max_length=100,
    )
    kind: CommentKind | None = None
    body: str | None = Field(default=None, min_length=1, max_length=100_000)
    comment_id: str | None = None
    thread_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    path: str | None = Field(default=None, max_length=4096)
    line: int | None = Field(default=None, gt=0)
    commit_sha: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def normalize_single_comment(self) -> CommentPublishRequest:
        if self.comments and (self.kind is not None or self.body is not None):
            raise ValueError("use comments or a single kind/body comment, not both")
        if self.comments:
            return self
        if self.kind is None or self.body is None:
            raise ValueError("at least one comment is required")
        item = CommentPublishItem(
            kind=self.kind,
            body=self.body,
            comment_id=self.comment_id,
            thread_id=self.thread_id,
            idempotency_key=self.idempotency_key,
            path=self.path,
            line=self.line,
            commit_sha=self.commit_sha,
        )
        object.__setattr__(self, "comments", (item,))
        return self


class PublishedComment(ProviderContractModel):
    kind: CommentKind
    comment_id: str | None = None
    thread_id: str | None = None
    url: str | None = None
    error: str | None = None


class CommentPublishResult(ProviderContractModel):
    comments: tuple[PublishedComment, ...]
    published: int = Field(ge=0)
    failed: int = Field(ge=0)
    comment_id: str | None = None
    thread_id: str | None = None
    url: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def expose_single_comment(self) -> CommentPublishResult:
        if len(self.comments) == 1:
            item = self.comments[0]
            object.__setattr__(self, "comment_id", item.comment_id)
            object.__setattr__(self, "thread_id", item.thread_id)
            object.__setattr__(self, "url", item.url)
            object.__setattr__(self, "error", item.error)
        return self


PlatformQueryAction = Literal[
    "pull_request.get",
    "pull_request.changes.list",
    "pull_request.comments.list",
    "pull_request.status.get",
]


class PlatformQueryRequest(ProviderContractModel):
    action: PlatformQueryAction
    repository: str = Field(min_length=1, max_length=512)
    pull_request_number: int = Field(gt=0)
    cursor: str | None = Field(default=None, max_length=128)
    page_size: int = Field(default=100, ge=1, le=100)


class PlatformQueryResult(ProviderContractModel):
    action: PlatformQueryAction
    data: dict[str, Any] | None = None
    items: tuple[dict[str, Any], ...] = ()
    next_cursor: str | None = None


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
class NormalizedWebhook:
    delivery_id: str
    provider_event: ProviderWebhookEvent
    payload: dict[str, Any]


@dataclass(frozen=True)
class ParsedProviderWebhook(NormalizedWebhook):
    raw_body: bytes


@runtime_checkable
class Provider(Protocol):
    """The four provider-neutral protocol conversions exposed by Provider Core."""

    key: str

    async def normalize_webhook(
        self,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> NormalizedWebhook: ...

    async def resolve_git_checkout(
        self,
        request: GitCheckoutRequest,
    ) -> GitCheckoutTarget: ...

    async def publish_comments(
        self,
        request: CommentPublishRequest,
    ) -> CommentPublishResult: ...

    async def query(self, request: PlatformQueryRequest) -> PlatformQueryResult: ...


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
_MISSING_CONFIG = object()


@dataclass(init=False)
class ProviderRuntime:
    """A configured provider instance and the resources owned by it."""

    provider: ProviderAdapter
    descriptor: ProviderDescriptor | None = None
    close: ProviderCloser | None = None

    def __init__(
        self,
        provider: ProviderAdapter | None = None,
        descriptor: ProviderDescriptor | None = None,
        close: ProviderCloser | None = None,
        *,
        adapter: ProviderAdapter | None = None,
    ) -> None:
        if provider is not None and adapter is not None:
            raise TypeError("Pass provider or adapter, not both.")
        resolved = provider if provider is not None else adapter
        if resolved is None:
            raise TypeError("ProviderRuntime requires a provider.")
        self.provider = resolved
        self.descriptor = descriptor
        self.close = close
        if self.descriptor is None:
            key = _provider_key(self.provider)
            self.descriptor = ProviderDescriptor(
                key=key,
                kind=key,
                display_name=key.replace("-", " ").title(),
            )

    @property
    def adapter(self) -> ProviderAdapter:
        """Compatibility name for callers using the pre-Platform architecture."""

        return self.provider


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
            self._register_runtime(runtime)
        self._closed = False

    def register(
        self,
        *,
        key: str,
        factory: Callable[..., ProviderRuntime],
        config: Any = _MISSING_CONFIG,
    ) -> ProviderRuntime:
        """Build and register one runtime, keeping construction in the registry."""

        if self._closed:
            raise RuntimeError("Provider registry is already closed.")
        runtime = factory() if config is _MISSING_CONFIG else factory(config)
        actual_key = _provider_key(runtime.provider)
        if actual_key != key:
            raise ValueError(
                f"Provider factory registered as {key!r} returned {actual_key!r}."
            )
        self._register_runtime(runtime)
        return runtime

    def _register_runtime(self, runtime: ProviderRuntime) -> None:
        key = _provider_key(runtime.provider)
        if key in self._runtimes:
            raise ValueError(f"Provider {key!r} is registered more than once.")
        if runtime.descriptor is None or runtime.descriptor.key != key:
            raise ValueError("Provider descriptor key must match provider key.")
        self._runtimes[key] = runtime

    def get(self, provider: str) -> ProviderAdapter | None:
        runtime = self._runtimes.get(provider)
        return runtime.provider if runtime is not None else None

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
            adapter if adapter is not None and isinstance(adapter, capability) else None
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


def _provider_key(provider: ProviderAdapter) -> str:
    key = getattr(provider, "key", None) or getattr(provider, "provider", None)
    if not isinstance(key, str) or not key:
        raise ValueError("Provider must declare a non-empty key.")
    return key


def lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def payload_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
