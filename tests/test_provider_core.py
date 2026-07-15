import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.github import (
    GitHubClientError,
    GitHubComment,
    GitHubPlatform,
    GitHubProvider,
)
from review_orchestrator.gitlab import GitLabNote, GitLabPlatform, GitLabProvider
from review_orchestrator.main import create_app
from review_orchestrator.providers import (
    CommentPublishItem,
    CommentPublishRequest,
    GitCheckoutRequest,
    PlatformQueryRequest,
    ProviderCapabilityError,
    ProviderDescriptor,
    ProviderRegistry,
    ProviderRuntime,
)


class FakeGitHubCoreClient:
    api_base_url = "https://github.example/api/v3"

    def __init__(self) -> None:
        self.token = "platform-secret"
        self.closed = False
        self.credential_targets: list[str] = []
        self.comments: list[GitHubComment] = []
        self.create_count = 0

    async def get_token(self, repository: str) -> str:
        self.credential_targets.append(repository)
        return self.token

    async def aclose(self) -> None:
        self.closed = True

    async def get_pull_request(self, repository: str, number: int) -> dict:
        return {
            "id": 10,
            "number": number,
            "state": "open",
            "merged": False,
            "repository": repository,
            "head": {"sha": "b" * 40},
        }

    async def list_pull_request_files(
        self,
        repository: str,
        number: int,
    ) -> list[dict]:
        del repository, number
        return [
            {"filename": "one.py"},
            {"filename": "two.py"},
            {"filename": "three.py"},
        ]

    async def list_issue_comments(
        self,
        repository: str,
        number: int,
    ) -> list[GitHubComment]:
        del repository, number
        return list(self.comments)

    async def list_review_comments(
        self,
        repository: str,
        number: int,
    ) -> list[GitHubComment]:
        del repository, number
        return list(self.comments)

    async def create_issue_comment(
        self,
        repository: str,
        number: int,
        body: str,
    ) -> str:
        del repository, number
        if body.startswith("fail"):
            raise GitHubClientError("request failed token=platform-secret")
        self.create_count += 1
        comment_id = str(self.create_count)
        self.comments.append(GitHubComment(id=comment_id, body=body))
        return comment_id

    async def update_issue_comment(
        self,
        repository: str,
        comment_id: str,
        body: str,
    ) -> str:
        del repository
        for comment in self.comments:
            if str(comment.id) == comment_id:
                comment.body = body
                return comment_id
        raise GitHubClientError("comment not found")

    async def create_review_comment(
        self,
        repository: str,
        number: int,
        *,
        body: str,
        commit_id: str,
        path: str,
        line: int,
    ) -> str:
        del repository, number, commit_id, path, line
        return await self.create_issue_comment("", 0, body)

    async def update_review_comment(
        self,
        repository: str,
        comment_id: str,
        body: str,
    ) -> str:
        return await self.update_issue_comment(repository, comment_id, body)


class FakeGitLabCoreClient:
    api_base_url = "https://gitlab.example/api/v4"
    token = "gitlab-secret"

    def __init__(self) -> None:
        self.notes: list[GitLabNote] = []
        self.discussions: list[dict] = []
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def get_merge_request(self, repository: str, number: int) -> dict:
        return {
            "id": 20,
            "iid": number,
            "state": "opened",
            "sha": "d" * 40,
            "repository": repository,
            "diff_refs": {
                "base_sha": "a" * 40,
                "start_sha": "a" * 40,
                "head_sha": "d" * 40,
            },
        }

    async def get_merge_request_changes(
        self,
        repository: str,
        number: int,
    ) -> dict:
        del repository, number
        return {"changes": [{"new_path": "one.py"}, {"new_path": "two.py"}]}

    async def list_merge_request_notes(
        self,
        repository: str,
        number: int,
    ) -> list[GitLabNote]:
        del repository, number
        return list(self.notes)

    async def create_merge_request_note(
        self,
        repository: str,
        number: int,
        body: str,
    ) -> str:
        del repository, number
        note_id = str(len(self.notes) + 1)
        self.notes.append(GitLabNote(id=note_id, body=body))
        return note_id

    async def update_merge_request_note(
        self,
        repository: str,
        number: int,
        note_id: str,
        body: str,
    ) -> str:
        del repository, number
        for note in self.notes:
            if str(note.id) == note_id:
                note.body = body
                return note_id
        raise AssertionError("note not found")

    async def list_merge_request_discussions(
        self,
        repository: str,
        number: int,
    ) -> list[dict]:
        del repository, number
        return list(self.discussions)

    async def create_merge_request_discussion(
        self,
        repository: str,
        number: int,
        *,
        body: str,
        position: dict,
    ) -> tuple[str, str]:
        del repository, number
        discussion_id = f"thread-{len(self.discussions) + 1}"
        note_id = f"line-{len(self.discussions) + 1}"
        self.discussions.append(
            {
                "id": discussion_id,
                "notes": [{"id": note_id, "body": body, "position": position}],
            }
        )
        return note_id, discussion_id

    async def update_merge_request_discussion_note(
        self,
        repository: str,
        number: int,
        discussion_id: str,
        note_id: str,
        body: str,
    ) -> tuple[str, str]:
        del repository, number
        for discussion in self.discussions:
            if discussion["id"] == discussion_id:
                discussion["notes"][0]["body"] = body
                return note_id, discussion_id
        raise AssertionError("discussion not found")


