import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.github import GitHubAdapter, GitHubClientError
from review_orchestrator.gitlab import (
    GitLabAdapter,
    GitLabClientError,
    normalize_gitlab_event,
)
from review_orchestrator.main import create_app
from review_orchestrator.models import AgentTask, ReviewRun
from review_orchestrator.providers import (
    ProviderCapabilityError,
    ProviderOperationError,
    ProviderRegistry,
)


class FakeGitHubAdapterClient:
    async def get_pull_request(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> dict:
        return {
            "id": 2002,
            "title": "Improve review",
            "state": "open",
            "html_url": (
                f"https://github.com/{repo_full_name}/pull/{pull_request_number}"
            ),
            "user": {"login": "alice"},
            "base": {
                "ref": "main",
                "sha": "a" * 40,
                "repo": {"full_name": repo_full_name},
            },
            "head": {
                "ref": "feature",
                "sha": "b" * 40,
                "repo": {"full_name": "alice/repo"},
            },
        }

    async def list_pull_request_files(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> list[dict]:
        del repo_full_name, pull_request_number
        return [
            {
                "filename": "src/app.py",
                "patch": "@@ -0,0 +1,2 @@\n+first\n+second",
            }
        ]


class FakeGitLabAdapterClient:
    async def get_merge_request(
        self,
        project_path: str,
        merge_request_iid: int,
    ) -> dict:
        return {
            "id": 3003,
            "iid": merge_request_iid,
            "title": "Improve GitLab review",
            "state": "opened",
            "web_url": (
                f"https://gitlab.com/{project_path}/-/merge_requests/"
                f"{merge_request_iid}"
            ),
            "author": {"username": "bob"},
            "target_branch": "main",
            "source_branch": "feature",
            "sha": "d" * 40,
            "diff_refs": {"base_sha": "c" * 40},
        }

    async def get_merge_request_changes(
        self,
        project_path: str,
        merge_request_iid: int,
    ) -> dict:
        del project_path, merge_request_iid
        return {
            "changes": [
                {
                    "new_path": "src/gitlab.py",
                    "diff": "@@ -0,0 +4,1 @@\n+changed",
                }
            ]
        }


class FailingGitHubAdapterClient(FakeGitHubAdapterClient):
    async def get_pull_request(
        self,
        repo_full_name: str,
        pull_request_number: int,
    ) -> dict:
        del repo_full_name, pull_request_number
        raise GitHubClientError("GitHub lookup failed")


class FailingGitLabAdapterClient(FakeGitLabAdapterClient):
    async def get_merge_request_changes(
        self,
        project_path: str,
        merge_request_iid: int,
    ) -> dict:
        del project_path, merge_request_iid
        raise GitLabClientError("GitLab diff lookup failed")


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        gitlab_webhook_secret="secret",
    )
    return TestClient(create_app(settings))


def gitlab_payload(action: str = "open", *, source_changed: bool = False) -> dict:
    changes = {}
    if source_changed:
        changes = {"last_commit": {"previous": "a" * 40, "current": "b" * 40}}
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "user": {"username": "alice"},
        "project": {"id": 1001, "path_with_namespace": "example/repo"},
        "object_attributes": {
            "id": 2002,
            "iid": 42,
            "action": action,
            "state": "opened",
            "title": "Improve review",
            "target_branch": "main",
            "source_branch": "feature",
            "target_branch_sha": "a" * 40,
            "last_commit": {"id": "b" * 40},
            "changes": changes,
            "url": "https://gitlab.com/example/repo/-/merge_requests/42",
        },
    }


def test_gitlab_adapter_normalizes_merge_request_actions() -> None:
    opened = normalize_gitlab_event("Merge Request Hook", gitlab_payload("open"))
    metadata_updated = normalize_gitlab_event(
        "Merge Request Hook", gitlab_payload("update")
    )
    source_updated = normalize_gitlab_event(
        "Merge Request Hook", gitlab_payload("update", source_changed=True)
    )
    merged = normalize_gitlab_event("Merge Request Hook", gitlab_payload("merge"))

    assert opened.internal_event == "pr_opened"
    assert opened.should_create_review_run is True
    assert metadata_updated.internal_event == "pr_metadata_changed"
    assert metadata_updated.should_create_review_run is False
    assert source_updated.internal_event == "pr_updated"
    assert source_updated.should_create_review_run is True
    assert merged.internal_event == "pr_merged"
    assert merged.should_create_review_run is False


async def test_github_adapter_maps_context_and_changed_files() -> None:
    adapter = GitHubAdapter(FakeGitHubAdapterClient())
    task = AgentTask(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
    )
    review_run = ReviewRun(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        head_sha="b" * 40,
    )

    context = await adapter.get_pull_request_context(task)
    changed_files = await adapter.list_changed_files(review_run)

    assert context.provider_pr_id == "2002"
    assert context.author_login == "alice"
    assert context.base_sha == "a" * 40
    assert context.head_sha == "b" * 40
    assert context.head_repo_full_name == "alice/repo"
    assert context.is_fork is True
    assert [(item.path, item.commentable_lines) for item in changed_files] == [
        ("src/app.py", {1, 2})
    ]


async def test_gitlab_adapter_maps_context_and_changed_files() -> None:
    adapter = GitLabAdapter(FakeGitLabAdapterClient())
    task = AgentTask(
        provider="gitlab",
        repo_full_name="example/repo",
        pull_request_number=42,
    )
    review_run = ReviewRun(
        provider="gitlab",
        repo_full_name="example/repo",
        pull_request_number=42,
        head_sha="d" * 40,
    )

    context = await adapter.get_pull_request_context(task)
    changed_files = await adapter.list_changed_files(review_run)

    assert context.provider_pr_id == "3003"
    assert context.author_login == "bob"
    assert context.base_sha == "c" * 40
    assert context.head_sha == "d" * 40
    assert context.is_fork is False
    assert [(item.path, item.commentable_lines) for item in changed_files] == [
        ("src/gitlab.py", {4})
    ]


