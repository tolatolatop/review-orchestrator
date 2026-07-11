from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from review_orchestrator.comments import (
    SUMMARY_MARKER,
    _existing_line_ref,
    _existing_summary_ref_with_body,
    _line_comment_body,
    build_summary_comment_body,
    ensure_line_comment_ref,
    upsert_summary_comment_ref,
)
from review_orchestrator.models import Finding
from review_orchestrator.providers import (
    ParsedProviderWebhook,
    ProviderCapabilityError,
    ProviderPayloadError,
    ProviderSignatureError,
    ProviderWebhookError,
    ProviderWebhookEvent,
    lower_headers,
)
from review_orchestrator.review_results import ChangedFile

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from review_orchestrator.models import (
        AgentTask,
        PullRequestContext,
        ReviewCommentRef,
        ReviewRun,
    )


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
        settings: Any,
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
        )
        return ParsedProviderWebhook(
            delivery_id=delivery_id,
            provider_event=normalized_event.to_provider_event(),
            payload=payload,
            raw_body=raw_body,
        )

    async def get_pull_request_context(
        self, task: AgentTask,
    ) -> PullRequestContext | None:
        if self.client is None:
            _msg = "GitHub client is not configured."
            raise ProviderCapabilityError(_msg)
        pull_request = await self.client.get_pull_request(
            task.repo_full_name,
            task.pull_request_number,
        )
        return context_from_pull_request_task(task, pull_request)

    async def list_changed_files(self, review_run: ReviewRun) -> list[ChangedFile]:
        if self.client is None:
            _msg = "GitHub client is not configured."
            raise ProviderCapabilityError(_msg)
        return await fetch_changed_files(
            self.client,
            repo_full_name=review_run.repo_full_name,
            pull_request_number=review_run.pull_request_number,
        )

    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None = None,
    ) -> ReviewCommentRef | None:
        if self.client is None:
            _msg = "GitHub client is not configured."
            raise ProviderCapabilityError(_msg)

        return await _publish_github_summary_comment(
            session,
            review_run,
            github_client=self.client,
            status_text=status_text,
            finding_stats=finding_stats,
        )

    async def publish_line_comments(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        changed_files: list[ChangedFile],
    ) -> dict[str, int]:
        if self.client is None:
            _msg = "GitHub client is not configured."
            raise ProviderCapabilityError(_msg)

        return await _publish_github_line_comments(
            session,
            review_run,
            github_client=self.client,
            changed_files=changed_files,
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
        timeout: float = 30.0,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def list_pull_request_files(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> list[dict[str, Any]]:
        return await self._paginate(
            f"/repos/{repo_full_name}/pulls/{pull_request_number}/files",
        )

    async def get_pull_request(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/repos/{repo_full_name}/pulls/{pull_request_number}",
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
            json={
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": line,
                "side": "RIGHT",
            },
        )
        return str(response["id"])

    async def _paginate(self, path: str) -> list[dict[str, Any]]:
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            response = await self._request(
                "GET", path, params={"per_page": 100, "page": page},
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
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
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
                f"{exc.response.text[:500]}",
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubClientError(
                f"GitHub request failed ({method} {path}): {exc}",
            ) from exc
        return response.json() if response.content else {}


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
            ),
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
    pull_request: dict[str, object],
) -> PullRequestContext:
    from review_orchestrator.models import PullRequestContext

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
            head_repo_full_name and head_repo_full_name != base_repo_full_name,
        ),
        status=_optional_str(pull_request.get("state")) or "open",
        html_url=_optional_str(pull_request.get("html_url")),
    )


def _id_to_str(value: object) -> str | None:
    if isinstance(value, int | str):
        return str(value)
    return None


def _sha(ref_object: object) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _optional_str(ref_object.get("sha"))


def _ref(ref_object: object) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _optional_str(ref_object.get("ref"))


def _repo_full_name(repo: object) -> str | None:
    if not isinstance(repo, dict):
        return None
    return _optional_str(repo.get("full_name"))


def _login(user: object) -> str | None:
    if not isinstance(user, dict):
        return None
    return _optional_str(user.get("login"))



async def _publish_github_summary_comment(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    github_client: GitHubClient,
    status_text: str,
    finding_stats: dict[str, int] | None = None,
) -> ReviewCommentRef | None:
    body = build_summary_comment_body(
        review_run,
        status_text=status_text,
        finding_stats=finding_stats,
    )
    existing_ref = await _existing_summary_ref_with_body(session, review_run, body)
    if existing_ref is not None:
        return existing_ref
    try:
        provider_comment_id = await _upsert_github_issue_comment(
            github_client,
            review_run,
            body,
        )
    except GitHubClientError as exc:
        review_run.failure_code = "provider_permission_denied"
        review_run.error = str(exc)
        session.add(review_run)
        await session.commit()
        return None
    return await upsert_summary_comment_ref(
        session,
        review_run,
        provider_comment_id=provider_comment_id,
        body=body,
    )


