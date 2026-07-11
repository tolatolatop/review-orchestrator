from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from review_orchestrator.comments import (
    SUMMARY_MARKER,
    _existing_summary_ref_with_body,
    build_summary_comment_body,
    upsert_summary_comment_ref,
)
from review_orchestrator.github import parse_commentable_lines
from review_orchestrator.providers import (
    ParsedProviderWebhook,
    ProviderCapabilityError,
    ProviderPayloadError,
    ProviderSignatureError,
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


class GitLabClientError(RuntimeError):
    pass


class GitLabNote(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | str
    body: str | None = None


class GitLabClient:
    def __init__(
        self,
        *,
        api_base_url: str = "https://gitlab.com/api/v4",
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def get_merge_request(
        self,
        project_path: str,
        merge_request_iid: int,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/projects/{_quoted(project_path)}/merge_requests/{merge_request_iid}",
        )
        if not isinstance(response, dict):
            raise GitLabClientError("GitLab merge request response is not an object.")
        return response

    async def get_merge_request_changes(
        self,
        project_path: str,
        merge_request_iid: int,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/projects/{_quoted(project_path)}/merge_requests/{merge_request_iid}/changes",
        )
        if not isinstance(response, dict):
            raise GitLabClientError("GitLab changes response is not an object.")
        return response

    async def list_merge_request_notes(
        self,
        project_path: str,
        merge_request_iid: int,
    ) -> list[GitLabNote]:
        items = await self._paginate(
            f"/projects/{_quoted(project_path)}/merge_requests/{merge_request_iid}/notes",
        )
        return [GitLabNote.model_validate(item) for item in items]

    async def create_merge_request_note(
        self,
        project_path: str,
        merge_request_iid: int,
        body: str,
    ) -> str:
        response = await self._request(
            "POST",
            f"/projects/{_quoted(project_path)}/merge_requests/{merge_request_iid}/notes",
            json={"body": body},
        )
        return str(response["id"])

    async def update_merge_request_note(
        self,
        project_path: str,
        merge_request_iid: int,
        note_id: str,
        body: str,
    ) -> str:
        response = await self._request(
            "PUT",
            f"/projects/{_quoted(project_path)}/merge_requests/{merge_request_iid}/notes/{note_id}",
            json={"body": body},
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
                raise GitLabClientError(f"GitLab response for {path} is not a list.")
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
        headers = {"Accept": "application/json"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        try:
            async with httpx.AsyncClient(
                base_url=self.api_base_url,
                timeout=self.timeout,
                headers=headers,
            ) as client:
                response = await client.request(method, path, json=json, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitLabClientError(
                f"GitLab request failed ({exc.response.status_code} {method} {path}): "
                f"{exc.response.text[:500]}",
            ) from exc
        except httpx.RequestError as exc:
            raise GitLabClientError(
                f"GitLab request failed ({method} {path}): {exc}",
            ) from exc
        return response.json() if response.content else {}


async def fetch_gitlab_changed_files(
    client: GitLabClient,
    *,
    project_path: str,
    merge_request_iid: int,
) -> list[ChangedFile]:
    response = await client.get_merge_request_changes(project_path, merge_request_iid)
    changes = response.get("changes")
    if not isinstance(changes, list):
        return []
    changed_files: list[ChangedFile] = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        path = item.get("new_path") or item.get("old_path")
        if not isinstance(path, str) or not path:
            continue
        changed_files.append(
            ChangedFile(
                path=path,
                commentable_lines=parse_commentable_lines(item.get("diff")),
            ),
        )
    return changed_files


class GitLabAdapter:
    provider = "gitlab"

    def __init__(self, client: GitLabClient | None = None) -> None:
        self.client = client

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

    async def get_pull_request_context(
        self, task: AgentTask,
    ) -> PullRequestContext | None:
        if self.client is None:
            _msg = "GitLab client is not configured."
            raise ProviderCapabilityError(_msg)
        merge_request = await self.client.get_merge_request(
            task.repo_full_name,
            task.pull_request_number,
        )
        return context_from_merge_request_task(task, merge_request)

    async def list_changed_files(self, review_run: ReviewRun) -> list[ChangedFile]:
        if self.client is None:
            _msg = "GitLab client is not configured."
            raise ProviderCapabilityError(_msg)
        return await fetch_gitlab_changed_files(
            self.client,
            project_path=review_run.repo_full_name,
            merge_request_iid=review_run.pull_request_number,
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
            _msg = "GitLab client is not configured."
            raise ProviderCapabilityError(_msg)

        return await _publish_gitlab_summary_comment(
            session,
            review_run,
            gitlab_client=self.client,
            status_text=status_text,
            finding_stats=finding_stats,
        )

    async def publish_line_comments(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        changed_files: list[ChangedFile],
    ) -> dict[str, int]:  # noqa: ARG002
        return {"published": 0, "summary_only": 0, "deduped": 0, "failed": 0}



async def _publish_gitlab_summary_comment(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    gitlab_client: GitLabClient,
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
        provider_comment_id = await _upsert_gitlab_note(
            gitlab_client,
            review_run,
            body,
        )
    except GitLabClientError as exc:
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


async def _upsert_gitlab_note(
    gitlab_client: GitLabClient,
    review_run: ReviewRun,
    body: str,
) -> str:
    if review_run.summary_comment_id:
        return await gitlab_client.update_merge_request_note(
            review_run.repo_full_name,
            review_run.pull_request_number,
            review_run.summary_comment_id,
            body,
        )

    for note in await gitlab_client.list_merge_request_notes(
        review_run.repo_full_name,
        review_run.pull_request_number,
    ):
        if note.body and SUMMARY_MARKER in note.body:
            return await gitlab_client.update_merge_request_note(
                review_run.repo_full_name,
                review_run.pull_request_number,
                str(note.id),
                body,
            )
    return await gitlab_client.create_merge_request_note(
        review_run.repo_full_name,
        review_run.pull_request_number,
        body,
    )


def extract_pull_request_identity_from_payload(
    payload: dict[str, object],
) -> dict[str, object] | None:
    """Extract MR identity fields from a GitLab webhook payload."""
    from datetime import datetime

    # event_name is available as payload key; not needed for extraction

    attrs = payload.get("object_attributes")
    project = payload.get("project")
    if not isinstance(attrs, dict) or not isinstance(project, dict):
        return None
    repository_name = _optional_str(project.get("path_with_namespace"))
    pull_request_number = attrs.get("iid")
    head_sha = None
    last_commit = attrs.get("last_commit")
    if isinstance(last_commit, dict):
        head_sha = _optional_str(last_commit.get("id"))
    head_sha = head_sha or _optional_str(attrs.get("last_commit_id"))
    if (
        not repository_name
        or not isinstance(pull_request_number, int)
        or not head_sha
    ):
        return None
    target = attrs.get("target")
    source = attrs.get("source")

    closed_at = attrs.get("closed_at")
    if isinstance(closed_at, str):
        closed_at = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    else:
        closed_at = None
    merged_at = attrs.get("merged_at")
    if isinstance(merged_at, str):
        merged_at = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    else:
        merged_at = None

    return {
        "repository": repository_name,
        "number": pull_request_number,
        "provider_repo_id": _id_to_str(project.get("id")),
        "provider_pr_id": _id_to_str(attrs.get("id")),
        "title": _optional_str(attrs.get("title")),
        "author_login": _gitlab_author(payload.get("user")),
        "base_ref": _optional_str(attrs.get("target_branch")),
        "base_sha": _optional_str(attrs.get("target_branch_sha")),
        "head_ref": _optional_str(attrs.get("source_branch")),
        "head_sha": head_sha,
        "base_repo_full_name": _gitlab_project_path(target),
        "head_repo_full_name": _gitlab_project_path(source),
        "status": _optional_str(attrs.get("state")) or "open",
        "html_url": _optional_str(attrs.get("url")),
        "closed_at": closed_at,
        "merged_at": merged_at,
    }


def _gitlab_project_path(project_ref: object) -> str | None:
    if not isinstance(project_ref, dict):
        return None
    return _optional_str(project_ref.get("path_with_namespace"))


def get_gitlab_clone_url(repo_full_name: str) -> str:
    """Build a GitLab clone URL from a repository full name."""
    return f"https://gitlab.com/{repo_full_name}.git"

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
            "GitLab merge request payload is missing attributes.",
        )
    action = _optional_str(attrs.get("action")) or _optional_str(attrs.get("state"))
    source_changed = _source_commit_changed(attrs)
    internal_event = {
        "open": "pr_opened",
        "opened": "pr_opened",
        "reopen": "pr_reopened",
        "reopened": "pr_reopened",
        "merge": "pr_merged",
        "merged": "pr_merged",
        "close": "pr_closed",
        "closed": "pr_closed",
    }.get(action or "")
    if (action or "") in {"update", "updated"} and source_changed:
        internal_event = "pr_updated"
    elif (action or "") in {"update", "updated"}:
        internal_event = "pr_metadata_changed"
    review_actions = {"open", "opened", "reopen", "reopened"}
    should_review = (action or "") in review_actions or internal_event == "pr_updated"
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
        should_create_review_run=should_review,
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


def _quoted(value: str) -> str:
    return quote(value, safe="")


def context_from_merge_request_task(
    task: AgentTask,
    merge_request: dict[str, object],
) -> PullRequestContext:
    from review_orchestrator.models import PullRequestContext

    return PullRequestContext(
        provider=task.provider,
        repo_full_name=task.repo_full_name,
        pull_request_number=task.pull_request_number,
        provider_pr_id=_id_to_str(merge_request.get("id")),
        title=_optional_str(merge_request.get("title")),
        author_login=_gitlab_author(merge_request.get("author")),
        base_ref=_optional_str(merge_request.get("target_branch")),
        base_sha=_optional_str(merge_request.get("diff_refs", {}).get("base_sha"))
        if isinstance(merge_request.get("diff_refs"), dict)
        else None,
        head_ref=_optional_str(merge_request.get("source_branch")),
        head_sha=_optional_str(merge_request.get("sha")) or "",
        head_repo_full_name=task.repo_full_name,
        is_fork=False,
        status=_optional_str(merge_request.get("state")) or "opened",
        html_url=_optional_str(merge_request.get("web_url")),
    )


def _id_to_str(value: object) -> str | None:
    if isinstance(value, int | str):
        return str(value)
    return None


def _gitlab_author(author: object) -> str | None:
    if not isinstance(author, dict):
        return None
    return _optional_str(author.get("username")) or _optional_str(author.get("name"))


def _source_commit_changed(attrs: dict[str, Any]) -> bool:
    changes = attrs.get("changes")
    if not isinstance(changes, dict):
        return False
    for key in ("last_commit", "source_branch_sha"):
        value = changes.get(key)
        if isinstance(value, dict) and value.get("previous") != value.get("current"):
            return True
    return False
