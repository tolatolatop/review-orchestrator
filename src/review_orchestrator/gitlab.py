from __future__ import annotations

import json
from typing import Any

from review_orchestrator.providers import (
    ParsedProviderWebhook,
    ProviderPayloadError,
    ProviderSignatureError,
    ProviderWebhookEvent,
    lower_headers,
)


class GitLabAdapter:
    provider = "gitlab"

    def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        settings: Any,
    ) -> ParsedProviderWebhook:
        normalized_headers = lower_headers(headers)
        delivery_id = (
            normalized_headers.get("x-gitlab-event-uuid")
            or normalized_headers.get("x-request-id")
        )
        event_name = normalized_headers.get("x-gitlab-event")
        if not delivery_id:
            raise ProviderPayloadError("Missing X-Gitlab-Event-UUID header.")
        if not event_name:
            raise ProviderPayloadError("Missing X-Gitlab-Event header.")

        secret = getattr(settings, "gitlab_webhook_secret", None)
        token = normalized_headers.get("x-gitlab-token")
        if secret and token != secret:
            raise ProviderSignatureError("Invalid GitLab webhook token.")

        payload = _parse_json_body(raw_body)
        event = normalize_gitlab_event(event_name, payload)
        return ParsedProviderWebhook(
            delivery_id=delivery_id,
            provider_event=event,
            payload=payload,
            raw_body=raw_body,
        )


def normalize_gitlab_event(
    event_name: str,
    payload: dict[str, Any],
) -> ProviderWebhookEvent:
    if event_name != "Merge Request Hook":
        return ProviderWebhookEvent(
            provider="gitlab",
            provider_event=event_name,
            provider_action=_optional_str(payload.get("object_kind")),
            internal_event=None,
            repository=_project_path(payload),
            pull_request_number=None,
            head_sha=None,
            should_update_context=False,
            should_create_review_run=False,
            should_create_agent_task=False,
            status="ignored",
        )

    attrs = payload.get("object_attributes")
    if not isinstance(attrs, dict):
        raise ProviderPayloadError(
            "GitLab merge request payload is missing attributes."
        )
    action = _optional_str(attrs.get("action")) or _optional_str(attrs.get("state"))
    internal_event = {
        "open": "pr_opened",
        "opened": "pr_opened",
        "update": "pr_updated",
        "updated": "pr_updated",
        "reopen": "pr_reopened",
        "reopened": "pr_reopened",
        "merge": "pr_merged",
        "merged": "pr_merged",
        "close": "pr_closed",
        "closed": "pr_closed",
    }.get(action or "")
    review_actions = {"open", "opened", "update", "updated", "reopen", "reopened"}
    return ProviderWebhookEvent(
        provider="gitlab",
        provider_event=event_name,
        provider_action=action,
        internal_event=internal_event,
        repository=_project_path(payload),
        pull_request_number=_int_or_none(attrs.get("iid")),
        head_sha=_optional_str(attrs.get("last_commit", {}).get("id"))
        if isinstance(attrs.get("last_commit"), dict)
        else _optional_str(attrs.get("last_commit_id")),
        should_update_context=internal_event is not None,
        should_create_review_run=(action or "") in review_actions,
        should_create_agent_task=False,
        status="received" if internal_event else "ignored",
    )


def _parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderPayloadError("GitLab webhook payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ProviderPayloadError("GitLab webhook payload must be a JSON object.")
    return payload


def _project_path(payload: dict[str, Any]) -> str | None:
    project = payload.get("project")
    if not isinstance(project, dict):
        return None
    return _optional_str(project.get("path_with_namespace"))


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None
