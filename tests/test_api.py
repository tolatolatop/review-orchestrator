import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app
from review_orchestrator.models import (
    AgentTask,
    Finding,
    ProviderEventInbox,
    PullRequestContext,
    ReviewCommentRef,
    ReviewRun,
    ReviewSession,
    Workspace,
)
from review_orchestrator.openhands import (
    OpenHandsConversation,
    OpenHandsStartTask,
    OpenHandsStartTaskStatus,
)


class FakeOpenHandsClient:
    def __init__(self) -> None:
        self.started_inputs: list[Any] = []
        self.deleted_conversation_ids: list[str] = []
        self.start_task = OpenHandsStartTask(
            id="task-1",
            status=OpenHandsStartTaskStatus.ready,
            app_conversation_id="conversation-1",
            sandbox_id="sandbox-1",
            agent_server_url="http://agent-server",
        )
        self.conversation = OpenHandsConversation(
            id="conversation-1",
            sandbox_status="RUNNING",
            execution_status="RUNNING",
        )

    async def start_conversation(self, review_input: Any) -> OpenHandsStartTask:
        self.started_inputs.append(review_input)
        return self.start_task

    async def get_start_task(self, task_id: str) -> OpenHandsStartTask:
        assert task_id == self.start_task.id
        return self.start_task

    async def get_conversation(self, conversation_id: str) -> OpenHandsConversation:
        assert conversation_id == self.conversation.id
        return self.conversation

    async def delete_conversation(self, conversation_id: str) -> None:
        self.deleted_conversation_ids.append(conversation_id)


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    return TestClient(create_app(settings))


def make_signed_client(tmp_path: Path, secret: str = "secret") -> TestClient:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        github_webhook_secret=secret,
        review_bot_login="review-agent",
    )
    return TestClient(create_app(settings))


def github_headers(
    body: bytes,
    *,
    delivery_id: str = "delivery-1",
    event: str = "pull_request",
    secret: str = "secret",
) -> dict[str, str]:
    signature = "sha256=" + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }


def json_body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


