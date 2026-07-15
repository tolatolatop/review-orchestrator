"""GitLab webhook normalization, API client, and provider adapter."""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict

from review_orchestrator.domain.review_results import ChangedFile
from review_orchestrator.integrations.github import (
    _idempotency_marker,
    _idempotent_comment_body,
    _query_page,
    _safe_platform_error,
    parse_commentable_lines,
)
from review_orchestrator.integrations.providers import (
    CommentPublishItem,
    CommentPublishRequest,
    CommentPublishResult,
    Credential,
    GitCheckoutRequest,
    GitCheckoutTarget,
    NormalizedWebhook,
    ParsedProviderWebhook,
    PlatformQueryRequest,
    PlatformQueryResult,
    ProviderCapabilityError,
    ProviderOperationError,
    ProviderPayloadError,
    ProviderSignatureError,
    ProviderWebhookEvent,
    ProviderWorkspaceCheckout,
    PublishedComment,
    PullRequestSnapshot,
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


class GitLabClientError(RuntimeError):
    pass


def gitlab_clone_url(api_base_url: str, repo_full_name: str) -> str:
    parsed = urlsplit(api_base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v4"):
        path = path[: -len("/api/v4")]
    origin = urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
    return f"{origin}/{repo_full_name}.git"


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

    async def aclose(self) -> None:
        """Kept for a uniform platform lifecycle; requests own no persistent pool."""

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
            f"/projects/{_quoted(project_path)}/merge_requests/{merge_request_iid}/notes"
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

    async def list_merge_request_discussions(
        self,
        project_path: str,
        merge_request_iid: int,
    ) -> list[dict[str, Any]]:
        return await self._paginate(
            f"/projects/{_quoted(project_path)}/merge_requests/"
            f"{merge_request_iid}/discussions"
        )

    async def create_merge_request_discussion(
        self,
        project_path: str,
        merge_request_iid: int,
        *,
        body: str,
        position: dict[str, Any],
    ) -> tuple[str, str]:
        response = await self._request(
            "POST",
            f"/projects/{_quoted(project_path)}/merge_requests/"
            f"{merge_request_iid}/discussions",
            json={"body": body, "position": position},
        )
        if not isinstance(response, dict):
            raise GitLabClientError("GitLab discussion response is not an object.")
        notes = response.get("notes")
        if not isinstance(notes, list) or not notes or not isinstance(notes[0], dict):
            raise GitLabClientError("GitLab discussion response has no note.")
        return str(notes[0]["id"]), str(response["id"])

    async def update_merge_request_discussion_note(
        self,
        project_path: str,
        merge_request_iid: int,
        discussion_id: str,
        note_id: str,
        body: str,
    ) -> tuple[str, str]:
        response = await self._request(
            "PUT",
            f"/projects/{_quoted(project_path)}/merge_requests/"
            f"{merge_request_iid}/discussions/{discussion_id}/notes/{note_id}",
            json={"body": body},
        )
        return str(response["id"]), discussion_id

    async def _paginate(self, path: str) -> list[dict[str, Any]]:
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            response = await self._request(
                "GET", path, params={"per_page": 100, "page": page}
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
                f"GitLab request failed ({exc.response.status_code} {method} {path})."
            ) from exc
        except httpx.RequestError as exc:
            raise GitLabClientError(
                f"GitLab request failed ({method} {path}): transport error."
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
            )
        )
    return changed_files


class GitLabPlatform:
    """Owns GitLab credentials, native API access, and client lifecycle."""

    key = "gitlab"

    def __init__(
        self,
        client: GitLabClient | None,
        config: Settings | None = None,
        *,
        diagnostics_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.diagnostics_transport = diagnostics_transport

    async def get_credential(self, target: str, scope: str) -> Credential:
        del target
        if scope == "webhook":
            secret = getattr(self.config, "gitlab_webhook_secret", None)
            return Credential(value=secret or "")
        if scope not in {"git:read", "comment:write", "query:read"}:
            raise ProviderCapabilityError(
                f"GitLab does not support credential scope {scope!r}.",
                provider=self.key,
                operation="get_credential",
            )
        client = self.require_client("get_credential")
        return Credential(value=getattr(client, "token", None) or "", username="oauth2")

    async def aclose(self) -> None:
        if self.client is not None and hasattr(self.client, "aclose"):
            await self.client.aclose()

    def require_client(self, operation: str) -> GitLabClient:
        if self.client is None:
            raise ProviderCapabilityError(
                "GitLab client is not configured.",
                provider=self.key,
                operation=operation,
            )
        return self.client

    def clone_url(self, repository: str) -> str:
        client = self.require_client("resolve_git_checkout")
        return gitlab_clone_url(
            getattr(client, "api_base_url", "https://gitlab.com/api/v4"),
            repository,
        )

    async def get_merge_request(self, repository: str, number: int) -> dict[str, Any]:
        return await self.require_client("pull_request.get").get_merge_request(
            repository,
            number,
        )

    async def get_merge_request_changes(
        self, repository: str, number: int
    ) -> dict[str, Any]:
        return await self.require_client(
            "pull_request.changes.list"
        ).get_merge_request_changes(repository, number)

    async def list_merge_request_comments(
        self, repository: str, number: int
    ) -> list[dict[str, Any]]:
        notes = await self.require_client(
            "pull_request.comments.list"
        ).list_merge_request_notes(repository, number)
        return [
            note.model_dump(mode="json") if isinstance(note, BaseModel) else dict(note)
            for note in notes
        ]

    async def publish_comment(
        self,
        repository: str,
        number: int,
        item: CommentPublishItem,
    ) -> tuple[str, str | None]:
        client = self.require_client("publish_comments")
        body = _idempotent_comment_body(item.body, item.idempotency_key)
        comment_id = item.comment_id or item.thread_id
        thread_id = item.thread_id
        if comment_id is None and item.idempotency_key:
            marker = _idempotency_marker(item.idempotency_key)
            if item.kind == "line":
                comment_id, thread_id = await self._find_discussion_comment(
                    repository,
                    number,
                    marker,
                )
            else:
                comments = await client.list_merge_request_notes(repository, number)
                comment_id = next(
                    (
                        str(comment.id)
                        for comment in comments
                        if marker in (comment.body or "")
                    ),
                    None,
                )
        if item.kind == "line":
            if comment_id is not None and thread_id is not None:
                return await client.update_merge_request_discussion_note(
                    repository,
                    number,
                    thread_id,
                    comment_id,
                    body,
                )
            if comment_id is not None:
                updated = await client.update_merge_request_note(
                    repository,
                    number,
                    comment_id,
                    body,
                )
                return updated, thread_id
            merge_request = await self.get_merge_request(repository, number)
            diff_refs = merge_request.get("diff_refs")
            if not isinstance(diff_refs, dict):
                raise GitLabClientError(
                    "GitLab merge request has no diff refs for a line comment."
                )
            base_sha = diff_refs.get("base_sha")
            start_sha = diff_refs.get("start_sha")
            if not isinstance(base_sha, str) or not isinstance(start_sha, str):
                raise GitLabClientError(
                    "GitLab merge request has incomplete diff refs."
                )
            assert item.commit_sha is not None
            assert item.path is not None
            assert item.line is not None
            return await client.create_merge_request_discussion(
                repository,
                number,
                body=body,
                position={
                    "position_type": "text",
                    "base_sha": base_sha,
                    "start_sha": start_sha,
                    "head_sha": item.commit_sha,
                    "new_path": item.path,
                    "new_line": item.line,
                },
            )
        if comment_id is not None:
            updated = await client.update_merge_request_note(
                repository,
                number,
                comment_id,
                body,
            )
            return updated, None
        created = await client.create_merge_request_note(repository, number, body)
        return created, None

    async def _find_discussion_comment(
        self,
        repository: str,
        number: int,
        marker: str,
    ) -> tuple[str | None, str | None]:
        discussions = await self.require_client(
            "publish_comments"
        ).list_merge_request_discussions(repository, number)
        for discussion in discussions:
            discussion_id = discussion.get("id")
            notes = discussion.get("notes")
            if not isinstance(discussion_id, str | int) or not isinstance(notes, list):
                continue
            for note in notes:
                if not isinstance(note, dict) or marker not in str(
                    note.get("body", "")
                ):
                    continue
                note_id = note.get("id")
                if isinstance(note_id, str | int):
                    return str(note_id), str(discussion_id)
        return None, None

    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None,
    ) -> ReviewCommentRef | None:
        from review_orchestrator.integrations.comments import (
            publish_gitlab_summary_comment,
        )

        return await publish_gitlab_summary_comment(
            session,
            review_run,
            gitlab_client=self.require_client("publish_summary_comment"),
            status_text=status_text,
            finding_stats=finding_stats,
        )

    async def diagnose_permissions(self, payload: Any) -> Any:
        if self.config is None:
            raise ProviderCapabilityError(
                "GitLab diagnostics settings are not configured.",
                provider=self.key,
                operation="diagnose_permissions",
            )
        from review_orchestrator.integrations.platform_diagnostics import (
            diagnose_gitlab_permissions,
        )

        return await diagnose_gitlab_permissions(
            self.config,
            payload,
            transport=self.diagnostics_transport,
        )


class GitLabProvider:
    """Converts GitLab-native protocols into provider-neutral contracts."""

    key = "gitlab"
    provider = "gitlab"

    def __init__(
        self,
        platform: GitLabPlatform | GitLabClient | None = None,
        *,
        settings: Settings | None = None,
        diagnostics_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.platform = (
            platform
            if isinstance(platform, GitLabPlatform)
            else GitLabPlatform(
                platform,
                settings,
                diagnostics_transport=diagnostics_transport,
            )
        )

    @property
    def client(self) -> GitLabClient | None:
        return self.platform.client

    @property
    def settings(self) -> Settings | None:
        return self.platform.config

    @property
    def diagnostics_transport(self) -> httpx.AsyncBaseTransport | None:
        return self.platform.diagnostics_transport

    def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        settings: Settings,
    ) -> ParsedProviderWebhook:
        return _parse_gitlab_webhook(
            headers=headers,
            raw_body=raw_body,
            settings=settings,
            webhook_secret=getattr(settings, "gitlab_webhook_secret", None),
        )

    async def normalize_webhook(
        self,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> NormalizedWebhook:
        credential = await self.platform.get_credential("", "webhook")
        parsed = _parse_gitlab_webhook(
            headers=headers,
            raw_body=raw_body,
            settings=self.platform.config,
            webhook_secret=credential.value or None,
        )
        return NormalizedWebhook(
            delivery_id=parsed.delivery_id,
            provider_event=parsed.provider_event,
            payload=parsed.payload,
        )

    async def resolve_git_checkout(
        self,
        request: GitCheckoutRequest,
    ) -> GitCheckoutTarget:
        credential = await self.platform.get_credential(
            request.repository,
            "git:read",
        )
        return GitCheckoutTarget(
            remote_url=request.clone_url or self.platform.clone_url(request.repository),
            username=credential.username,
            password=credential.value or None,
            expires_at=credential.expires_at,
        )

    async def publish_comments(
        self,
        request: CommentPublishRequest,
    ) -> CommentPublishResult:
        await self.platform.get_credential(request.repository, "comment:write")
        results: list[PublishedComment] = []
        for item in request.comments:
            try:
                comment_id, thread_id = await self.platform.publish_comment(
                    request.repository,
                    request.pull_request_number,
                    item,
                )
                results.append(
                    PublishedComment(
                        kind=item.kind,
                        comment_id=comment_id,
                        thread_id=thread_id,
                        url=self._comment_url(
                            request.repository,
                            request.pull_request_number,
                            comment_id,
                        ),
                    )
                )
            except GitLabClientError as exc:
                results.append(
                    PublishedComment(
                        kind=item.kind,
                        error=_safe_platform_error(exc),
                    )
                )
        failed = sum(item.error is not None for item in results)
        return CommentPublishResult(
            comments=tuple(results),
            published=len(results) - failed,
            failed=failed,
        )

    async def query(self, request: PlatformQueryRequest) -> PlatformQueryResult:
        await self.platform.get_credential(request.repository, "query:read")
        try:
            if request.action == "pull_request.get":
                merge_request = await self.platform.get_merge_request(
                    request.repository,
                    request.pull_request_number,
                )
                return PlatformQueryResult(
                    action=request.action,
                    data=_gitlab_query_pull_request(
                        request.repository,
                        request.pull_request_number,
                        merge_request,
                    ),
                )
            if request.action == "pull_request.status.get":
                merge_request = await self.platform.get_merge_request(
                    request.repository,
                    request.pull_request_number,
                )
                return PlatformQueryResult(
                    action=request.action,
                    data={
                        "status": _gitlab_status(merge_request.get("state")),
                        "state": _gitlab_status(merge_request.get("state")),
                        "merged": merge_request.get("state") == "merged",
                        "head_sha": merge_request.get("sha"),
                    },
                )
            if request.action == "pull_request.changes.list":
                response = await self.platform.get_merge_request_changes(
                    request.repository,
                    request.pull_request_number,
                )
                raw_items = response.get("changes", [])
                items = raw_items if isinstance(raw_items, list) else []
            elif request.action == "pull_request.comments.list":
                items = await self.platform.list_merge_request_comments(
                    request.repository,
                    request.pull_request_number,
                )
            else:
                raise ProviderCapabilityError(
                    f"Unsupported query action: {request.action}",
                    provider=self.provider,
                    operation="query",
                )
        except GitLabClientError as exc:
            raise self._operation_error(request.action, exc) from exc
        page, next_cursor = _query_page(items, request.cursor, request.page_size)
        converter = (
            _gitlab_query_change
            if request.action == "pull_request.changes.list"
            else _gitlab_query_comment
        )
        return PlatformQueryResult(
            action=request.action,
            items=tuple(converter(item) for item in page),
            next_cursor=next_cursor,
        )

    async def get_pull_request_context(
        self,
        task: AgentTask,
    ) -> PullRequestContext:
        operation = "get_pull_request_context"
        try:
            merge_request = await self.platform.get_merge_request(
                task.repo_full_name,
                task.pull_request_number,
            )
        except GitLabClientError as exc:
            raise self._operation_error(operation, exc) from exc
        return context_from_merge_request_task(task, merge_request)

    async def get_workspace_checkout(
        self,
        repo_full_name: str,
        *,
        clone_url: str | None = None,
    ) -> ProviderWorkspaceCheckout:
        resolved = await self.resolve_git_checkout(
            GitCheckoutRequest(repository=repo_full_name, clone_url=clone_url)
        )
        return ProviderWorkspaceCheckout(
            clone_url=resolved.remote_url,
            auth_token=resolved.password,
            auth_username=resolved.username or "oauth2",
        )

    async def list_changed_files(
        self,
        review_run: ReviewRun,
    ) -> list[ChangedFile]:
        operation = "list_changed_files"
        try:
            return await fetch_gitlab_changed_files(
                self.platform.require_client(operation),
                project_path=review_run.repo_full_name,
                merge_request_iid=review_run.pull_request_number,
            )
        except GitLabClientError as exc:
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
        try:
            return await self.platform.publish_summary_comment(
                session,
                review_run,
                status_text=status_text,
                finding_stats=finding_stats,
            )
        except GitLabClientError as exc:
            raise self._operation_error(operation, exc) from exc

    async def diagnose_permissions(self, payload: Any) -> Any:
        return await self.platform.diagnose_permissions(payload)

    def agent_task_comment_url(self, task: AgentTask) -> str | None:
        if not task.response_comment_id:
            return None
        return self._comment_url(
            task.repo_full_name,
            task.pull_request_number,
            task.response_comment_id,
        )

    def _comment_url(self, repository: str, number: int, comment_id: str) -> str:
        repository_url = self.platform.clone_url(repository).removesuffix(".git")
        return f"{repository_url}/-/merge_requests/{number}#note_{comment_id}"

    def _require_client(self, operation: str) -> GitLabClient:
        return self.platform.require_client(operation)

    def _operation_error(
        self,
        operation: str,
        error: GitLabClientError,
    ) -> ProviderOperationError:
        message = _safe_platform_error(error)
        error.args = (message,)
        return ProviderOperationError(
            message,
            provider=self.provider,
            operation=operation,
        )


# Compatibility name retained for Worker, Workspace, and third-party callers.
GitLabAdapter = GitLabProvider


def _parse_gitlab_webhook(
    *,
    headers: dict[str, str],
    raw_body: bytes,
    settings: Any | None,
    webhook_secret: str | None,
) -> ParsedProviderWebhook:
    del settings
    normalized_headers = lower_headers(headers)
    delivery_id = normalized_headers.get(
        "x-gitlab-event-uuid"
    ) or normalized_headers.get("x-request-id")
    event_name = normalized_headers.get("x-gitlab-event")
    if not delivery_id:
        raise ProviderPayloadError("Missing X-Gitlab-Event-UUID header.")
    if not event_name:
        raise ProviderPayloadError("Missing X-Gitlab-Event header.")

    token = normalized_headers.get("x-gitlab-token")
    if webhook_secret and (
        token is None or not hmac.compare_digest(token, webhook_secret)
    ):
        raise ProviderSignatureError("Invalid GitLab webhook token.")

    payload = _parse_json_body(raw_body)
    event = normalize_gitlab_event(event_name, payload)
    return ParsedProviderWebhook(
        delivery_id=delivery_id,
        provider_event=event,
        payload=payload,
        raw_body=raw_body,
    )


def _gitlab_query_pull_request(
    repository: str,
    number: int,
    merge_request: dict[str, Any],
) -> dict[str, Any]:
    author = merge_request.get("author")
    diff_refs = merge_request.get("diff_refs")
    return {
        "provider": "gitlab",
        "repository": repository,
        "number": merge_request.get("iid", number),
        "provider_id": _id_to_str(merge_request.get("id")),
        "title": merge_request.get("title"),
        "author": _gitlab_author(author),
        "status": _gitlab_status(merge_request.get("state")),
        "url": merge_request.get("web_url"),
        "base_ref": merge_request.get("target_branch"),
        "base_sha": (
            diff_refs.get("base_sha") if isinstance(diff_refs, dict) else None
        ),
        "head_ref": merge_request.get("source_branch"),
        "head_sha": merge_request.get("sha"),
    }


def _gitlab_query_change(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("new_file"):
        status = "added"
    elif item.get("deleted_file"):
        status = "removed"
    elif item.get("renamed_file"):
        status = "renamed"
    else:
        status = "modified"
    return {
        "path": item.get("new_path") or item.get("old_path"),
        "previous_path": item.get("old_path"),
        "status": status,
        "patch": item.get("diff"),
        "additions": None,
        "deletions": None,
    }


def _gitlab_query_comment(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author")
    position = item.get("position")
    return {
        "comment_id": _id_to_str(item.get("id")),
        "thread_id": _id_to_str(item.get("discussion_id")),
        "kind": "line" if isinstance(position, dict) else "summary",
        "body": item.get("body"),
        "author": _gitlab_author(author),
        "url": item.get("web_url"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "path": position.get("new_path") if isinstance(position, dict) else None,
        "line": position.get("new_line") if isinstance(position, dict) else None,
    }


def _gitlab_status(value: Any) -> str | None:
    if value == "opened":
        return "open"
    return value if isinstance(value, str) else None


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
    snapshot = gitlab_pull_request_snapshot(payload)
    if internal_event is not None and snapshot is None:
        raise ProviderPayloadError(
            "GitLab merge request payload is missing MR identity fields."
        )
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
        pull_request=snapshot,
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


def gitlab_pull_request_snapshot(
    payload: dict[str, Any],
) -> PullRequestSnapshot | None:
    """Normalize the complete GitLab MR payload at the integration boundary."""

    attrs = payload.get("object_attributes")
    project = payload.get("project")
    if not isinstance(attrs, dict) or not isinstance(project, dict):
        return None
    repository = _optional_str(project.get("path_with_namespace"))
    number = _int_or_none(attrs.get("iid"))
    last_commit = attrs.get("last_commit")
    head_sha = (
        _optional_str(last_commit.get("id")) if isinstance(last_commit, dict) else None
    ) or _optional_str(attrs.get("last_commit_id"))
    if not repository or number is None or not head_sha:
        return None
    target = attrs.get("target")
    source = attrs.get("source")
    return PullRequestSnapshot(
        repository=repository,
        number=number,
        head_sha=head_sha,
        provider_repo_id=_id_to_str(project.get("id")),
        provider_pr_id=_id_to_str(attrs.get("id")),
        title=_optional_str(attrs.get("title")),
        author_login=_gitlab_author(payload.get("user")),
        base_ref=_optional_str(attrs.get("target_branch")),
        base_sha=_optional_str(attrs.get("target_branch_sha")),
        head_ref=_optional_str(attrs.get("source_branch")),
        base_repo_full_name=_gitlab_project_path(target),
        head_repo_full_name=_gitlab_project_path(source),
        status=_optional_str(attrs.get("state")) or "open",
        html_url=_optional_str(attrs.get("url")),
        closed_at=_parse_datetime(attrs.get("closed_at")),
        merged_at=_parse_datetime(attrs.get("merged_at")),
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _quoted(value: str) -> str:
    return quote(value, safe="")


def context_from_merge_request_task(
    task: AgentTask,
    merge_request: dict[str, Any],
) -> PullRequestContext:
    from review_orchestrator.domain.models import PullRequestContext

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


def _id_to_str(value: Any) -> str | None:
    if isinstance(value, int | str):
        return str(value)
    return None


def _gitlab_author(author: Any) -> str | None:
    if not isinstance(author, dict):
        return None
    return _optional_str(author.get("username")) or _optional_str(author.get("name"))


def _gitlab_project_path(project: Any) -> str | None:
    if not isinstance(project, dict):
        return None
    return _optional_str(project.get("path_with_namespace"))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _source_commit_changed(attrs: dict[str, Any]) -> bool:
    changes = attrs.get("changes")
    if not isinstance(changes, dict):
        return False
    for key in ("last_commit", "source_branch_sha"):
        value = changes.get(key)
        if isinstance(value, dict) and value.get("previous") != value.get("current"):
            return True
    return False
