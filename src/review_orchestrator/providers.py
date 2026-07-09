from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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
        settings: Any,
    ) -> ParsedProviderWebhook:
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
