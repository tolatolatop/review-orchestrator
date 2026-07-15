"""GitHub webhook normalization, API client, and provider adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict

from review_orchestrator.domain.review_results import ChangedFile
from review_orchestrator.integrations.github_auth import (
    GitHubAppTokenProvider,
    GitHubAuthenticationError,
    GitHubTokenProvider,
    StaticGitHubTokenProvider,
)
from review_orchestrator.integrations.providers import (
    AgentCommand,
    ParsedProviderWebhook,
    ProviderCapabilityError,
    ProviderOperationError,
    ProviderPayloadError,
    ProviderSignatureError,
    ProviderWebhookError,
    ProviderWebhookEvent,
    lower_headers,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from review_orchestrator.domain.models import (
        AgentTask,
        PullRequestContext,
        ReviewCommentRef,
        ReviewRun,
    )
    from review_orchestrator.infrastructure.config import Settings


class GitHubWebhookError(ProviderWebhookError):
    error_code = "github_webhook_error"


class GitHubSignatureError(GitHubWebhookError, ProviderSignatureError):
    error_code = "github_signature_invalid"


class GitHubPayloadError(GitHubWebhookError, ProviderPayloadError):
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
    should_create_agent_task: bool
    status: str
    agent_command: AgentCommand | None = None

    def to_provider_event(self) -> ProviderWebhookEvent:
        return ProviderWebhookEvent(
            provider="github",
            provider_event=self.provider_event,
            provider_action=self.provider_action,
            internal_event=self.internal_event,
            repository=self.repository,
            pull_request_number=self.pull_request_number,
            head_sha=self.head_sha,
            should_update_context=self.should_update_context,
            should_create_review_run=self.should_create_review_run,
            should_create_agent_task=self.should_create_agent_task,
            status=self.status,
            agent_command=self.agent_command,
        )


class GitHubAdapter:
    provider = "github"

    def __init__(self, client: GitHubClient | None = None) -> None:
        self.client = client

    def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        settings: Settings,
    ) -> ParsedProviderWebhook:
        normalized_headers = lower_headers(headers)
        delivery_id = normalized_headers.get("x-github-delivery")
        provider_event = normalized_headers.get("x-github-event")
        if not delivery_id:
            raise GitHubPayloadError("Missing X-GitHub-Delivery header.")
        if not provider_event:
            raise GitHubPayloadError("Missing X-GitHub-Event header.")

        verify_signature(
            raw_body,
            normalized_headers.get("x-hub-signature-256"),
            getattr(settings, "github_webhook_secret", None),
        )
        payload = parse_json_body(raw_body)
        normalized_event = normalize_github_event(
            provider_event,
            payload,
            bot_login=getattr(settings, "review_bot_login", None),
            command_enabled=getattr(settings, "agent_command_enabled", True),
            allowed_associations={
                item.strip().upper()
                for item in getattr(
                    settings,
                    "agent_task_allowed_associations",
                    "OWNER,MEMBER,COLLABORATOR",
                ).split(",")
                if item.strip()
            },
            max_command_chars=getattr(settings, "agent_task_max_command_chars", 8000),
        )
        return ParsedProviderWebhook(
            delivery_id=delivery_id,
            provider_event=normalized_event.to_provider_event(),
            payload=payload,
            raw_body=raw_body,
        )

    async def get_pull_request_context(
        self,
        task: AgentTask,
    ) -> PullRequestContext:
        operation = "get_pull_request_context"
        client = self._require_client(operation)
        try:
            pull_request = await client.get_pull_request(
                task.repo_full_name,
                task.pull_request_number,
            )
        except GitHubClientError as exc:
            raise self._operation_error(operation, exc) from exc
        return context_from_pull_request_task(task, pull_request)

    async def list_changed_files(
        self,
        review_run: ReviewRun,
    ) -> list[ChangedFile]:
        operation = "list_changed_files"
        client = self._require_client(operation)
        try:
            return await fetch_changed_files(
                client,
                repo_full_name=review_run.repo_full_name,
                pull_request_number=review_run.pull_request_number,
            )
        except GitHubClientError as exc:
            raise self._operation_error(operation, exc) from exc

    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None = None,
    ) -> ReviewCommentRef | None:
        operation = "publish_summary_comment"
        client = self._require_client(operation)
        from review_orchestrator.integrations.comments import (
            publish_github_summary_comment,
        )

        try:
            return await publish_github_summary_comment(
                session,
                review_run,
                github_client=client,
                status_text=status_text,
                finding_stats=finding_stats,
            )
        except GitHubClientError as exc:
            raise self._operation_error(operation, exc) from exc

    async def publish_line_comments(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        changed_files: list[ChangedFile],
    ) -> dict[str, int]:
        operation = "publish_line_comments"
        client = self._require_client(operation)
        from review_orchestrator.integrations.comments import (
            publish_github_line_comments,
        )

        try:
            return await publish_github_line_comments(
                session,
                review_run,
                github_client=client,
                changed_files=changed_files,
            )
        except GitHubClientError as exc:
            raise self._operation_error(operation, exc) from exc

    async def publish_agent_task_comment(
        self,
        session: AsyncSession,
        task: AgentTask,
        *,
        state: str,
    ) -> str:
        operation = "publish_agent_task_comment"
        client = self._require_client(operation)
        from review_orchestrator.integrations.comments import (
            publish_github_agent_task_comment,
        )

        try:
            return await publish_github_agent_task_comment(
                session,
                task,
                github_client=client,
                state=state,
            )
        except GitHubClientError as exc:
            raise self._operation_error(operation, exc) from exc

    def _require_client(self, operation: str) -> GitHubClient:
        if self.client is None:
            raise ProviderCapabilityError(
                "GitHub client is not configured.",
                provider=self.provider,
                operation=operation,
            )
        return self.client

    def _operation_error(
        self,
        operation: str,
        error: GitHubClientError,
    ) -> ProviderOperationError:
        return ProviderOperationError(
            str(error),
            provider=self.provider,
            operation=operation,
        )


class GitHubClientError(RuntimeError):
    pass


class GitHubComment(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | str
    body: str | None = None


class GitHubClient:
    def __init__(
        self,
        *,
        api_base_url: str = "https://api.github.com",
        token: str | None = None,
        token_provider: GitHubTokenProvider | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.token = token
        self.token_provider = token_provider or StaticGitHubTokenProvider(token)
        self.timeout = timeout

    async def get_token(self, repo_full_name: str) -> str | None:
        try:
            return await self.token_provider.get_token(repo_full_name)
        except GitHubAuthenticationError as exc:
            raise GitHubClientError(str(exc)) from exc

    async def get_permissions(self, repo_full_name: str) -> dict[str, str] | None:
        try:
            return await self.token_provider.get_permissions(repo_full_name)
        except GitHubAuthenticationError as exc:
            raise GitHubClientError(str(exc)) from exc

    async def aclose(self) -> None:
        await self.token_provider.aclose()

    async def list_pull_request_files(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> list[dict[str, Any]]:
        return await self._paginate(
            f"/repos/{repo_full_name}/pulls/{pull_request_number}/files",
            repo_full_name=repo_full_name,
        )

    async def get_pull_request(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/repos/{repo_full_name}/pulls/{pull_request_number}",
            repo_full_name=repo_full_name,
        )
        if not isinstance(response, dict):
            raise GitHubClientError("GitHub pull request response is not an object.")
        return response

    async def list_issue_comments(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> list[GitHubComment]:
        items = await self._paginate(
            f"/repos/{repo_full_name}/issues/{pull_request_number}/comments",
            repo_full_name=repo_full_name,
        )
        return [GitHubComment.model_validate(item) for item in items]

    async def create_issue_comment(
        self,
        repo_full_name: str,
        pull_request_number: int,
        body: str,
    ) -> str:
        response = await self._request(
            "POST",
            f"/repos/{repo_full_name}/issues/{pull_request_number}/comments",
            repo_full_name=repo_full_name,
            json={"body": body},
        )
        return str(response["id"])

    async def update_issue_comment(
        self,
        repo_full_name: str,
        comment_id: str,
        body: str,
    ) -> str:
        response = await self._request(
            "PATCH",
            f"/repos/{repo_full_name}/issues/comments/{comment_id}",
            repo_full_name=repo_full_name,
            json={"body": body},
        )
        return str(response["id"])

    async def create_review_comment(
        self,
        repo_full_name: str,
        pull_request_number: int,
        *,
        body: str,
        commit_id: str,
        path: str,
        line: int,
    ) -> str:
        response = await self._request(
            "POST",
            f"/repos/{repo_full_name}/pulls/{pull_request_number}/comments",
            repo_full_name=repo_full_name,
            json={
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": line,
                "side": "RIGHT",
            },
        )
        return str(response["id"])

    async def _paginate(
        self, path: str, *, repo_full_name: str | None = None
    ) -> list[dict[str, Any]]:
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            response = await self._request(
                "GET",
                path,
                params={"per_page": 100, "page": page},
                repo_full_name=repo_full_name,
            )
            if not isinstance(response, list):
                raise GitHubClientError(f"GitHub response for {path} is not a list.")
            items.extend(response)
            if len(response) < 100:
                return items
            page += 1

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        repo_full_name: str | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = await self.get_token(repo_full_name) if repo_full_name else self.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(
                base_url=self.api_base_url,
                timeout=self.timeout,
                headers=headers,
            ) as client:
                response = await client.request(method, path, json=json, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubClientError(
                f"GitHub request failed ({exc.response.status_code} {method} {path}): "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubClientError(
                f"GitHub request failed ({method} {path}): {exc}"
            ) from exc
        return response.json() if response.content else {}


def create_github_client(settings: Any) -> GitHubClient:
    app_id = getattr(settings, "github_app_id", None)
    private_key_path = getattr(settings, "github_private_key_path", None)
    if bool(app_id) != bool(private_key_path):
        raise GitHubAuthenticationError(
            "GITHUB_APP_ID and GITHUB_PRIVATE_KEY_PATH must be configured together."
        )

    if app_id and private_key_path:
        token_provider: GitHubTokenProvider = GitHubAppTokenProvider(
            app_id=app_id,
            private_key_path=private_key_path,
            api_base_url=settings.github_api_base_url,
            installation_id=getattr(settings, "github_installation_id", None),
        )
    else:
        token_provider = StaticGitHubTokenProvider(
            getattr(settings, "github_installation_token", None)
        )

    return GitHubClient(
        api_base_url=settings.github_api_base_url,
        token_provider=token_provider,
        timeout=settings.provider_api_timeout_seconds,
    )


async def fetch_changed_files(
    client: GitHubClient,
    *,
    repo_full_name: str,
    pull_request_number: int,
) -> list[ChangedFile]:
    files = await client.list_pull_request_files(repo_full_name, pull_request_number)
    changed_files: list[ChangedFile] = []
    for item in files:
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            continue
        changed_files.append(
            ChangedFile(
                path=filename,
                commentable_lines=parse_commentable_lines(item.get("patch")),
            )
        )
    return changed_files


def parse_commentable_lines(patch: Any) -> set[int]:
    if not isinstance(patch, str) or not patch:
        return set()

    lines: set[int] = set()
    new_line: int | None = None
    for raw_line in patch.splitlines():
        if raw_line.startswith("@@"):
            new_line = _parse_hunk_new_start(raw_line)
            continue
        if new_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            lines.add(new_line)
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        else:
            new_line += 1
    return lines


def _parse_hunk_new_start(header: str) -> int | None:
    marker = header.split(" +", 1)
    if len(marker) != 2:
        return None
    range_part = marker[1].split(" ", 1)[0]
    start = range_part.split(",", 1)[0]
    try:
        return int(start)
    except ValueError:
        return None


def context_from_pull_request_task(
    task: AgentTask,
    pull_request: dict[str, Any],
) -> PullRequestContext:
    from review_orchestrator.domain.models import PullRequestContext

    base = pull_request.get("base") if isinstance(pull_request, dict) else None
    head = pull_request.get("head") if isinstance(pull_request, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None
    head_repo_full_name = _repo_full_name(head_repo)
    base_repo_full_name = _repo_full_name(base_repo)
    return PullRequestContext(
        provider=task.provider,
        repo_full_name=task.repo_full_name,
        pull_request_number=task.pull_request_number,
        provider_pr_id=_id_to_str(pull_request.get("id")),
        title=_optional_str(pull_request.get("title")),
        author_login=_login(pull_request.get("user")),
        base_ref=_ref(base),
        base_sha=_sha(base),
        head_ref=_ref(head),
        head_sha=_sha(head) or "",
        head_repo_full_name=head_repo_full_name,
        is_fork=bool(
            head_repo_full_name and head_repo_full_name != base_repo_full_name
        ),
        status=_optional_str(pull_request.get("state")) or "open",
        html_url=_optional_str(pull_request.get("html_url")),
    )


def _id_to_str(value: Any) -> str | None:
    if isinstance(value, int | str):
        return str(value)
    return None


def _sha(ref_object: Any) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _optional_str(ref_object.get("sha"))


def _ref(ref_object: Any) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _optional_str(ref_object.get("ref"))


def _repo_full_name(repo: Any) -> str | None:
    if not isinstance(repo, dict):
        return None
    return _optional_str(repo.get("full_name"))


def _login(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return _optional_str(user.get("login"))


PR_ACTIONS_TO_INTERNAL_EVENT = {
    "opened": "pr_opened",
    "synchronize": "pr_updated",
    "reopened": "pr_reopened",
    "closed": "pr_closed",
    "edited": "pr_metadata_changed",
    "ready_for_review": "pr_ready_for_review",
    "converted_to_draft": "pr_converted_to_draft",
    "labeled": "pr_metadata_changed",
    "unlabeled": "pr_metadata_changed",
    "assigned": "pr_metadata_changed",
    "unassigned": "pr_metadata_changed",
}
REVIEW_RUN_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
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

    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise GitHubSignatureError("Invalid GitHub webhook signature.")


def normalize_github_event(
    provider_event: str,
    payload: dict[str, Any],
    *,
    bot_login: str | None = None,
    command_enabled: bool = True,
    allowed_associations: set[str] | None = None,
    max_command_chars: int = 8000,
) -> NormalizedGitHubEvent:
    action = _optional_str(payload.get("action"))

    if provider_event == "pull_request":
        return _normalize_pull_request_event(action, payload)

    if provider_event in COMMENT_CONTEXT_EVENTS:
        return _normalize_comment_context_event(
            provider_event,
            action,
            payload,
            bot_login=bot_login,
            command_enabled=command_enabled,
            allowed_associations=allowed_associations,
            max_command_chars=max_command_chars,
        )

    return NormalizedGitHubEvent(
        provider_event=provider_event,
        provider_action=action,
        internal_event=None,
        repository=_repository_name(payload),
        pull_request_number=_pull_request_number(payload),
        head_sha=_pull_request_head_sha(payload),
        should_update_context=False,
        should_create_review_run=False,
        should_create_agent_task=False,
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
        should_create_agent_task=False,
        status=status,
    )


def _normalize_comment_context_event(
    provider_event: str,
    action: str | None,
    payload: dict[str, Any],
    *,
    bot_login: str | None,
    command_enabled: bool,
    allowed_associations: set[str] | None,
    max_command_chars: int,
) -> NormalizedGitHubEvent:
    pull_request_number = _pull_request_number(payload)
    command = _extract_agent_command(
        provider_event,
        action,
        payload,
        bot_login=bot_login,
        command_enabled=command_enabled,
        allowed_associations=allowed_associations,
        max_command_chars=max_command_chars,
    )
    internal_event = (
        "agent_command"
        if pull_request_number and command is not None
        else COMMENT_CONTEXT_EVENTS[provider_event]
        if pull_request_number
        else None
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
        should_create_agent_task=(
            pull_request_number is not None and command is not None
        ),
        status="received" if internal_event else "ignored",
        agent_command=command,
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


def _extract_agent_command(
    provider_event: str,
    action: str | None,
    payload: dict[str, Any],
    *,
    bot_login: str | None,
    command_enabled: bool,
    allowed_associations: set[str] | None,
    max_command_chars: int,
) -> AgentCommand | None:
    accepted_actions = {
        "issue_comment": "created",
        "pull_request_review": "submitted",
        "pull_request_review_comment": "created",
    }
    if (
        not command_enabled
        or not bot_login
        or action != accepted_actions.get(provider_event)
    ):
        return None
    comment = payload.get("comment")
    review = payload.get("review")
    source = comment if isinstance(comment, dict) else review
    if not isinstance(source, dict):
        return None
    body = source.get("body")
    source_id = source.get("id")
    user = source.get("user")
    if not isinstance(body, str) or not isinstance(source_id, int | str):
        return None
    if not isinstance(user, dict):
        return None
    author_login = _optional_str(user.get("login"))
    author_type = _optional_str(user.get("type"))
    if (
        not author_login
        or author_login.lower() == bot_login.lower()
        or (author_type and author_type.lower() == "bot")
    ):
        return None
    association = _optional_str(source.get("author_association")) or _optional_str(
        payload.get("author_association")
    )
    if allowed_associations is not None and (
        not association or association.upper() not in allowed_associations
    ):
        return None
    mention_pattern = re.compile(
        rf"(?<![A-Za-z0-9_-])@{re.escape(bot_login)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    if mention_pattern.search(body) is None:
        return None
    command_text = mention_pattern.sub("", body).strip()
    if len(command_text) > max_command_chars:
        return None
    return AgentCommand(
        source_kind=provider_event,
        source_comment_id=str(source_id),
        source_url=_optional_str(source.get("html_url")),
        author_login=author_login,
        author_association=association,
        command_text=command_text,
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
