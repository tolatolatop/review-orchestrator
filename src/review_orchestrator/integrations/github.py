"""GitHub webhook normalization, API client, and provider adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

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
    ProviderWebhookError,
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
    pull_request: PullRequestSnapshot | None = None

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
            pull_request=self.pull_request,
        )


class GitHubPlatform:
    """Owns GitHub credentials, native API access, and client lifecycle."""

    key = "github"

    def __init__(
        self,
        client: GitHubClient | None,
        config: Settings | None = None,
        *,
        diagnostics_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.diagnostics_transport = diagnostics_transport

    async def get_credential(self, target: str, scope: str) -> Credential:
        if scope == "webhook":
            secret = getattr(self.config, "github_webhook_secret", None)
            return Credential(value=secret or "")
        if scope not in {"git:read", "comment:write", "query:read"}:
            raise ProviderCapabilityError(
                f"GitHub does not support credential scope {scope!r}.",
                provider=self.key,
                operation="get_credential",
            )
        client = self.require_client("get_credential")
        try:
            token = await client.get_token(target)
        except GitHubClientError as exc:
            message = _safe_platform_error(exc)
            exc.args = (message,)
            raise ProviderOperationError(
                message,
                provider=self.key,
                operation="get_credential",
            ) from exc
        return Credential(
            value=token or "",
            username="x-access-token",
        )

    async def aclose(self) -> None:
        if self.client is not None and hasattr(self.client, "aclose"):
            await self.client.aclose()

    def require_client(self, operation: str) -> GitHubClient:
        if self.client is None:
            raise ProviderCapabilityError(
                "GitHub client is not configured.",
                provider=self.key,
                operation=operation,
            )
        return self.client

    def clone_url(self, repository: str) -> str:
        client = self.require_client("resolve_git_checkout")
        return github_clone_url(
            getattr(client, "api_base_url", "https://api.github.com"),
            repository,
        )

    async def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        return await self.require_client("pull_request.get").get_pull_request(
            repository,
            number,
        )

    async def list_pull_request_files(
        self, repository: str, number: int
    ) -> list[dict[str, Any]]:
        return await self.require_client(
            "pull_request.changes.list"
        ).list_pull_request_files(repository, number)

    async def list_pull_request_comments(
        self, repository: str, number: int
    ) -> list[dict[str, Any]]:
        client = self.require_client("pull_request.comments.list")
        issue_comments = await client.list_issue_comments(repository, number)
        review_comments = await client.list_review_comments(repository, number)
        comments = [*issue_comments, *review_comments]
        return [
            comment.model_dump(mode="json")
            if isinstance(comment, BaseModel)
            else dict(comment)
            for comment in comments
        ]

    async def publish_comment(
        self,
        repository: str,
        number: int,
        item: CommentPublishItem,
    ) -> str:
        client = self.require_client("publish_comments")
        body = _idempotent_comment_body(item.body, item.idempotency_key)
        comment_id = item.comment_id or item.thread_id
        if comment_id is None and item.idempotency_key:
            marker = _idempotency_marker(item.idempotency_key)
            comments = (
                await client.list_review_comments(repository, number)
                if item.kind == "line"
                else await client.list_issue_comments(repository, number)
            )
            comment_id = next(
                (
                    str(comment.id)
                    for comment in comments
                    if marker in (comment.body or "")
                ),
                None,
            )
        if item.kind == "line":
            if comment_id is not None:
                return await client.update_review_comment(
                    repository,
                    comment_id,
                    body,
                )
            assert item.path is not None
            assert item.line is not None
            assert item.commit_sha is not None
            return await client.create_review_comment(
                repository,
                number,
                body=body,
                commit_id=item.commit_sha,
                path=item.path,
                line=item.line,
            )
        if comment_id is not None:
            return await client.update_issue_comment(repository, comment_id, body)
        return await client.create_issue_comment(repository, number, body)

    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None,
    ) -> ReviewCommentRef | None:
        from review_orchestrator.integrations.comments import (
            publish_github_summary_comment,
        )

        return await publish_github_summary_comment(
            session,
            review_run,
            github_client=self.require_client("publish_summary_comment"),
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
        from review_orchestrator.integrations.comments import (
            publish_github_line_comments,
        )

        return await publish_github_line_comments(
            session,
            review_run,
            github_client=self.require_client("publish_line_comments"),
            changed_files=changed_files,
        )

    async def publish_agent_task_comment(
        self,
        session: AsyncSession,
        task: AgentTask,
        *,
        state: str,
    ) -> str:
        from review_orchestrator.integrations.comments import (
            publish_github_agent_task_comment,
        )

        return await publish_github_agent_task_comment(
            session,
            task,
            github_client=self.require_client("publish_agent_task_comment"),
            state=state,
        )

    async def diagnose_permissions(self, payload: Any) -> Any:
        if self.config is None:
            raise ProviderCapabilityError(
                "GitHub diagnostics settings are not configured.",
                provider=self.key,
                operation="diagnose_permissions",
            )
        from review_orchestrator.integrations.platform_diagnostics import (
            diagnose_github_permissions,
        )

        return await diagnose_github_permissions(
            self.config,
            payload,
            transport=self.diagnostics_transport,
            github_client=self.require_client("diagnose_permissions"),
        )


class GitHubProvider:
    """Converts GitHub-native protocols into provider-neutral contracts."""

    key = "github"
    provider = "github"

    def __init__(
        self,
        platform: GitHubPlatform | GitHubClient | None = None,
        *,
        settings: Settings | None = None,
        diagnostics_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.platform = (
            platform
            if isinstance(platform, GitHubPlatform)
            else GitHubPlatform(
                platform,
                settings,
                diagnostics_transport=diagnostics_transport,
            )
        )

    @property
    def client(self) -> GitHubClient | None:
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
        return _parse_github_webhook(
            headers=headers,
            raw_body=raw_body,
            settings=settings,
            webhook_secret=getattr(settings, "github_webhook_secret", None),
        )

    async def normalize_webhook(
        self,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> NormalizedWebhook:
        credential = await self.platform.get_credential("", "webhook")
        parsed = _parse_github_webhook(
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
                comment_id = await self.platform.publish_comment(
                    request.repository,
                    request.pull_request_number,
                    item,
                )
                results.append(
                    PublishedComment(
                        kind=item.kind,
                        comment_id=comment_id,
                        thread_id=comment_id if item.kind == "line" else None,
                        url=self._comment_url(
                            request.repository,
                            request.pull_request_number,
                            comment_id,
                            item.kind,
                        ),
                    )
                )
            except GitHubClientError as exc:
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
                pull_request = await self.platform.get_pull_request(
                    request.repository,
                    request.pull_request_number,
                )
                return PlatformQueryResult(
                    action=request.action,
                    data=_github_query_pull_request(
                        request.repository,
                        request.pull_request_number,
                        pull_request,
                    ),
                )
            if request.action == "pull_request.status.get":
                pull_request = await self.platform.get_pull_request(
                    request.repository,
                    request.pull_request_number,
                )
                return PlatformQueryResult(
                    action=request.action,
                    data={
                        "status": (
                            "merged"
                            if pull_request.get("merged")
                            else pull_request.get("state")
                        ),
                        "state": pull_request.get("state"),
                        "merged": bool(pull_request.get("merged")),
                        "head_sha": _sha(pull_request.get("head")),
                    },
                )
            if request.action == "pull_request.changes.list":
                items = await self.platform.list_pull_request_files(
                    request.repository,
                    request.pull_request_number,
                )
            elif request.action == "pull_request.comments.list":
                items = await self.platform.list_pull_request_comments(
                    request.repository,
                    request.pull_request_number,
                )
            else:
                raise ProviderCapabilityError(
                    f"Unsupported query action: {request.action}",
                    provider=self.provider,
                    operation="query",
                )
        except GitHubClientError as exc:
            raise self._operation_error(request.action, exc) from exc
        page, next_cursor = _query_page(items, request.cursor, request.page_size)
        converter = (
            _github_query_change
            if request.action == "pull_request.changes.list"
            else _github_query_comment
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
            pull_request = await self.platform.get_pull_request(
                task.repo_full_name,
                task.pull_request_number,
            )
        except GitHubClientError as exc:
            raise self._operation_error(operation, exc) from exc
        return context_from_pull_request_task(task, pull_request)

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
            auth_username=resolved.username or "x-access-token",
        )

    async def list_changed_files(
        self,
        review_run: ReviewRun,
    ) -> list[ChangedFile]:
        operation = "list_changed_files"
        try:
            return await fetch_changed_files(
                self.platform.require_client(operation),
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
        try:
            return await self.platform.publish_summary_comment(
                session,
                review_run,
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
        try:
            return await self.platform.publish_line_comments(
                session,
                review_run,
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
        try:
            return await self.platform.publish_agent_task_comment(
                session,
                task,
                state=state,
            )
        except GitHubClientError as exc:
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
            "agent",
        )

    def _comment_url(
        self,
        repository: str,
        number: int,
        comment_id: str,
        kind: str,
    ) -> str:
        repository_url = self.platform.clone_url(repository).removesuffix(".git")
        anchor = "discussion_r" if kind == "line" else "issuecomment-"
        return f"{repository_url}/pull/{number}#{anchor}{comment_id}"

    def _require_client(self, operation: str) -> GitHubClient:
        return self.platform.require_client(operation)

    def _operation_error(
        self,
        operation: str,
        error: GitHubClientError,
    ) -> ProviderOperationError:
        message = _safe_platform_error(error)
        error.args = (message,)
        return ProviderOperationError(
            message,
            provider=self.provider,
            operation=operation,
        )


# Compatibility name retained for Worker, Workspace, and third-party callers.
GitHubAdapter = GitHubProvider


class GitHubClientError(RuntimeError):
    pass


def github_clone_url(api_base_url: str, repo_full_name: str) -> str:
    parsed = urlsplit(api_base_url)
    if parsed.hostname == "api.github.com":
        return f"https://github.com/{repo_full_name}.git"
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v3"):
        path = path[: -len("/api/v3")]
    origin = urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
    return f"{origin}/{repo_full_name}.git"


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

    async def list_review_comments(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> list[GitHubComment]:
        items = await self._paginate(
            f"/repos/{repo_full_name}/pulls/{pull_request_number}/comments",
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

    async def update_review_comment(
        self,
        repo_full_name: str,
        comment_id: str,
        body: str,
    ) -> str:
        response = await self._request(
            "PATCH",
            f"/repos/{repo_full_name}/pulls/comments/{comment_id}",
            repo_full_name=repo_full_name,
            json={"body": body},
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
                f"GitHub request failed ({exc.response.status_code} {method} {path})."
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubClientError(
                f"GitHub request failed ({method} {path}): transport error."
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


def _idempotency_marker(key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"<!-- review-orchestrator:{digest} -->"


def _idempotent_comment_body(body: str, key: str | None) -> str:
    if key is None:
        return body
    marker = _idempotency_marker(key)
    return body if marker in body else f"{body}\n\n{marker}"


def _safe_platform_error(error: Exception) -> str:
    message = str(error).splitlines()[0][:1000]
    return re.sub(
        r"(?i)((?:\bbearer\s+)|(?:\b(?:private-token|token|password)\s*[:=]\s*))"
        r"[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )


def _query_page(
    items: list[dict[str, Any]],
    cursor: str | None,
    page_size: int,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        offset = int(cursor) if cursor is not None else 0
    except ValueError as exc:
        raise ProviderCapabilityError(
            "Query cursor must be a non-negative integer.",
            operation="query",
        ) from exc
    if offset < 0:
        raise ProviderCapabilityError(
            "Query cursor must be a non-negative integer.",
            operation="query",
        )
    page = items[offset : offset + page_size]
    next_offset = offset + len(page)
    return page, str(next_offset) if next_offset < len(items) else None


def _github_query_pull_request(
    repository: str,
    number: int,
    pull_request: dict[str, Any],
) -> dict[str, Any]:
    base = pull_request.get("base")
    head = pull_request.get("head")
    status = "merged" if pull_request.get("merged") else pull_request.get("state")
    return {
        "provider": "github",
        "repository": repository,
        "number": pull_request.get("number", number),
        "provider_id": _id_to_str(pull_request.get("id")),
        "title": pull_request.get("title"),
        "author": _login(pull_request.get("user")),
        "status": status,
        "url": pull_request.get("html_url"),
        "base_ref": _ref(base),
        "base_sha": _sha(base),
        "head_ref": _ref(head),
        "head_sha": _sha(head),
    }


def _github_query_change(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": item.get("filename"),
        "previous_path": item.get("previous_filename"),
        "status": item.get("status"),
        "patch": item.get("patch"),
        "additions": item.get("additions"),
        "deletions": item.get("deletions"),
    }


def _github_query_comment(item: dict[str, Any]) -> dict[str, Any]:
    user = item.get("user")
    return {
        "comment_id": _id_to_str(item.get("id")),
        "thread_id": _id_to_str(item.get("pull_request_review_id")),
        "kind": "line" if item.get("path") else "summary",
        "body": item.get("body"),
        "author": _login(user),
        "url": item.get("html_url"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "path": item.get("path"),
        "line": item.get("line") or item.get("original_line"),
    }


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


def _parse_github_webhook(
    *,
    headers: dict[str, str],
    raw_body: bytes,
    settings: Any | None,
    webhook_secret: str | None,
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
        webhook_secret,
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

    snapshot = github_pull_request_snapshot(payload)
    if internal_event is not None and snapshot is None:
        raise GitHubPayloadError(
            "GitHub pull_request payload is missing PR identity fields."
        )
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
        pull_request=snapshot,
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


def github_pull_request_snapshot(
    payload: dict[str, Any],
) -> PullRequestSnapshot | None:
    """Normalize the complete GitHub PR payload at the integration boundary."""

    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        return None
    repository_name = _optional_str(repository.get("full_name"))
    number = pull_request.get("number")
    head_sha = _pull_request_head_sha(payload)
    if not repository_name or not isinstance(number, int) or not head_sha:
        return None
    base = pull_request.get("base")
    head = pull_request.get("head")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None
    status = (
        "merged"
        if pull_request.get("merged") is True
        else (_optional_str(pull_request.get("state")) or "open")
    )
    return PullRequestSnapshot(
        repository=repository_name,
        number=number,
        head_sha=head_sha,
        provider_repo_id=_id_to_str(repository.get("id")),
        provider_pr_id=_id_to_str(pull_request.get("id")),
        title=_optional_str(pull_request.get("title")),
        author_login=_login(pull_request.get("user")),
        base_ref=_ref(base),
        base_sha=_sha(base),
        head_ref=_ref(head),
        base_repo_full_name=_repo_full_name(base_repo),
        head_repo_full_name=_repo_full_name(head_repo),
        status=status,
        html_url=_optional_str(pull_request.get("html_url")),
        closed_at=parse_github_datetime(pull_request.get("closed_at")),
        merged_at=parse_github_datetime(pull_request.get("merged_at")),
    )


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