def _runtime(
    settings: Settings,
    client: FakeGitHubCoreClient,
) -> tuple[GitHubPlatform, ProviderRuntime]:
    platform = GitHubPlatform(client, settings)
    provider = GitHubProvider(platform)
    runtime = ProviderRuntime(
        provider=provider,
        descriptor=ProviderDescriptor(
            key="github",
            kind="github",
            display_name="GitHub",
        ),
        close=platform.aclose,
    )
    return platform, runtime


async def test_platform_scoped_credentials_and_registry_lifecycle() -> None:
    settings = Settings(
        _env_file=None,
        github_webhook_secret="webhook-secret",
    )
    client = FakeGitHubCoreClient()
    platform, runtime = _runtime(settings, client)
    registry = ProviderRegistry()
    registered = registry.register(key="github", factory=lambda: runtime)

    webhook = await platform.get_credential("", "webhook")
    checkout = await platform.get_credential("example/repo", "git:read")

    assert registered is runtime
    assert registry.require("github") is runtime.provider
    assert webhook.value == "webhook-secret"
    assert checkout.value == "platform-secret"
    assert checkout.username == "x-access-token"
    assert client.credential_targets == ["example/repo"]
    with pytest.raises(ProviderCapabilityError, match="credential scope"):
        await platform.get_credential("example/repo", "admin")

    await registry.aclose()
    await registry.aclose()
    assert client.closed is True
    with pytest.raises(RuntimeError, match="already closed"):
        registry.register(key="github", factory=lambda: runtime)


async def test_provider_checkout_query_comments_idempotency_and_partial_failure() -> (
    None
):
    settings = Settings(_env_file=None)
    client = FakeGitHubCoreClient()
    provider = GitHubProvider(GitHubPlatform(client, settings))

    checkout = await provider.resolve_git_checkout(
        GitCheckoutRequest(repository="example/repo")
    )
    first_page = await provider.query(
        PlatformQueryRequest(
            action="pull_request.changes.list",
            repository="example/repo",
            pull_request_number=7,
            page_size=2,
        )
    )
    second_page = await provider.query(
        PlatformQueryRequest(
            action="pull_request.changes.list",
            repository="example/repo",
            pull_request_number=7,
            cursor=first_page.next_cursor,
            page_size=2,
        )
    )
    first_publish = await provider.publish_comments(
        CommentPublishRequest(
            repository="example/repo",
            pull_request_number=7,
            kind="summary",
            body="reviewed",
            idempotency_key="review-run-1",
        )
    )
    repeated_publish = await provider.publish_comments(
        CommentPublishRequest(
            repository="example/repo",
            pull_request_number=7,
            kind="summary",
            body="reviewed again",
            idempotency_key="review-run-1",
        )
    )
    partial = await provider.publish_comments(
        CommentPublishRequest(
            repository="example/repo",
            pull_request_number=7,
            comments=(
                CommentPublishItem(kind="agent", body="done"),
                CommentPublishItem(kind="summary", body="fail publication"),
            ),
        )
    )

    assert checkout.remote_url == "https://github.example/example/repo.git"
    assert checkout.username == "x-access-token"
    assert checkout.password == "platform-secret"
    assert [item["path"] for item in first_page.items] == ["one.py", "two.py"]
    assert first_page.next_cursor == "2"
    assert [item["path"] for item in second_page.items] == ["three.py"]
    assert second_page.next_cursor is None
    assert first_publish.comment_id == repeated_publish.comment_id == "1"
    assert client.create_count == 2
    assert partial.published == 1
    assert partial.failed == 1
    assert "platform-secret" not in (partial.comments[1].error or "")
    assert "[REDACTED]" in (partial.comments[1].error or "")


