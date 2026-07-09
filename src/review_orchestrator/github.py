from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class GitHubWebhookError(Exception):
    status_code = 400
    error_code = "github_webhook_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GitHubSignatureError(GitHubWebhookError):
    status_code = 401
    error_code = "github_signature_invalid"


class GitHubPayloadError(GitHubWebhookError):
    error_code = "github_payload_invalid"


@dataclass(frozen=True)
class NormalizedGitHubEvent:
    provider_event: str
    provider_action: str | None
    internal_event: str | None
    repository: str | None
    pull_request_number: int | None
    head_sha: str | None
    should_update_context: bool
    should_create_review_run: bool
    status: str


PR_ACTIONS_TO_INTERNAL_EVENT = {
    "opened": "pr_opened",
    "synchronize": "pr_updated",
    "reopened": "pr_reopened",
    "closed": "pr_closed",
    "edited": "pr_metadata_changed",
    "ready_for_review": "pr_metadata_changed",
    "converted_to_draft": "pr_metadata_changed",
    "labeled": "pr_metadata_changed",
    "unlabeled": "pr_metadata_changed",
    "assigned": "pr_metadata_changed",
    "unassigned": "pr_metadata_changed",
}
REVIEW_RUN_ACTIONS = {"opened", "synchronize", "reopened"}
COMMENT_CONTEXT_EVENTS = {
    "issue_comment": "pr_comment_context",
    "pull_request_review": "pr_comment_context",
    "pull_request_review_comment": "pr_comment_context",
}


def parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GitHubPayloadError("GitHub webhook payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise GitHubPayloadError("GitHub webhook payload must be a JSON object.")
    return payload


def payload_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def verify_signature(body: bytes, signature: str | None, secret: str | None) -> None:
    if not secret:
        return
    if not signature:
        raise GitHubSignatureError("Missing X-Hub-Signature-256 header.")

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise GitHubSignatureError("Invalid GitHub webhook signature.")


def normalize_github_event(
    provider_event: str,
    payload: dict[str, Any],
) -> NormalizedGitHubEvent:
    action = _optional_str(payload.get("action"))

    if provider_event == "pull_request":
        return _normalize_pull_request_event(action, payload)

    if provider_event in COMMENT_CONTEXT_EVENTS:
        return _normalize_comment_context_event(provider_event, action, payload)

    return NormalizedGitHubEvent(
        provider_event=provider_event,
        provider_action=action,
        internal_event=None,
        repository=_repository_name(payload),
        pull_request_number=_pull_request_number(payload),
        head_sha=_pull_request_head_sha(payload),
        should_update_context=False,
        should_create_review_run=False,
        status="ignored",
    )


def parse_github_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_pull_request_event(
    action: str | None,
    payload: dict[str, Any],
) -> NormalizedGitHubEvent:
    internal_event = PR_ACTIONS_TO_INTERNAL_EVENT.get(action or "")
    if action == "closed" and _pull_request_merged(payload):
        internal_event = "pr_merged"

    status = "received" if internal_event else "ignored"
    return NormalizedGitHubEvent(
        provider_event="pull_request",
        provider_action=action,
        internal_event=internal_event,
        repository=_repository_name(payload),
        pull_request_number=_pull_request_number(payload),
        head_sha=_pull_request_head_sha(payload),
        should_update_context=internal_event is not None,
        should_create_review_run=action in REVIEW_RUN_ACTIONS,
        status=status,
    )


def _normalize_comment_context_event(
    provider_event: str,
    action: str | None,
    payload: dict[str, Any],
) -> NormalizedGitHubEvent:
    pull_request_number = _pull_request_number(payload)
    internal_event = (
        COMMENT_CONTEXT_EVENTS[provider_event] if pull_request_number else None
    )
    return NormalizedGitHubEvent(
        provider_event=provider_event,
        provider_action=action,
        internal_event=internal_event,
        repository=_repository_name(payload),
        pull_request_number=pull_request_number,
        head_sha=_pull_request_head_sha(payload),
        should_update_context=False,
        should_create_review_run=False,
        status="received" if internal_event else "ignored",
    )


def _repository_name(payload: dict[str, Any]) -> str | None:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    return _optional_str(repository.get("full_name"))


def _pull_request_number(payload: dict[str, Any]) -> int | None:
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict) and isinstance(pull_request.get("number"), int):
        return pull_request["number"]

    issue = payload.get("issue")
    if (
        isinstance(issue, dict)
        and isinstance(issue.get("number"), int)
        and isinstance(issue.get("pull_request"), dict)
    ):
        return issue["number"]

    return None


def _pull_request_head_sha(payload: dict[str, Any]) -> str | None:
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    head = pull_request.get("head")
    if not isinstance(head, dict):
        return None
    return _optional_str(head.get("sha"))


def _pull_request_merged(payload: dict[str, Any]) -> bool:
    pull_request = payload.get("pull_request")
    return isinstance(pull_request, dict) and pull_request.get("merged") is True


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