async def create_agent_task_record(
    client: TestClient,
    *,
    status: str = "queued",
    provider: str = "github",
    repo_full_name: str = "example/repo",
    pull_request_number: int = 42,
    task_type: str = "mention",
    created_at: datetime | None = None,
    input_json: dict | None = None,
    result_json: dict | None = None,
    error_message: str | None = None,
) -> AgentTask:
    created_at = created_at or datetime.now(UTC)
    session_factory = client.app.state.session_factory
    async with session_factory() as session:
        event = ProviderEventInbox(
            provider=provider,
            delivery_id=(
                f"delivery-{provider}-{repo_full_name}-"
                f"{pull_request_number}-{status}-{task_type}"
            ),
            provider_event="issue_comment",
            provider_action="created",
            internal_event="agent_mention",
            repo_full_name=repo_full_name,
            pull_request_number=pull_request_number,
            head_sha="b" * 40,
            dedupe_key=(
                f"{provider}:{repo_full_name}:"
                f"{pull_request_number}:{status}:{task_type}"
            ),
            coalesce_key=f"{provider}:{repo_full_name}:{pull_request_number}:mention",
            payload_digest="d" * 64,
            payload={"repository": {"full_name": repo_full_name}},
            status="queued",
            created_at=created_at,
        )
        result = await session.execute(
            select(PullRequestContext).where(
                PullRequestContext.provider == provider,
                PullRequestContext.repo_full_name == repo_full_name,
                PullRequestContext.pull_request_number == pull_request_number,
            )
        )
        context = result.scalar_one_or_none()
        if context is None:
            context = PullRequestContext(
                provider=provider,
                repo_full_name=repo_full_name,
                pull_request_number=pull_request_number,
                head_sha="b" * 40,
                html_url=(
                    f"https://provider.example/"
                    f"{repo_full_name}/pull/{pull_request_number}"
                ),
                created_at=created_at,
            )
            session.add(context)
        session.add(event)
        await session.flush()
        task = AgentTask(
            provider_event_id=event.id,
            pull_request_context_id=context.id,
            provider=provider,
            repo_full_name=repo_full_name,
            pull_request_number=pull_request_number,
            task_type=task_type,
            status=status,
            input_json=input_json,
            result_json=result_json,
            error_message=error_message,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


def pull_request_payload(
    action: str = "opened",
    *,
    merged: bool = False,
    head_sha: str = "b" * 40,
) -> dict:
    return {
        "action": action,
        "repository": {
            "id": 1001,
            "full_name": "example/repo",
            "default_branch": "main",
        },
        "pull_request": {
            "id": 2002,
            "number": 42,
            "title": "Improve review",
            "state": "closed" if action == "closed" else "open",
            "merged": merged,
            "html_url": "https://github.com/example/repo/pull/42",
            "user": {"login": "alice"},
            "base": {
                "ref": "main",
                "sha": "a" * 40,
                "repo": {"full_name": "example/repo"},
            },
            "head": {
                "ref": "feature",
                "sha": head_sha,
                "repo": {"full_name": "fork/repo"},
            },
            "closed_at": "2026-07-09T09:42:00Z" if action == "closed" else None,
            "merged_at": "2026-07-09T09:43:00Z" if merged else None,
        },
    }


def test_health(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_get_review_run(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        create_response = client.post("/api/v1/review-runs", json=payload)
        assert create_response.status_code == 201
        review_run = create_response.json()

        get_response = client.get(f"/api/v1/review-runs/{review_run['id']}")

    assert get_response.status_code == 200
    assert get_response.json()["head_sha"] == payload["head_sha"]
    assert get_response.json()["attempt"] == 1


def test_create_review_run_is_idempotent_without_force(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        first = client.post("/api/v1/review-runs", json=payload).json()
        second = client.post("/api/v1/review-runs", json=payload).json()

    assert second["id"] == first["id"]
    assert second["attempt"] == 1


def test_force_create_review_run_creates_new_attempt(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        first = client.post("/api/v1/review-runs", json=payload).json()
        forced = client.post(
            "/api/v1/review-runs", json={**payload, "force": True}
        ).json()

    assert forced["id"] != first["id"]
    assert forced["attempt"] == 2


def test_list_review_runs_filters_and_reports_worker_state(tmp_path: Path) -> None:
    first_payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    second_payload = {
        **first_payload,
        "head_sha": "c" * 40,
    }

    async def mark_second_running(client: TestClient, review_run_id: str) -> None:
        async with client.app.state.session_factory() as session:
            review_run = await session.get(ReviewRun, review_run_id)
            assert review_run is not None
            review_run.status = "running"
            review_run.stage = "collecting_result"
            review_run.trigger_type = "webhook"
            review_run.lock_owner = "worker-1"
            review_run.locked_until = datetime.now(UTC) + timedelta(minutes=5)
            await session.commit()

    with make_client(tmp_path) as client:
        first = client.post("/api/v1/review-runs", json=first_payload).json()
        second = client.post("/api/v1/review-runs", json=second_payload).json()
        client.portal.call(mark_second_running, client, second["id"])

        locked_response = client.get(
            "/api/v1/review-runs",
            params={
                "provider": "github",
                "repo_full_name": "example/repo",
                "pull_request_number": 42,
                "status": "running",
                "stage": "collecting_result",
                "head_sha": "c" * 40,
                "trigger_type": "webhook",
                "lock_state": "locked",
            },
        )
        unlocked_response = client.get(
            "/api/v1/review-runs",
            params={"lock_state": "unlocked"},
        )

    assert locked_response.status_code == 200
    locked = locked_response.json()
    assert locked["total"] == 1
    assert locked["items"][0]["id"] == second["id"]
    assert locked["items"][0]["operational_state"] == {
        "lock_state": "locked",
        "timeout_state": "none",
        "worker_state": "locked_by_worker",
    }
    assert unlocked_response.status_code == 200
    assert unlocked_response.json()["total"] == 1
    assert unlocked_response.json()["items"][0]["id"] == first["id"]


def test_get_review_run_detail_exposes_operator_context(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    async def seed_detail_context(client: TestClient, review_run_id: str) -> None:
        async with client.app.state.session_factory() as session:
            review_run = await session.get(ReviewRun, review_run_id)
            assert review_run is not None

            event = ProviderEventInbox(
                provider="github",
                delivery_id="delivery-detail",
                provider_event="pull_request",
                provider_action="synchronize",
                internal_event="pr_synchronize",
                repo_full_name="example/repo",
                pull_request_number=42,
                head_sha="b" * 40,
                dedupe_key="github:delivery-detail",
                coalesce_key="github:example/repo:42:bbbb:review",
                payload_digest="digest",
                payload={"installation": {"token": "secret"}},
                status="failed",
                error_code="dispatch_failed",
                error_message="first line\nsecret stack",
                review_run_id=review_run_id,
                processed_at=datetime.now(UTC),
            )
            session.add(event)
            await session.flush()

            context = PullRequestContext(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                provider_repo_id="1001",
                provider_pr_id="2002",
                title="Improve review",
                author_login="alice",
                base_ref="main",
                base_sha="a" * 40,
                head_ref="feature",
                head_sha="b" * 40,
                head_repo_full_name="fork/repo",
                is_fork=True,
                status="open",
                html_url="https://github.com/example/repo/pull/42",
                latest_event_id=event.id,
            )
            session.add(context)
            await session.flush()

            review_run.pull_request_context_id = context.id
            review_run.trigger_event_id = event.id
            review_run.status = "failed"
            review_run.stage = "publishing_summary"
            review_run.workspace_path = "/workspaces/example-repo/pr-42/bbbbbbb"
            review_run.openhands_conversation_id = "conversation-1"
            review_run.failure_code = "invalid_result"
            review_run.error = "invalid result\nraw provider payload"
            review_run.hard_timeout_emitted_at = datetime.now(UTC)
            review_run.validation_warnings_json = [{"code": "unknown_field"}]
            review_run.validation_errors_json = [{"code": "missing_summary"}]
            review_run.summary_comment_id = "summary-42"

            session.add(
                Workspace(
                    workspace_id="github-example-repo-42-bbbbbbb",
                    provider="github",
                    repository="example/repo",
                    repository_clone_url="https://github.com/example/repo.git",
                    repo_hash="repohash",
                    pull_request_number=42,
                    base_sha="a" * 40,
                    head_sha="b" * 40,
                    workspace_path="/workspaces/example-repo/pr-42/bbbbbbb",
                    status="failed",
                    failure_code="checkout_failed",
                    failure_message="missing head\nfull stderr",
                )
            )
            session.add(
                ReviewSession(
                    review_run_id=review_run_id,
                    openhands_conversation_id="conversation-1",
                    status="failed",
                    skill_name="code-review",
                    profile_name="default",
                    result_ref="s3://results/1",
                    error_message="agent stopped\ntrace",
                )
            )
            session.add(
                AgentTask(
                    provider_event_id=event.id,
                    pull_request_context_id=context.id,
                    provider="github",
                    repo_full_name="example/repo",
                    pull_request_number=42,
                    task_type="mention",
                    status="failed",
                    error_message="task failed\ntrace",
                )
            )
            session.add(
                Finding(
                    review_run_id=review_run_id,
                    pull_request_context_id=context.id,
                    fingerprint="fp-1",
                    file_path="src/app.py",
                    severity="high",
                    message="Auth check is skipped.",
                    status="active",
                    state="new",
                )
            )
            session.add(
                ReviewCommentRef(
                    provider="github",
                    repo_full_name="example/repo",
                    pull_request_number=42,
                    review_run_id=review_run_id,
                    comment_type="summary",
                    provider_comment_id="summary-42",
                    status="active",
                )
            )
            session.add(
                ReviewCommentRef(
                    provider="github",
                    repo_full_name="example/repo",
                    pull_request_number=42,
                    review_run_id=review_run_id,
                    finding_id=None,
                    comment_type="line",
                    provider_comment_id="line-1",
                    status="active",
                )
            )
            await session.commit()

    with make_client(tmp_path) as client:
        review_run = client.post("/api/v1/review-runs", json=payload).json()
        client.portal.call(seed_detail_context, client, review_run["id"])
        response = client.get(f"/api/v1/review-runs/{review_run['id']}")

    assert response.status_code == 200
    data = response.json()
    assert data["failure_code"] == "invalid_result"
    assert data["error"] == "invalid result"
    assert data["operational_state"]["timeout_state"] == "hard_timeout"
    assert data["provider_publishing"]["summary_published"] is True
    assert data["provider_publishing"]["summary_comment_id"] == "summary-42"
    assert data["provider_publishing"]["line_comment_count"] == 1
    assert data["pull_request_context"]["html_url"].endswith("/pull/42")
    assert data["workspace"]["status"] == "failed"
    assert data["workspace"]["failure_message"] == "missing head"
    assert data["review_session"]["error_message"] == "agent stopped"
    assert data["findings_summary"]["by_severity"] == {"high": 1}
    assert data["validation_errors"] == [{"code": "missing_summary"}]
    assert data["trigger_event"]["error_message"] == "first line"
    assert data["agent_task"]["status"] == "failed"


def test_retry_rejects_non_failed_run(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        review_run = client.post("/api/v1/review-runs", json=payload).json()
        response = client.post(f"/api/v1/review-runs/{review_run['id']}/retry")

    assert response.status_code == 409


def test_cancel_review_run(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        review_run = client.post("/api/v1/review-runs", json=payload).json()
        response = client.post(f"/api/v1/review-runs/{review_run['id']}/cancel")

    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"


def test_accept_github_pull_request_webhook_creates_review_run(
    tmp_path: Path,
) -> None:
    payload = pull_request_payload(action="opened")
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(body),
        )
        data = response.json()
        review_run_response = client.get(f"/api/v1/review-runs/{data['review_run_id']}")

    assert response.status_code == 200
    assert data["internal_event"] == "pr_opened"
    assert data["status"] == "queued"
    assert data["duplicate"] is False
    assert review_run_response.status_code == 200
    assert (
        review_run_response.json()["head_sha"]
        == payload["pull_request"]["head"]["sha"]
    )


def test_duplicate_github_delivery_is_idempotent(tmp_path: Path) -> None:
    payload = pull_request_payload(action="synchronize", head_sha="c" * 40)
    body = json_body(payload)
    headers = github_headers(body, delivery_id="delivery-duplicate")

    with make_signed_client(tmp_path) as client:
        first_response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=headers,
        )
        second_response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=headers,
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["duplicate"] is True
    assert (
        second_response.json()["review_run_id"]
        == first_response.json()["review_run_id"]
    )


def test_synchronize_supersedes_older_queued_review_run(tmp_path: Path) -> None:
    opened_payload = pull_request_payload(action="opened", head_sha="b" * 40)
    opened_body = json_body(opened_payload)
    sync_payload = pull_request_payload(action="synchronize", head_sha="c" * 40)
    sync_body = json_body(sync_payload)

    with make_signed_client(tmp_path) as client:
        opened_response = client.post(
            "/api/v1/webhooks/github",
            content=opened_body,
            headers=github_headers(opened_body, delivery_id="delivery-opened"),
        )
        opened_run_id = opened_response.json()["review_run_id"]

        sync_response = client.post(
            "/api/v1/webhooks/github",
            content=sync_body,
            headers=github_headers(sync_body, delivery_id="delivery-sync"),
        )
        old_run = client.get(f"/api/v1/review-runs/{opened_run_id}").json()

    assert sync_response.status_code == 200
    assert sync_response.json()["review_run_id"] != opened_run_id
    assert old_run["status"] == "superseded"
    assert old_run["failure_code"] == "superseded_by_new_head"


def test_rejects_invalid_github_signature(tmp_path: Path) -> None:
    payload = pull_request_payload()
    body = json_body(payload)
    headers = github_headers(body)
    headers["X-Hub-Signature-256"] = "sha256=invalid"

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=headers,
        )

    assert response.status_code == 401


def test_closed_pull_request_is_processed_without_review_run(tmp_path: Path) -> None:
    payload = pull_request_payload(action="closed", merged=True)
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(body),
        )

    assert response.status_code == 200
    assert response.json()["internal_event"] == "pr_merged"
    assert response.json()["status"] == "processed"
    assert response.json()["review_run_id"] is None


def test_closed_pull_request_cancels_existing_queued_review_run(tmp_path: Path) -> None:
    opened_payload = pull_request_payload(action="opened", head_sha="b" * 40)
    opened_body = json_body(opened_payload)
    closed_payload = pull_request_payload(
        action="closed", merged=True, head_sha="b" * 40
    )
    closed_body = json_body(closed_payload)

    with make_signed_client(tmp_path) as client:
        opened_response = client.post(
            "/api/v1/webhooks/github",
            content=opened_body,
            headers=github_headers(opened_body, delivery_id="delivery-open"),
        )
        review_run_id = opened_response.json()["review_run_id"]

        closed_response = client.post(
            "/api/v1/webhooks/github",
            content=closed_body,
            headers=github_headers(closed_body, delivery_id="delivery-closed"),
        )
        review_run = client.get(f"/api/v1/review-runs/{review_run_id}").json()

    assert closed_response.status_code == 200
    assert closed_response.json()["internal_event"] == "pr_merged"
    assert review_run["status"] == "cancelled"
    assert review_run["failure_code"] == "pr_merged"


def test_pr_issue_comment_is_context_only(tmp_path: Path) -> None:
    payload = {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {"number": 42, "pull_request": {"url": "https://api.github/pr"}},
        "comment": {"id": 123, "body": "please explain"},
    }
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(body, event="issue_comment"),
        )

    assert response.status_code == 200
    assert response.json()["internal_event"] == "pr_comment_context"
    assert response.json()["status"] == "processed"
    assert response.json()["review_run_id"] is None
    assert response.json()["agent_task_id"] is None


def test_pr_issue_comment_mention_creates_agent_task(tmp_path: Path) -> None:
    payload = {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {"number": 42, "pull_request": {"url": "https://api.github/pr"}},
        "comment": {"id": 123, "body": "@review-agent should I re-review this?"},
    }
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(body, event="issue_comment"),
        )

    assert response.status_code == 200
    assert response.json()["internal_event"] == "agent_mention"
    assert response.json()["status"] == "queued"
    assert response.json()["review_run_id"] is None
    assert response.json()["agent_task_id"] is not None


def test_list_provider_events_filters_and_returns_safe_summary(
    tmp_path: Path,
) -> None:
    opened_payload = pull_request_payload(action="opened")
    opened_body = json_body(opened_payload)
    comment_payload = {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {"number": 42, "pull_request": {"url": "https://api.github/pr"}},
        "comment": {"id": 123, "body": "@review-agent should I re-review this?"},
    }
    comment_body = json_body(comment_payload)

    with make_signed_client(tmp_path) as client:
        client.post(
            "/api/v1/webhooks/github",
            content=opened_body,
            headers=github_headers(opened_body, delivery_id="delivery-opened"),
        )
        mention_response = client.post(
            "/api/v1/webhooks/github",
            content=comment_body,
            headers=github_headers(
                comment_body,
                delivery_id="delivery-comment",
                event="issue_comment",
            ),
        )

        response = client.get(
            "/api/v1/provider-events",
            params={
                "provider": "github",
                "repo_full_name": "example/repo",
                "pull_request_number": 42,
                "internal_event": "agent_mention",
                "status": "queued",
                "delivery_id": "delivery-comment",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["delivery_id"] == "delivery-comment"
    assert data["items"][0]["payload_digest"]
    assert data["items"][0]["coalesce_key"].startswith("github:example/repo:42")
    assert data["items"][0]["agent_task_id"] == mention_response.json()["agent_task_id"]
    assert "payload" not in data["items"][0]


def test_get_provider_event_detail_omits_payload_by_default(
    tmp_path: Path,
) -> None:
    payload = pull_request_payload(action="opened")
    payload["installation"] = {"id": 1, "token": "sensitive-token"}
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(body, delivery_id="delivery-detail"),
        )
        listed = client.get(
            "/api/v1/provider-events",
            params={"delivery_id": "delivery-detail"},
        ).json()
        event_id = listed["items"][0]["id"]

        default_detail = client.get(f"/api/v1/provider-events/{event_id}")
        payload_detail = client.get(
            f"/api/v1/provider-events/{event_id}",
            params={"include_payload": True},
        )

    assert default_detail.status_code == 200
    assert default_detail.json()["payload"] is None
    assert payload_detail.status_code == 200
    assert payload_detail.json()["payload"]["installation"] == "[redacted]"


async def test_list_agent_tasks_filters_and_queue_health(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with make_client(tmp_path) as client:
        queued_task = await create_agent_task_record(
            client,
            status="queued",
            created_at=now - timedelta(minutes=10),
        )
        await create_agent_task_record(
            client,
            status="running",
            created_at=now - timedelta(minutes=5),
        )
        await create_agent_task_record(
            client,
            status="completed",
            provider="gitlab",
            repo_full_name="group/repo",
            created_at=now - timedelta(minutes=2),
        )
        await create_agent_task_record(
            client,
            status="failed",
            task_type="scheduled",
            created_at=now - timedelta(minutes=1),
        )

        response = client.get(
            "/api/v1/agent-tasks",
            params={
                "status": "queued",
                "provider": "github",
                "repo_full_name": "example/repo",
                "pull_request_number": 42,
                "task_type": "mention",
                "created_from": (now - timedelta(hours=1)).isoformat(),
                "created_to": (now + timedelta(minutes=1)).isoformat(),
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == queued_task.id
    assert data["items"][0]["status"] == "queued"
    assert data["items"][0]["provider_event_link"].startswith(
        "/api/v1/provider-events/"
    )
    assert data["items"][0]["pull_request_context_link"].startswith(
        "/api/v1/pull-request-contexts/"
    )
    assert data["queue"]["queued"] == 1
    assert data["queue"]["running"] == 1
    assert data["queue"]["completed"] == 0
    assert data["queue"]["failed"] == 0
    assert data["queue"]["oldest_queued_age_seconds"] >= 590


async def test_get_agent_task_detail_returns_redacted_metadata(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = await create_agent_task_record(
            client,
            status="failed",
            input_json={
                "provider_action": "created",
                "token": "secret-token",
                "payload": {
                    "installation": {"id": 123},
                    "comment": {"body": "@review-agent retry"},
                },
            },
            result_json={"published": False, "reason": "provider_error"},
            error_message="first line\nsecond line with internal details",
        )

        response = client.get(f"/api/v1/agent-tasks/{task.id}")
        missing_response = client.get("/api/v1/agent-tasks/missing")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == task.id
    assert data["status"] == "failed"
    assert data["provider_event_id"] == task.provider_event_id
    assert data["provider_event_link"] == (
        f"/api/v1/provider-events/{task.provider_event_id}"
    )
    assert data["pull_request_context_link"] == (
        f"/api/v1/pull-request-contexts/{task.pull_request_context_id}"
    )
    assert data["input_metadata"]["token"] == "[redacted]"
    assert data["input_metadata"]["payload"]["installation"] == "[redacted]"
    assert data["input_metadata"]["payload"]["comment"]["body"] == "@review-agent retry"
    assert data["result_json"] == {"published": False, "reason": "provider_error"}
    assert data["error_message"] == "first line"
    assert missing_response.status_code == 404


def test_start_review_session_records_openhands_identifiers(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_openhands = FakeOpenHandsClient()

    with make_client(tmp_path) as client:
        client.app.state.openhands_client = fake_openhands
        create_response = client.post("/api/v1/review-runs", json=payload)
        review_run = create_response.json()

        start_response = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example-repo/pr-42/bbbbbbb"},
        )

    assert start_response.status_code == 200
    data = start_response.json()
    assert data["status"] == "running"
    assert data["workspace_path"] == "/workspaces/example-repo/pr-42/bbbbbbb"
    assert data["openhands_start_task_id"] == "task-1"
    assert data["openhands_conversation_id"] == "conversation-1"
    assert data["openhands_sandbox_id"] == "sandbox-1"
    assert fake_openhands.started_inputs[0].repo_full_name == "example/repo"
    assert fake_openhands.started_inputs[0].base_sha == "a" * 40


def test_start_review_session_requires_workspace_path(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        client.app.state.openhands_client = FakeOpenHandsClient()
        create_response = client.post("/api/v1/review-runs", json=payload)
        review_run = create_response.json()
        start_response = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={},
        )

    assert start_response.status_code == 409
    assert "workspace_path" in start_response.json()["detail"]


def test_sync_review_session_marks_openhands_failure(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_openhands = FakeOpenHandsClient()
    fake_openhands.conversation = OpenHandsConversation(
        id="conversation-1",
        sandbox_status="RUNNING",
        execution_status="ERROR",
    )

    with make_client(tmp_path) as client:
        client.app.state.openhands_client = fake_openhands
        review_run = client.post("/api/v1/review-runs", json=payload).json()
        started = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example-repo/pr-42/bbbbbbb"},
        ).json()
        sync_response = client.post(
            f"/api/v1/review-runs/{started['id']}/session/sync",
        )

    assert sync_response.status_code == 200
    assert sync_response.json()["status"] == "failed"
    assert "ERROR" in sync_response.json()["error"]


def test_collect_review_result_completes_review_run(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    raw_output = {
        "summary": "One issue found.",
        "findings": [
            {
                "file": "src/app.py",
                "line": 12,
                "severity": "high",
                "message": "Auth check is skipped.",
                "confidence": 0.9,
            }
        ],
    }

    with make_client(tmp_path) as client:
        review_run = client.post("/api/v1/review-runs", json=payload).json()
        collect_response = client.post(
            f"/api/v1/review-runs/{review_run['id']}/result",
            json={
                "raw_output": raw_output,
                "changed_files": [
                    {"path": "src/app.py", "commentable_lines": [12, 13]}
                ],
            },
        )

    assert collect_response.status_code == 200
    data = collect_response.json()
    assert data["review_run"]["status"] == "completed"
    assert data["review_run"]["review_summary"] == "One issue found."
    assert data["parsed"]["findings"][0]["publish_as_line_comment"] is True


def test_cancel_review_session_deletes_openhands_conversation(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_openhands = FakeOpenHandsClient()

    with make_client(tmp_path) as client:
        client.app.state.openhands_client = fake_openhands
        review_run = client.post("/api/v1/review-runs", json=payload).json()
        started = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example-repo/pr-42/bbbbbbb"},
        ).json()
        cancel_response = client.post(
            f"/api/v1/review-runs/{started['id']}/session/cancel",
            json={"reason": "superseded by new head"},
        )

    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"
    assert fake_openhands.deleted_conversation_ids == ["conversation-1"]
