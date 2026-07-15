from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from review_orchestrator.config import Settings
    from review_orchestrator.models import (
        AgentTask,
        PullRequestContext,
        ReviewCommentRef,
        ReviewRun,
    )
    from review_orchestrator.review_results import ChangedFile


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


@dataclass(frozen=True)
class ParsedProviderWebhook:
    delivery_id: str
    provider_event: ProviderWebhookEvent
    payload: dict[str, Any]
    raw_body: bytes


class ProviderAdapter(Protocol):
    provider: str

    def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        settings: Settings,
    ) -> ParsedProviderWebhook:
        ...

    async def get_pull_request_context(
        self,
        task: AgentTask,
    ) -> PullRequestContext | None:
        ...

    async def list_changed_files(
        self,
        review_run: ReviewRun,
    ) -> list[ChangedFile]:
        ...

    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None = None,
    ) -> ReviewCommentRef | None:
        ...

    async def publish_line_comments(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        changed_files: list[ChangedFile],
    ) -> dict[str, int]:
        ...

    async def publish_agent_task_comment(
        self,
        session: AsyncSession,
        task: AgentTask,
        *,
        state: str,
    ) -> str:
        ...


class ProviderRegistry:
    def __init__(self, adapters: list[ProviderAdapter]) -> None:
        self._adapters = {adapter.provider: adapter for adapter in adapters}

    def get(self, provider: str) -> ProviderAdapter | None:
        return self._adapters.get(provider)

    def require(self, provider: str) -> ProviderAdapter:
        adapter = self.get(provider)
        if adapter is None:
            raise KeyError(provider)
        return adapter


def lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}