async def test_gitlab_uses_the_same_platform_and_provider_contracts() -> None:
    settings = Settings(
        _env_file=None,
        gitlab_webhook_secret="gitlab-webhook",
    )
    client = FakeGitLabCoreClient()
    platform = GitLabPlatform(client, settings)
    provider = GitLabProvider(platform)

    webhook_credential = await platform.get_credential("", "webhook")
    checkout = await provider.resolve_git_checkout(
        GitCheckoutRequest(repository="group/project")
    )
    changes = await provider.query(
        PlatformQueryRequest(
            action="pull_request.changes.list",
            repository="group/project",
            pull_request_number=8,
            page_size=1,
        )
    )
    published = await provider.publish_comments(
        CommentPublishRequest(
            repository="group/project",
            pull_request_number=8,
            kind="agent",
            body="done",
            idempotency_key="agent-task-8",
        )
    )
    repeated = await provider.publish_comments(
        CommentPublishRequest(
            repository="group/project",
            pull_request_number=8,
            kind="agent",
            body="done again",
            idempotency_key="agent-task-8",
        )
    )
    line = await provider.publish_comments(
        CommentPublishRequest(
            repository="group/project",
            pull_request_number=8,
            kind="line",
            body="line issue",
            idempotency_key="line-8",
            path="src/app.py",
            line=12,
            commit_sha="d" * 40,
        )
    )
    repeated_line = await provider.publish_comments(
        CommentPublishRequest(
            repository="group/project",
            pull_request_number=8,
            kind="line",
            body="line issue updated",
            idempotency_key="line-8",
            path="src/app.py",
            line=12,
            commit_sha="d" * 40,
        )
    )

    assert webhook_credential.value == "gitlab-webhook"
    assert checkout.remote_url == "https://gitlab.example/group/project.git"
    assert checkout.username == "oauth2"
    assert checkout.password == "gitlab-secret"
    assert changes.items[0]["path"] == "one.py"
    assert changes.items[0]["status"] == "modified"
    assert changes.next_cursor == "1"
    assert published.comment_id == repeated.comment_id == "1"
    assert len(client.notes) == 1
    assert line.comment_id == repeated_line.comment_id == "line-1"
    assert line.thread_id == repeated_line.thread_id == "thread-1"
    assert len(client.discussions) == 1
    assert client.discussions[0]["notes"][0]["position"]["new_line"] == 12

    await platform.aclose()
    assert client.closed is True


def test_provider_core_http_auth_contracts_and_secret_redaction(tmp_path: Path) -> None:
    webhook_secret = "webhook-secret"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/provider-core.db",
        providers_enabled="github",
        provider_core_api_token="core-secret",
        github_webhook_secret=webhook_secret,
    )
    github_client = FakeGitHubCoreClient()
    _, runtime = _runtime(settings, github_client)
    body = (Path(__file__).parent / "fixtures/github_pr_opened.json").read_bytes()
    signature = (
        "sha256=" + hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    core_auth = {"Authorization": "Bearer core-secret"}

    with TestClient(create_app(settings)) as client:
        client.app.state.provider_registry = ProviderRegistry(runtimes=[runtime])
        unauthenticated = client.post(
            "/v1/git/github/resolve-checkout",
            json={"repository": "example/repo"},
        )
        rejected_credential = client.post(
            "/v1/git/github/resolve-checkout",
            headers=core_auth,
            json={
                "repository": "example/repo",
                "token": "must-not-be-echoed",
            },
        )
        normalized = client.post(
            "/v1/webhooks/github/normalize",
            content=body,
            headers={
                **core_auth,
                "X-GitHub-Delivery": "delivery-core-1",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
            },
        )
        checkout = client.post(
            "/v1/git/github/resolve-checkout",
            headers=core_auth,
            json={"repository": "example/repo"},
        )
        query = client.post(
            "/v1/query/github",
            headers=core_auth,
            json={
                "action": "pull_request.status.get",
                "repository": "example/repo",
                "pull_request_number": 42,
            },
        )
        comments = client.post(
            "/v1/comments/github/publish",
            headers=core_auth,
            json={
                "repository": "example/repo",
                "pull_request_number": 42,
                "kind": "summary",
                "body": "fail publication",
            },
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    assert rejected_credential.status_code == 422
    assert "must-not-be-echoed" not in rejected_credential.text
    assert normalized.status_code == 200
    assert normalized.json()["delivery_id"] == "delivery-core-1"
    assert normalized.json()["provider_event"]["provider"] == "github"
    assert checkout.status_code == 200
    assert checkout.json() == {
        "remote_url": "https://github.example/example/repo.git",
        "username": "x-access-token",
        "password": "platform-secret",
        "expires_at": None,
    }
    assert query.status_code == 200
    assert query.json()["data"]["state"] == "open"
    assert comments.status_code == 200
    assert comments.json()["failed"] == 1
    assert "platform-secret" not in comments.text
    assert "core-secret" not in json.dumps(
        [normalized.json(), query.json(), comments.json()]
    )


def test_provider_core_is_fail_closed_when_token_is_unconfigured(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/provider-core-disabled.db",
        providers_enabled="github",
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/git/github/resolve-checkout",
            json={"repository": "example/repo"},
        )

    assert response.status_code == 503