async def test_adapters_delegate_comment_publishing(monkeypatch) -> None:
    from review_orchestrator import comments

    github_client = FakeGitHubAdapterClient()
    gitlab_client = FakeGitLabAdapterClient()
    review_run = ReviewRun(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        head_sha="b" * 40,
    )
    calls = []

    async def publish_github_summary(
        session,
        supplied_review_run,
        *,
        github_client,
        status_text,
        finding_stats,
    ):
        calls.append(
            (
                "github_summary",
                session,
                supplied_review_run,
                github_client,
                status_text,
                finding_stats,
            )
        )
        return None

    async def publish_github_lines(
        session,
        supplied_review_run,
        *,
        github_client,
        changed_files,
    ):
        calls.append(
            (
                "github_lines",
                session,
                supplied_review_run,
                github_client,
                changed_files,
            )
        )
        return {"published": 1, "summary_only": 0, "deduped": 0, "failed": 0}

    async def publish_gitlab_summary(
        session,
        supplied_review_run,
        *,
        gitlab_client,
        status_text,
        finding_stats,
    ):
        calls.append(
            (
                "gitlab_summary",
                session,
                supplied_review_run,
                gitlab_client,
                status_text,
                finding_stats,
            )
        )
        return None

    monkeypatch.setattr(
        comments,
        "publish_github_summary_comment",
        publish_github_summary,
    )
    monkeypatch.setattr(comments, "publish_github_line_comments", publish_github_lines)
    monkeypatch.setattr(
        comments,
        "publish_gitlab_summary_comment",
        publish_gitlab_summary,
    )
    session = object()

    await GitHubAdapter(github_client).publish_summary_comment(
        session,
        review_run,
        status_text="completed",
        finding_stats={"high": 1},
    )
    line_stats = await GitHubAdapter(github_client).publish_line_comments(
        session,
        review_run,
        changed_files=[],
    )
    await GitLabAdapter(gitlab_client).publish_summary_comment(
        session,
        review_run,
        status_text="failed",
    )

    assert calls == [
        (
            "github_summary",
            session,
            review_run,
            github_client,
            "completed",
            {"high": 1},
        ),
        ("github_lines", session, review_run, github_client, []),
        ("gitlab_summary", session, review_run, gitlab_client, "failed", None),
    ]
    assert line_stats == {
        "published": 1,
        "summary_only": 0,
        "deduped": 0,
        "failed": 0,
    }


async def test_adapters_translate_client_failures_to_provider_errors() -> None:
    task = AgentTask(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
    )
    with pytest.raises(ProviderOperationError, match="GitHub lookup failed") as info:
        await GitHubAdapter(FailingGitHubAdapterClient()).get_pull_request_context(
            task
        )

    assert info.value.provider == "github"
    assert info.value.operation == "get_pull_request_context"
    assert isinstance(info.value.__cause__, GitHubClientError)

    review_run = ReviewRun(
        provider="gitlab",
        repo_full_name="example/repo",
        pull_request_number=42,
        head_sha="d" * 40,
    )
    with pytest.raises(
        ProviderOperationError, match="GitLab diff lookup failed"
    ) as info:
        await GitLabAdapter(FailingGitLabAdapterClient()).list_changed_files(
            review_run
        )

    assert info.value.provider == "gitlab"
    assert info.value.operation == "list_changed_files"
    assert isinstance(info.value.__cause__, GitLabClientError)


async def test_adapter_missing_client_reports_capability_and_operation() -> None:
    review_run = ReviewRun(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        head_sha="b" * 40,
    )

    with pytest.raises(ProviderCapabilityError) as info:
        await GitHubAdapter().list_changed_files(review_run)

    assert str(info.value) == "GitHub client is not configured."
    assert info.value.provider == "github"
    assert info.value.operation == "list_changed_files"


def test_provider_registry_resolves_registered_adapter() -> None:
    github_adapter = GitHubAdapter(FakeGitHubAdapterClient())
    gitlab_adapter = GitLabAdapter(FakeGitLabAdapterClient())
    registry = ProviderRegistry([github_adapter, gitlab_adapter])

    assert registry.get("github") is github_adapter
    assert registry.require("gitlab") is gitlab_adapter
    assert registry.get("unknown") is None
    with pytest.raises(KeyError, match="unknown"):
        registry.require("unknown")


def test_gitlab_webhook_creates_review_run(tmp_path: Path) -> None:
    body = json.dumps(gitlab_payload()).encode()
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Event-UUID": "gitlab-delivery-1",
        "X-Gitlab-Token": "secret",
        "Content-Type": "application/json",
    }

    with make_client(tmp_path) as client:
        response = client.post("/api/v1/webhooks/gitlab", content=body, headers=headers)
        data = response.json()
        review_run = client.get(f"/api/v1/review-runs/{data['review_run_id']}").json()

    assert response.status_code == 200
    assert data["provider"] == "gitlab"
    assert data["internal_event"] == "pr_opened"
    assert data["status"] == "queued"
    assert review_run["provider"] == "gitlab"
    assert review_run["repo_full_name"] == "example/repo"


def test_gitlab_webhook_rejects_bad_token(tmp_path: Path) -> None:
    body = json.dumps(gitlab_payload()).encode()
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Event-UUID": "gitlab-delivery-1",
        "X-Gitlab-Token": "bad",
        "Content-Type": "application/json",
    }

    with make_client(tmp_path) as client:
        response = client.post("/api/v1/webhooks/gitlab", content=body, headers=headers)

    assert response.status_code == 401