async def _publish_github_line_comments(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    github_client: GitHubClient,
    changed_files: list[ChangedFile],
) -> dict[str, int]:
    commentable = {item.path: item.commentable_lines for item in changed_files}
    result = await session.execute(
        select(Finding).where(
            Finding.review_run_id == review_run.id,
            Finding.status == "active",
        )
    )
    stats = {"published": 0, "summary_only": 0, "deduped": 0, "failed": 0}
    for finding in result.scalars().all():
        line = finding.line_start
        file_lines = commentable.get(finding.file_path)
        if (
            line is None
            or file_lines is None
            or not file_lines
            or line not in file_lines
        ):
            stats["summary_only"] += 1
            continue

        body = _line_comment_body(finding)
        existing = await _existing_line_ref(session, finding)
        if existing is not None:
            stats["deduped"] += 1
            continue
        try:
            provider_comment_id = await github_client.create_review_comment(
                review_run.repo_full_name,
                review_run.pull_request_number,
                body=body,
                commit_id=review_run.head_sha,
                path=finding.file_path,
                line=line,
            )
        except GitHubClientError as exc:
            review_run.failure_code = "provider_permission_denied"
            review_run.error = str(exc)
            session.add(review_run)
            await session.commit()
            stats["failed"] += 1
            continue
        _, created = await ensure_line_comment_ref(
            session,
            review_run,
            finding,
            provider_comment_id=provider_comment_id,
            body=body,
        )
        stats["published" if created else "deduped"] += 1
    return stats


async def _upsert_github_issue_comment(
    github_client: GitHubClient,
    review_run: ReviewRun,
    body: str,
) -> str:
    if review_run.summary_comment_id:
        return await github_client.update_issue_comment(
            review_run.repo_full_name,
            review_run.summary_comment_id,
            body,
        )

    for comment in await github_client.list_issue_comments(
        review_run.repo_full_name,
        review_run.pull_request_number,
    ):
        if comment.body and SUMMARY_MARKER in comment.body:
            return await github_client.update_issue_comment(
                review_run.repo_full_name,
                str(comment.id),
                body,
            )
    return await github_client.create_issue_comment(
        review_run.repo_full_name,
        review_run.pull_request_number,
        body,
    )


def extract_pull_request_identity_from_payload(
    payload: dict[str, object],
) -> dict[str, object] | None:
    """Extract PR identity fields from a GitHub webhook payload."""
    from datetime import datetime

    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        return None
    repository_name = _optional_str(repository.get("full_name"))
    pull_request_number = pull_request.get("number")
    if not repository_name or not isinstance(pull_request_number, int):
        return None
    base = pull_request.get("base")
    head = pull_request.get("head")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None

    closed_at = pull_request.get("closed_at")
    if isinstance(closed_at, str):
        closed_at = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    else:
        closed_at = None
    merged_at = pull_request.get("merged_at")
    if isinstance(merged_at, str):
        merged_at = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    else:
        merged_at = None

    return {
        "repository": repository_name,
        "number": pull_request_number,
        "provider_repo_id": _id_to_str(repository.get("id")),
        "provider_pr_id": _id_to_str(pull_request.get("id")),
        "title": _optional_str(pull_request.get("title")),
        "author_login": _login(pull_request.get("user")),
        "base_ref": _ref(base),
        "base_sha": _sha(base),
        "head_ref": _ref(head),
        "head_sha": _head_sha_github(pull_request),
        "base_repo_full_name": _repo_full_name(base_repo),
        "head_repo_full_name": _repo_full_name(head_repo),
        "status": _pull_request_status(pull_request),
        "html_url": _optional_str(pull_request.get("html_url")),
        "closed_at": closed_at,
        "merged_at": merged_at,
    }


def _pull_request_status(pull_request: dict[str, object]) -> str:
    if pull_request.get("merged") is True:
        return "merged"
    state = pull_request.get("state")
    return state if isinstance(state, str) and state else "open"


def _head_sha_github(pull_request: dict[str, object]) -> str | None:
    head = pull_request.get("head")
    if not isinstance(head, dict):
        return None
    return _optional_str(head.get("sha"))


def get_github_clone_url(repo_full_name: str) -> str:
    """Build a GitHub clone URL from a repository full name."""
    return f"https://github.com/{repo_full_name}.git"

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
    *,
    bot_login: str | None = None,
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
) -> NormalizedGitHubEvent:
    pull_request_number = _pull_request_number(payload)
    mentions_bot = _comment_mentions_bot(payload, bot_login)
    internal_event = (
        "agent_mention"
        if pull_request_number and mentions_bot
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
        should_create_agent_task=pull_request_number is not None and mentions_bot,
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


def _comment_mentions_bot(payload: dict[str, Any], bot_login: str | None) -> bool:
    if not bot_login:
        return False
    comment = payload.get("comment")
    review = payload.get("review")
    body = None
    if isinstance(comment, dict):
        body = comment.get("body")
    elif isinstance(review, dict):
        body = review.get("body")
    if not isinstance(body, str):
        return False
    return f"@{bot_login.lower()}" in body.lower()


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
