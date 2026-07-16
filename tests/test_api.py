import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from review_orchestrator.application.delivery import enqueue_delivery
from review_orchestrator.application.scheduler import claim_next_task
from review_orchestrator.config import Settings
from review_orchestrator.github import GitHubAdapter
from review_orchestrator.main import create_app
from review_orchestrator.models import (
    AgentTask,
    DeliveryOutbox,
    Finding,
    ProviderEventInbox,
    PullRequestContext,
    ReviewCommentRef,
    ReviewCommentSlot,
    ReviewRun,
    ReviewSession,
    SessionArchive,
    Task,
    TaskAttempt,
    Workspace,
)
from review_orchestrator.pi_agent import (
    PiAgentSession,
)
from review_orchestrator.providers import ProviderRegistry
from tests.factories import ReviewRunCreate, create_review_run


class FakePiAgentClient:
    def __init__(self) -> None:
        self.started_inputs: list[Any] = []
        self.started_options: list[dict[str, Any]] = []
        self.cancelled_session_ids: list[str] = []
        self.session = PiAgentSession(
            id="session-1",
            status="running",
            stage="analyzing",
            provider="openai",
            model="gpt-5.4",
            thinking_level="high",
        )

    async def start_session(self, review_input: Any, **kwargs: Any) -> PiAgentSession:
        self.started_inputs.append(review_input)
        self.started_options.append(kwargs)
        return self.session

    async def get_session(self, session_id: str) -> PiAgentSession:
        assert session_id == self.session.id
        return self.session

    async def cancel_session(self, session_id: str) -> PiAgentSession:
        self.cancelled_session_ids.append(session_id)
        self.session = self.session.model_copy(
            update={"status": "cancelled", "stage": "cancelled"}
        )
        return self.session

def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    return TestClient(create_app(settings))


def make_client_with_settings(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        **overrides,
    )
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


async def _seed_review_run_for_api(client: TestClient, payload: dict) -> str:
    async with client.app.state.session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(**payload),
        )
        return review_run.id


def seed_review_run(client: TestClient, payload: dict) -> dict:
    review_run_id = client.portal.call(_seed_review_run_for_api, client, payload)
    response = client.get(f"/api/v1/review-runs/{review_run_id}")
    assert response.status_code == 200
    return response.json()


async def make_review_placeholder_ready(
    client: TestClient,
    review_run_id: str,
) -> None:
    async with client.app.state.session_factory() as session:
        review_run = await session.get(ReviewRun, review_run_id)
        assert review_run is not None
        slot = (
            await session.execute(
                select(ReviewCommentSlot).where(
                    ReviewCommentSlot.review_run_id == review_run_id
                )
            )
        ).scalar_one_or_none()
        if slot is None:
            slot = ReviewCommentSlot(
                review_run_id=review_run.id,
                provider=review_run.provider,
                repo_full_name=review_run.repo_full_name,
                pull_request_number=review_run.pull_request_number,
                head_sha=review_run.head_sha,
                marker=f"review-orchestrator:summary:slot:test-{review_run.id}",
            )
        slot.status = "ready"
        slot.provider_comment_id = f"placeholder-{review_run.id}"
        review_run.status = "queued"
        review_run.stage = "placeholder_ready"
        session.add_all([slot, review_run])
        await session.commit()


async def create_running_review_action_task(
    client: TestClient,
    review_run_id: str,
    agent_session_id: str | None = "agent-tool-session",
) -> AgentTask:
    async with client.app.state.session_factory() as session:
        review_run = await session.get(ReviewRun, review_run_id)
        assert review_run is not None
        task = AgentTask(
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
            pull_request_number=review_run.pull_request_number,
            pull_request_context_id=review_run.pull_request_context_id,
            task_type="message_command",
            source_kind="issue_comment",
            source_comment_id=f"tool-comment-{review_run.id}",
            source_author_login="alice",
            command_text="retry the review",
            head_sha=review_run.head_sha,
            status="running",
            stage="waiting_for_agent",
            execution_status="running",
            agent_session_id=agent_session_id,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def make_review_run_rerunnable(
    client: TestClient,
    review_run_id: str,
    context_head_sha: str | None = None,
) -> None:
    async with client.app.state.session_factory() as session:
        review_run = await session.get(ReviewRun, review_run_id)
        assert review_run is not None
        context = PullRequestContext(
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
            pull_request_number=review_run.pull_request_number,
            base_sha=review_run.base_sha,
            head_sha=context_head_sha or review_run.head_sha,
            status="open",
        )
        session.add(context)
        await session.flush()
        review_run.pull_request_context_id = context.id
        review_run.status = "completed"
        review_run.stage = "completed"
        await session.commit()


async def make_review_run_retryable(
    client: TestClient,
    review_run_id: str,
    context_head_sha: str | None = None,
    context_status: str = "open",
) -> None:
    async with client.app.state.session_factory() as session:
        review_run = await session.get(ReviewRun, review_run_id)
        assert review_run is not None
        context = PullRequestContext(
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
            pull_request_number=review_run.pull_request_number,
            base_sha=review_run.base_sha,
            head_sha=context_head_sha or review_run.head_sha,
            status=context_status,
        )
        session.add(context)
        await session.flush()
        review_run.pull_request_context_id = context.id
        review_run.status = "failed"
        review_run.stage = "failed"
        review_run.failure_code = "agent_failed"
        await session.commit()


def test_bundled_dashboard_is_mounted(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        redirect = client.get("/dashboard", follow_redirects=False)
        response = client.get("/dashboard/")

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/dashboard/"
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Review operations" in response.text
    assert "/api/v1/observability" in response.text
    assert "headers['X-Review-Token']=PROXY_TOKEN" in response.text
    assert "Review requested · rerun" in response.text
    assert "<option>rejected</option>" in response.text


def test_review_ledger_dashboard_is_mounted(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        redirect = client.get("/reviews", follow_redirects=False)
        response = client.get("/reviews/")

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/reviews/"
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Review 执行台账" in response.text
    assert "/api/v1/observability/review-runs" in response.text
    assert "REFRESH_SECONDS=30" in response.text
    assert "headers['X-Review-Token']=PROXY_TOKEN" in response.text
    assert "重新审查" in response.text
    assert 'data-review-action="retry"' in response.text
    assert "重试失败审查" in response.text
    assert "idempotency_key" in response.text


def test_observability_list_aliases_are_available(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for resource in ("provider-events", "review-runs", "agent-tasks"):
            response = client.get(f"/api/v1/observability/{resource}")
            assert response.status_code == 200
            assert response.json()["items"] == []


def test_unified_task_scheduling_api_exposes_and_updates_review_task(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        review_run = seed_review_run(
            client,
            {
                "provider": "github",
                "repo_full_name": "owner/repo",
                "pull_request_number": 42,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
        )
        task_id = review_run["id"]

        listed = client.get(
            "/api/v1/tasks",
            params={"kind": "review", "queue": "manual-review"},
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        task = listed.json()["items"][0]
        assert task["id"] == task_id
        assert task["capability_id"] == "code-review"
        assert task["priority"] == 60
        assert task["domain_metadata"]["repo_full_name"] == "owner/repo"

        updated = client.patch(
            f"/api/v1/tasks/{task_id}/scheduling",
            json={
                "queue": "interactive",
                "priority": 90,
                "resource_class": "agent-heavy",
                "resource_context": {
                    "repository": {
                        "keys": ["github/owner/repo"],
                        "units": 1,
                    },
                    "model": "gpt-5.4",
                },
            },
        )
        assert updated.status_code == 200
        assert updated.json()["queue"] == "interactive"
        assert updated.json()["priority"] == 90
        assert updated.json()["effective_priority"] == 90
        assert updated.json()["resource_class"] == "agent-heavy"
        assert updated.json()["resource_context"]["model"] == "gpt-5.4"
        assert updated.json()["resource_context"]["repository"] == {
            "keys": ["github/owner/repo"],
            "units": 1,
        }


async def _claim_task_for_api_test(client: TestClient) -> str:
    async with client.app.state.session_factory() as session:
        task = await claim_next_task(
            session,
            worker_id="api-test-worker",
            task_kinds={"review"},
        )
        assert task is not None
        return task.id


def test_resource_pool_api_updates_counted_lock_capacity(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        seed_review_run(
            client,
            {
                "provider": "github",
                "repo_full_name": "owner/repo",
                "pull_request_number": 42,
                "head_sha": "b" * 40,
            },
        )
        client.portal.call(_claim_task_for_api_test, client)

        pools = client.get("/api/v1/resource-pools")
        assert pools.status_code == 200
        concurrency_pool = next(
            item
            for item in pools.json()["items"]
            if item["dimension"] == "concurrency"
        )
        assert concurrency_pool["capacity"] == 1
        assert concurrency_pool["active_units"] == 1

        updated = client.put(
            f"/api/v1/resource-pools/{concurrency_pool['resource_key']}",
            json={"capacity": 3},
        )
        assert updated.status_code == 200
        assert updated.json()["capacity"] == 3
        assert updated.json()["active_units"] == 1

        created_pool = client.put(
            "/api/v1/resource-pools/custom:gpu-a",
            json={"capacity": 2, "dimension": "custom"},
        )
        assert created_pool.status_code == 200
        assert created_pool.json()["resource_key"] == "custom:gpu-a"
        assert created_pool.json()["dimension"] == "custom"
        assert created_pool.json()["capacity"] == 2
        dimension_conflict = client.put(
            "/api/v1/resource-pools/custom:gpu-a",
            json={"capacity": 3, "dimension": "model"},
        )
        assert dimension_conflict.status_code == 409


async def _enqueue_delivery_for_api_test(client: TestClient, task_id: str) -> str:
    async with client.app.state.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        delivery = await enqueue_delivery(
            session,
            task,
            provider="github",
            operation="review_summary",
            destination_key=f"task:{task.id}:summary",
            idempotency_key=f"task:{task.id}:summary:completed",
            payload={"status_text": "completed"},
        )
        await session.commit()
        return delivery.id


def test_delivery_scheduling_api_lists_and_reschedules_outbox(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        review_run = seed_review_run(
            client,
            {
                "provider": "github",
                "repo_full_name": "owner/repo",
                "pull_request_number": 42,
                "head_sha": "b" * 40,
            },
        )
        task_id = review_run["id"]
        delivery_id = client.portal.call(
            _enqueue_delivery_for_api_test,
            client,
            task_id,
        )

        listed = client.get(
            "/api/v1/deliveries",
            params={"task_id": task_id, "status": "queued"},
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["id"] == delivery_id

        updated = client.patch(
            f"/api/v1/deliveries/{delivery_id}/scheduling",
            json={"queue": "provider-urgent", "priority": 95, "max_attempts": 8},
        )
        assert updated.status_code == 200
        assert updated.json()["queue"] == "provider-urgent"
        assert updated.json()["priority"] == 95
        assert updated.json()["max_attempts"] == 8


async def _archive_session_for_api_test(client: TestClient, task_id: str) -> str:
    async with client.app.state.session_factory() as session:
        attempt = TaskAttempt(
            task_id=task_id,
            attempt_no=1,
            status="completed",
            agent_run_id="agent-run-api",
        )
        session.add(attempt)
        await session.flush()
        archive = SessionArchive(
            task_id=task_id,
            task_attempt_id=attempt.id,
            agent_run_id="agent-run-api",
            session_json={"entries": [{"role": "assistant", "content": "done"}]},
            task_metadata_json={"task": {"id": task_id}},
        )
        session.add(archive)
        await session.commit()
        return archive.id


def test_session_archive_api_lists_permanent_task_sessions(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        review_run = seed_review_run(
            client,
            {
                "provider": "github",
                "repo_full_name": "owner/repo",
                "pull_request_number": 42,
                "head_sha": "b" * 40,
            },
        )
        task_id = review_run["id"]
        archive_id = client.portal.call(
            _archive_session_for_api_test,
            client,
            task_id,
        )

        listed = client.get(f"/api/v1/tasks/{task_id}/sessions")
        fetched = client.get(f"/api/v1/session-archives/{archive_id}")
        alias = client.get(
            f"/api/v1/observability/session-archives/{archive_id}"
        )

        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [archive_id]
        assert fetched.status_code == 200
        assert fetched.json()["task_attempt"]["attempt_no"] == 1
        assert fetched.json()["session"]["entries"][0]["content"] == "done"
        assert alias.json() == fetched.json()
        assert client.get("/api/v1/tasks/missing/sessions").status_code == 404


def test_observability_detail_aliases_match_legacy_routes(tmp_path: Path) -> None:
    payload = pull_request_payload(action="opened")
    body = json_body(payload)
    with make_signed_client(tmp_path) as client:
        accepted = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(body, delivery_id="alias-detail"),
        ).json()
        resources = {
            "provider-events": accepted["delivery_id"],
            "review-runs": accepted["review_run_id"],
        }
        event = client.get(
            "/api/v1/observability/provider-events",
            params={"delivery_id": resources["provider-events"]},
        ).json()["items"][0]
        resources["provider-events"] = event["id"]
        for resource, item_id in resources.items():
            alias = client.get(f"/api/v1/observability/{resource}/{item_id}")
            legacy = client.get(f"/api/v1/{resource}/{item_id}")
            assert alias.status_code == 200
            assert alias.json() == legacy.json()


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
    stage: str | None = None,
    agent_session_id: str | None = None,
    response_comment_id: str | None = None,
    command_text: str | None = None,
    source_author_login: str | None = None,
    failure_code: str | None = None,
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
            stage=stage,
            agent_session_id=agent_session_id,
            response_comment_id=response_comment_id,
            command_text=command_text,
            source_author_login=source_author_login,
            failure_code=failure_code,
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


class FakeTaskCommentGitHubClient:
    def __init__(self) -> None:
        self.updated: list[tuple[str, str]] = []

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        del repo_full_name
        self.updated.append((str(comment_id), body))
        return str(comment_id)

    async def list_issue_comments(self, repo_full_name, pull_request_number):
        del repo_full_name, pull_request_number
        return []

    async def create_issue_comment(self, repo_full_name, pull_request_number, body):
        del repo_full_name, pull_request_number, body
        return "created-task-comment"


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


def test_get_seeded_review_run(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        review_run = seed_review_run(client, payload)
        get_response = client.get(f"/api/v1/review-runs/{review_run['id']}")

    assert get_response.status_code == 200
    assert get_response.json()["head_sha"] == payload["head_sha"]
    assert get_response.json()["attempt"] == 1


def test_direct_review_run_creation_is_not_exposed(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        response = client.post("/api/v1/review-runs", json=payload)
        openapi = client.get("/openapi.json").json()

    assert response.status_code == 405
    assert "post" not in openapi["paths"]["/api/v1/review-runs"]


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
        first = seed_review_run(client, first_payload)
        second = seed_review_run(client, second_payload)
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


def test_list_review_runs_includes_pull_request_context_in_bulk(
    tmp_path: Path,
) -> None:
    github_payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    gitlab_payload = {
        "provider": "gitlab",
        "repo_full_name": "group/project",
        "pull_request_number": 7,
        "base_sha": "c" * 40,
        "head_sha": "d" * 40,
    }

    async def seed_contexts(
        client: TestClient,
        github_run_id: str,
    ) -> None:
        async with client.app.state.session_factory() as session:
            github_context = PullRequestContext(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                title="Add review ledger",
                author_login="alice",
                base_ref="main",
                base_sha="a" * 40,
                head_ref="feature/ledger",
                head_sha="b" * 40,
                status="open",
                html_url="https://github.com/example/repo/pull/42",
            )
            gitlab_context = PullRequestContext(
                provider="gitlab",
                repo_full_name="group/project",
                pull_request_number=7,
                title="Improve review state",
                author_login="bob",
                base_ref="main",
                base_sha="c" * 40,
                head_ref="review-state",
                head_sha="d" * 40,
                status="opened",
                html_url="https://gitlab.com/group/project/-/merge_requests/7",
            )
            session.add_all([github_context, gitlab_context])
            await session.flush()
            github_run = await session.get(ReviewRun, github_run_id)
            assert github_run is not None
            github_run.pull_request_context_id = github_context.id
            await session.commit()

    with make_client(tmp_path) as client:
        github_run = seed_review_run(client, github_payload)
        gitlab_run = seed_review_run(client, gitlab_payload)
        client.portal.call(seed_contexts, client, github_run["id"])

        github_response = client.get(
            "/api/v1/observability/review-runs",
            params={"provider": "github"},
        )
        gitlab_response = client.get(
            "/api/v1/observability/review-runs",
            params={"provider": "gitlab"},
        )
        page_response = client.get(
            "/api/v1/observability/review-runs",
            params={"limit": 1, "offset": 0},
        )

    assert github_response.status_code == 200
    github_context = github_response.json()["items"][0]["pull_request_context"]
    assert github_context["title"] == "Add review ledger"
    assert github_context["html_url"].endswith("/pull/42")

    assert gitlab_response.status_code == 200
    gitlab_item = gitlab_response.json()["items"][0]
    assert gitlab_item["id"] == gitlab_run["id"]
    assert gitlab_item["pull_request_context"]["title"] == "Improve review state"
    assert gitlab_item["pull_request_context"]["html_url"].endswith(
        "/merge_requests/7"
    )

    assert page_response.status_code == 200
    assert page_response.json()["total"] == 2
    assert len(page_response.json()["items"]) == 1


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
            review_run.agent_session_id = "session-1"
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
                    agent_session_id="session-1",
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
        review_run = seed_review_run(client, payload)
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
        review_run = seed_review_run(client, payload)
        response = client.post(f"/api/v1/review-runs/{review_run['id']}/retry")
        event = client.get(
            "/api/v1/observability/provider-events/"
            f"{response.json()['detail']['review_request_event_id']}"
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_not_failed"
    assert event.status_code == 200
    assert event.json()["status"] == "rejected"
    assert event.json()["provider"] == "internal"
    assert event.json()["provider_action"] == "retry"
    assert event.json()["source_review_run_id"] == review_run["id"]
    assert event.json()["review_run_id"] is None


def test_cancel_review_run(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        review_run = seed_review_run(client, payload)
        response = client.post(f"/api/v1/review-runs/{review_run['id']}/cancel")

    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_delivery"
    assert response.json()["execution_status"] == "cancelled"
    assert response.json()["stage"] == "cancelled_delivery_pending"


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
    review_run = review_run_response.json()
    assert (
        review_run["head_sha"] == payload["pull_request"]["head"]["sha"]
    )
    assert review_run["trigger_type"] == "webhook"
    assert review_run["status"] == "awaiting_delivery"
    assert review_run["stage"] == "placeholder_delivery_pending"
    assert review_run["agent_session_id"] is None
    assert review_run["placeholder"]["status"] == "pending"
    assert review_run["placeholder"]["delivery_status"] == "queued"
    assert review_run["trigger_event"] is not None
    assert review_run["trigger_event"]["provider_event"] == "pull_request"
    assert review_run["trigger_event"]["internal_event"] == "pr_opened"


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
    assert old_run["status"] == "awaiting_delivery"
    assert old_run["execution_status"] == "cancelled"
    assert old_run["stage"] == "superseded_delivery_pending"
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
    assert review_run["status"] == "awaiting_delivery"
    assert review_run["execution_status"] == "cancelled"
    assert review_run["stage"] == "cancelled_delivery_pending"
    assert review_run["failure_code"] == "pr_merged"


def test_closed_pull_request_requests_active_message_task_cancellation(
    tmp_path: Path,
) -> None:
    opened_payload = pull_request_payload(action="opened", head_sha="b" * 40)
    opened_body = json_body(opened_payload)
    command_payload = {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {"number": 42, "pull_request": {"url": "https://api.github/pr"}},
        "comment": {
            "id": 777,
            "body": "@review-agent explain this change",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
        },
    }
    command_body = json_body(command_payload)
    closed_payload = pull_request_payload(
        action="closed", merged=False, head_sha="b" * 40
    )
    closed_body = json_body(closed_payload)

    with make_signed_client(tmp_path) as client:
        client.post(
            "/api/v1/webhooks/github",
            content=opened_body,
            headers=github_headers(opened_body, delivery_id="open-for-command"),
        )
        command = client.post(
            "/api/v1/webhooks/github",
            content=command_body,
            headers=github_headers(
                command_body,
                delivery_id="message-before-close",
                event="issue_comment",
            ),
        ).json()
        client.post(
            "/api/v1/webhooks/github",
            content=closed_body,
            headers=github_headers(closed_body, delivery_id="close-with-command"),
        )
        task = client.get(
            f"/api/v1/agent-tasks/{command['agent_task_id']}"
        ).json()

    assert task["status"] == "running"
    assert task["stage"] == "cancellation_pending"
    assert task["failure_code"] == "pr_closed"


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
        "comment": {
            "id": 123,
            "html_url": "https://github.com/example/repo/pull/42#issuecomment-123",
            "body": "@review-agent explain why this retry is safe",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
        },
    }
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(body, event="issue_comment"),
        )
        task = client.get(
            f"/api/v1/agent-tasks/{response.json()['agent_task_id']}"
        ).json()

    assert response.status_code == 200
    assert response.json()["internal_event"] == "agent_command"
    assert response.json()["status"] == "queued"
    assert response.json()["review_run_id"] is None
    assert response.json()["agent_task_id"] is not None
    assert task["task_type"] == "message_command"
    assert task["status"] == "queued"
    assert task["stage"] == "placeholder_pending"
    assert task["command_text"] == "explain why this retry is safe"
    assert task["source_kind"] == "issue_comment"
    assert task["source_comment_id"] == "123"
    assert task["source_author_login"] == "alice"


def test_bot_authored_mention_does_not_create_agent_task(tmp_path: Path) -> None:
    payload = {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {"number": 42, "pull_request": {"url": "https://api.github/pr"}},
        "comment": {
            "id": 124,
            "body": "@review-agent recursive message",
            "user": {"login": "review-agent", "type": "Bot"},
            "author_association": "MEMBER",
        },
    }
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(
                body,
                delivery_id="delivery-bot-comment",
                event="issue_comment",
            ),
        )

    assert response.status_code == 200
    assert response.json()["internal_event"] == "pr_comment_context"
    assert response.json()["agent_task_id"] is None


def test_untrusted_comment_author_does_not_create_agent_task(tmp_path: Path) -> None:
    payload = {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {"number": 42, "pull_request": {"url": "https://api.github/pr"}},
        "comment": {
            "id": 125,
            "body": "@review-agent inspect the authentication flow",
            "user": {"login": "external-user", "type": "User"},
            "author_association": "NONE",
        },
    }
    body = json_body(payload)

    with make_signed_client(tmp_path) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers=github_headers(
                body,
                delivery_id="delivery-untrusted-comment",
                event="issue_comment",
            ),
        )

    assert response.status_code == 200
    assert response.json()["internal_event"] == "pr_comment_context"
    assert response.json()["agent_task_id"] is None


def test_review_and_line_comment_mentions_create_message_commands(
    tmp_path: Path,
) -> None:
    cases = [
        (
            "pull_request_review",
            "submitted",
            "review",
            126,
        ),
        (
            "pull_request_review_comment",
            "created",
            "comment",
            127,
        ),
    ]
    with make_signed_client(tmp_path) as client:
        for event, action, source_key, source_id in cases:
            payload = {
                "action": action,
                "repository": {"full_name": "example/repo"},
                "pull_request": {"number": 42},
                source_key: {
                    "id": source_id,
                    "html_url": f"https://github.com/example/repo/pull/42#{source_id}",
                    "body": "@review-agent explain this change",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "COLLABORATOR",
                },
            }
            body = json_body(payload)
            response = client.post(
                "/api/v1/webhooks/github",
                content=body,
                headers=github_headers(
                    body,
                    delivery_id=f"delivery-{event}",
                    event=event,
                ),
            )

            assert response.status_code == 200
            assert response.json()["internal_event"] == "agent_command"
            task = client.get(
                f"/api/v1/agent-tasks/{response.json()['agent_task_id']}"
            ).json()
            assert task["task_type"] == "message_command"
            assert task["source_kind"] == event
            assert task["source_comment_id"] == str(source_id)


def test_list_provider_events_filters_and_returns_safe_summary(
    tmp_path: Path,
) -> None:
    opened_payload = pull_request_payload(action="opened")
    opened_body = json_body(opened_payload)
    comment_payload = {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {"number": 42, "pull_request": {"url": "https://api.github/pr"}},
        "comment": {
            "id": 123,
            "body": "@review-agent should I re-review this?",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
        },
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
                "internal_event": "agent_command",
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


async def test_cancel_agent_task_cancels_runtime_and_updates_placeholder(
    tmp_path: Path,
) -> None:
    pi_agent_client = FakePiAgentClient()
    github_client = FakeTaskCommentGitHubClient()
    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = pi_agent_client
        client.app.state.provider_registry = ProviderRegistry(
            [GitHubAdapter(github_client)]
        )
        task = await create_agent_task_record(
            client,
            status="running",
            stage="waiting_for_agent",
            task_type="message_command",
            agent_session_id="session-1",
            response_comment_id="task-comment-1",
            command_text="Explain the retry.",
            source_author_login="alice",
        )

        response = client.post(f"/api/v1/agent-tasks/{task.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["stage"] == "cancelled"
    assert pi_agent_client.cancelled_session_ids == ["session-1"]
    assert len(github_client.updated) == 1
    assert github_client.updated[0][0] == "task-comment-1"
    assert "cancelled" in github_client.updated[0][1]


async def test_retry_failed_agent_task_reuses_placeholder_with_new_attempt(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        task = await create_agent_task_record(
            client,
            status="failed",
            stage="failed",
            task_type="message_command",
            response_comment_id="task-comment-1",
            command_text="Explain the retry.",
            source_author_login="alice",
            failure_code="agent_failed",
            error_message="model failed",
        )

        response = client.post(f"/api/v1/agent-tasks/{task.id}/retry")

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "queued"
    assert data["stage"] == "placeholder_pending"
    assert data["response_comment_id"] == "task-comment-1"
    assert data["failure_code"] is None
    assert data["error_message"] is None


async def test_agent_task_session_diagnostics_reads_live_instruction_session(
    tmp_path: Path,
) -> None:
    pi_agent_client = FakePiAgentClient()
    pi_agent_client.session = PiAgentSession(
        id="session-1",
        kind="instruction",
        status="running",
        stage="tool:read_file",
        provider="openai",
        model="gpt-5.4",
        thinking_level="high",
        events=[
            {
                "at": "2026-07-15T00:00:00Z",
                "type": "tool_execution_start",
                "stage": "tool:read_file",
                "tool": "read_file",
            }
        ],
    )
    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = pi_agent_client
        task = await create_agent_task_record(
            client,
            status="running",
            stage="waiting_for_agent",
            task_type="message_command",
            agent_session_id="session-1",
            command_text="Explain the retry.",
            source_author_login="alice",
        )

        response = client.get(f"/api/v1/agent-tasks/{task.id}/agent-session")

    assert response.status_code == 200
    data = response.json()
    assert data["review_run_id"] is None
    assert data["agent_task_ids"] == [task.id]
    assert data["status"] == "running"
    assert data["stage"] == "waiting_for_agent"
    assert data["execution_status"] == "running"
    assert data["execution_stage"] == "tool:read_file"
    assert data["event_count"] == 1


def test_start_review_session_records_pi_agent_identifiers(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_pi_agent = FakePiAgentClient()

    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = fake_pi_agent
        review_run = seed_review_run(client, payload)
        client.portal.call(make_review_placeholder_ready, client, review_run["id"])

        start_response = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example-repo/pr-42/bbbbbbb"},
        )

    assert start_response.status_code == 200
    data = start_response.json()
    assert data["status"] == "running"
    assert data["workspace_path"] == "/workspaces/example-repo/pr-42/bbbbbbb"
    assert data["agent_session_id"] == "session-1"
    assert data["agent_status"] == "running"
    assert data["agent_provider"] == "openai"
    assert data["agent_model"] == "gpt-5.4"
    assert data["agent_thinking_level"] == "high"
    assert fake_pi_agent.started_inputs[0].repo_full_name == "example/repo"
    assert fake_pi_agent.started_inputs[0].base_sha == "a" * 40


def test_start_review_session_requires_workspace_path(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = FakePiAgentClient()
        review_run = seed_review_run(client, payload)
        client.portal.call(make_review_placeholder_ready, client, review_run["id"])
        start_response = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={},
        )

    assert start_response.status_code == 409
    assert "workspace_path" in start_response.json()["detail"]


def test_start_review_session_rejects_request_overrides_and_uses_domain_preset(
    tmp_path: Path,
) -> None:
    fake_pi_agent = FakePiAgentClient()
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = fake_pi_agent
        review_run = seed_review_run(client, payload)
        client.portal.call(make_review_placeholder_ready, client, review_run["id"])
        rejected = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={
                "workspace_path": "/workspaces/example/repo/pr-42/bbbbbbb",
                "skill": "security-review",
                "profile": "strict",
                "provider": "company-openai",
                "model": "review-model",
                "thinking_level": "xhigh",
                "model_base_url": "https://llm-gateway.example/v1",
            },
        )
        response = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example/repo/pr-42/bbbbbbb"},
        )

    assert rejected.status_code == 422
    assert response.status_code == 200
    assert len(fake_pi_agent.started_options) == 1
    preset = fake_pi_agent.started_options[0]["preset"]
    assert preset.model_dump() == {
        "agent_id": "code-review",
        "task_type": "code-review",
        "repository_skills": ["code-review"],
    }


def test_sync_review_session_marks_pi_agent_failure(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_pi_agent = FakePiAgentClient()
    fake_pi_agent.session = fake_pi_agent.session.model_copy(
        update={"status": "failed", "stage": "failed", "error": "model error"}
    )

    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = fake_pi_agent
        review_run = seed_review_run(client, payload)
        client.portal.call(make_review_placeholder_ready, client, review_run["id"])
        started = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example-repo/pr-42/bbbbbbb"},
        ).json()
        sync_response = client.post(
            f"/api/v1/review-runs/{started['id']}/session/sync",
        )

    assert sync_response.status_code == 200
    assert sync_response.json()["status"] == "failed"
    assert sync_response.json()["failure_code"] == "pi_agent_error"
    assert sync_response.json()["error"] == "model error"


def test_observability_pi_agent_session_returns_safe_metadata(
    tmp_path: Path,
) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_pi_agent = FakePiAgentClient()

    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = fake_pi_agent
        review_run = seed_review_run(client, payload)
        client.portal.call(make_review_placeholder_ready, client, review_run["id"])
        started = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example-repo/pr-42/bbbbbbb"},
        ).json()

        by_run = client.get(
            "/api/v1/observability/review-runs/"
            f"{started['id']}/agent-session"
        )
        by_session = client.get(
            "/api/v1/observability/agent-sessions/session-1"
        )

    assert by_run.status_code == 200
    data = by_run.json()
    assert data["review_run_id"] == started["id"]
    assert data["agent_session_id"] == "session-1"
    assert data["agent_provider"] == "openai"
    assert data["agent_model"] == "gpt-5.4"
    assert data["agent_thinking_level"] == "high"
    assert data["execution_status"] == "running"
    assert data["execution_stage"] == "analyzing"
    assert data["event_count"] == 0
    assert data["session_available"] is True
    assert data["live_status_available"] is True
    assert data["live_status_error"] is None
    assert by_session.status_code == 200
    assert by_session.json()["review_run_id"] == started["id"]


def test_observability_pi_agent_session_reports_disabled_configuration(
    tmp_path: Path,
) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client_with_settings(
        tmp_path,
        pi_agent_base_url=None,
    ) as client:
        review_run = seed_review_run(client, payload)
        response = client.get(
            "/api/v1/observability/review-runs/"
            f"{review_run['id']}/agent-session"
        )

    assert response.status_code == 200
    data = response.json()
    assert data["session_available"] is False
    assert data["live_status_available"] is False
    assert data["live_status_error"] == (
        "pi-agent runtime base URL is not configured."
    )


def test_observability_pi_agent_session_returns_404_for_unknown_session(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        response = client.get(
            "/api/v1/observability/agent-sessions/missing-session"
        )

    assert response.status_code == 404


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
        review_run = seed_review_run(client, payload)
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
    assert data["review_run"]["status"] == "awaiting_delivery"
    assert data["review_run"]["execution_status"] == "completed"
    assert data["review_run"]["stage"] == "completed_delivery_pending"
    assert data["review_run"]["review_summary"] == "One issue found."
    assert data["parsed"]["findings"][0]["publish_as_line_comment"] is True


def test_cancel_review_session_cancels_pi_agent_session(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_pi_agent = FakePiAgentClient()

    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = fake_pi_agent
        review_run = seed_review_run(client, payload)
        client.portal.call(make_review_placeholder_ready, client, review_run["id"])
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
    assert fake_pi_agent.cancelled_session_ids == ["session-1"]


def test_review_session_does_not_expose_human_message_route(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    fake_pi_agent = FakePiAgentClient()

    with make_client(tmp_path) as client:
        client.app.state.pi_agent_client = fake_pi_agent
        review_run = seed_review_run(client, payload)
        client.portal.call(make_review_placeholder_ready, client, review_run["id"])
        started = client.post(
            f"/api/v1/review-runs/{review_run['id']}/session/start",
            json={"workspace_path": "/workspaces/example-repo/pr-42/bbbbbbb"},
        ).json()
        response = client.post(
            f"/api/v1/review-runs/{started['id']}/session/messages",
            json={"message": "Yes, this is intentional.", "delivery": "answer"},
        )

    assert response.status_code == 404


def test_public_api_not_found_contracts(tmp_path: Path) -> None:
    """All resource endpoints must keep a stable 404 contract during reorganization."""
    requests = [
        ("GET", "/api/v1/provider-events/missing", None),
        ("GET", "/api/v1/agent-tasks/missing", None),
        ("GET", "/api/v1/agent-tasks/missing/agent-session", None),
        ("POST", "/api/v1/agent-tasks/missing/cancel", None),
        ("POST", "/api/v1/agent-tasks/missing/retry", None),
        ("GET", "/api/v1/review-runs/missing", None),
        ("GET", "/api/v1/observability/review-runs/missing/agent-session", None),
        (
            "POST",
            "/api/v1/review-runs/missing/session/start",
            {"workspace_path": "/tmp/missing"},
        ),
        ("POST", "/api/v1/review-runs/missing/session/sync", None),
        (
            "POST",
            "/api/v1/review-runs/missing/session/cancel",
            {"reason": "test"},
        ),
        (
            "POST",
            "/api/v1/review-runs/missing/session/messages",
            {"message": "answer", "delivery": "answer"},
        ),
        (
            "POST",
            "/api/v1/review-runs/missing/result",
            {"raw_output": {"summary": "none", "findings": []}},
        ),
        ("POST", "/api/v1/review-runs/missing/retry", None),
        (
            "POST",
            "/api/v1/review-runs/missing/rerun",
            {
                "revision": "same_revision",
                "idempotency_key": "11111111-1111-4111-8111-111111111111",
            },
        ),
        ("POST", "/api/v1/review-runs/missing/cancel", None),
        ("GET", "/api/v1/workspaces/missing", None),
        ("POST", "/api/v1/workspaces/missing/lease", {}),
        ("POST", "/api/v1/workspace-leases/missing/release", None),
        ("POST", "/api/v1/workspaces/missing/cleanup", {"force": False}),
    ]

    with make_client(tmp_path) as client:
        for method, path, body in requests:
            response = client.request(method, path, json=body)
            assert response.status_code == 404, (method, path, response.text)


def test_webhook_rejects_unknown_provider_and_malformed_payload(tmp_path: Path) -> None:
    malformed = b"not-json"
    with make_signed_client(tmp_path) as client:
        unsupported = client.post("/api/v1/webhooks/unknown", content=b"{}")
        invalid = client.post(
            "/api/v1/webhooks/github",
            content=malformed,
            headers=github_headers(malformed, delivery_id="malformed"),
        )

    assert unsupported.status_code == 404
    assert unsupported.json()["detail"] == "Unsupported provider: unknown"
    assert invalid.status_code == 400


def test_runtime_required_endpoint_fails_cleanly_when_disabled(tmp_path: Path) -> None:
    with make_client_with_settings(tmp_path, pi_agent_base_url=None) as client:
        response = client.get("/api/v1/agent-tasks/missing/agent-session")

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "pi-agent runtime base URL is not configured."
    )


def test_retry_failed_review_run_creates_next_attempt(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    async def inspect_created(
        client: TestClient,
        source_id: str,
        run_id: str,
        event_id: str,
    ) -> None:
        async with client.app.state.session_factory() as session:
            source = await session.get(ReviewRun, source_id)
            review_run = await session.get(ReviewRun, run_id)
            event = await session.get(ProviderEventInbox, event_id)
            slot = (
                await session.execute(
                    select(ReviewCommentSlot).where(
                        ReviewCommentSlot.review_run_id == run_id
                    )
                )
            ).scalar_one()
            delivery = (
                await session.execute(
                    select(DeliveryOutbox).where(
                        DeliveryOutbox.task_id == run_id,
                        DeliveryOutbox.operation == "review_placeholder",
                    )
                )
            ).scalar_one()
            assert source is not None
            assert review_run is not None
            assert event is not None
            assert review_run.pull_request_context_id == source.pull_request_context_id
            assert review_run.trigger_type == "retry"
            assert review_run.trigger_event_id == event.id
            assert review_run.queue == "manual-review"
            assert review_run.priority == 60
            assert review_run.attempt == 2
            assert review_run.status == "awaiting_delivery"
            assert review_run.stage == "placeholder_delivery_pending"
            assert slot.status == "pending"
            assert delivery.status == "queued"
            assert delivery.mandatory is True
            assert delivery.max_attempts == 0
            assert delivery.payload_json["slot_id"] == slot.id
            assert event.provider == "internal"
            assert event.provider_event == "review_request"
            assert event.provider_action == "retry"
            assert event.internal_event == "review_requested"
            assert event.status == "queued"
            assert event.review_run_id == review_run.id
            assert event.payload["source_review_run_id"] == source.id

    with make_client(tmp_path) as client:
        original = seed_review_run(client, payload)
        client.portal.call(make_review_run_retryable, client, original["id"])
        detail_before = client.get(
            f"/api/v1/observability/review-runs/{original['id']}"
        )
        first = client.post(f"/api/v1/review-runs/{original['id']}/retry")
        duplicate = client.post(f"/api/v1/review-runs/{original['id']}/retry")
        detail_after = client.get(
            f"/api/v1/observability/review-runs/{original['id']}"
        )

        assert detail_before.status_code == 200
        assert detail_before.json()["available_actions"]["retry"]["allowed"] is True
        assert (
            detail_before.json()["available_actions"]["rerun"]["reason_code"]
            == "failed_run_requires_retry"
        )
        assert first.status_code == 202
        assert first.json()["source_review_run_id"] == original["id"]
        assert first.json()["review_run_id"] != original["id"]
        assert first.json()["attempt"] == 2
        assert first.json()["status"] == "awaiting_delivery"
        assert first.json()["deduplicated"] is False
        assert duplicate.status_code == 202
        assert duplicate.json()["review_run_id"] == first.json()["review_run_id"]
        assert duplicate.json()["review_request_event_id"] == first.json()[
            "review_request_event_id"
        ]
        assert duplicate.json()["deduplicated"] is True
        assert (
            detail_after.json()["available_actions"]["retry"]["reason_code"]
            == "retry_already_requested"
        )
        client.portal.call(
            inspect_created,
            client,
            original["id"],
            first.json()["review_run_id"],
            first.json()["review_request_event_id"],
        )


def test_pi_agent_tool_can_request_retry_and_rerun_for_its_current_pr(
    tmp_path: Path,
) -> None:
    headers = {"Authorization": "Bearer runtime-tool-secret"}
    with make_client_with_settings(
        tmp_path,
        pi_agent_runtime_token="runtime-tool-secret",
    ) as client:
        for offset, action in enumerate(("retry", "rerun")):
            payload = {
                "provider": "github",
                "repo_full_name": "example/repo",
                "pull_request_number": 42 + offset,
                "base_sha": "a" * 40,
                "head_sha": chr(ord("b") + offset) * 40,
            }
            source = seed_review_run(client, payload)
            if action == "retry":
                client.portal.call(
                    make_review_run_retryable,
                    client,
                    source["id"],
                )
            else:
                client.portal.call(
                    make_review_run_rerunnable,
                    client,
                    source["id"],
                )
            task = client.portal.call(
                create_running_review_action_task,
                client,
                source["id"],
                None if offset == 0 else "agent-tool-session",
            )
            request = {
                "agent_task_id": task.id,
                "agent_session_id": "agent-tool-session",
                "action": action,
            }

            unauthorized = client.post(
                "/api/v1/internal/agent-tools/review-action",
                json=request,
            )
            first = client.post(
                "/api/v1/internal/agent-tools/review-action",
                headers=headers,
                json=request,
            )
            duplicate = client.post(
                "/api/v1/internal/agent-tools/review-action",
                headers=headers,
                json=request,
            )
            wrong_session = client.post(
                "/api/v1/internal/agent-tools/review-action",
                headers=headers,
                json={**request, "agent_session_id": "another-session"},
            )
            event = client.get(
                "/api/v1/observability/provider-events/"
                f"{first.json()['review_request_event_id']}"
            )
            created = client.get(
                f"/api/v1/review-runs/{first.json()['review_run_id']}"
            )

            assert unauthorized.status_code == 401
            assert first.status_code == 202
            assert first.json()["action"] == action
            assert first.json()["source_review_run_id"] == source["id"]
            assert first.json()["attempt"] == 2
            assert first.json()["status"] == "awaiting_delivery"
            assert duplicate.status_code == 202
            assert duplicate.json()["review_run_id"] == first.json()["review_run_id"]
            assert duplicate.json()["deduplicated"] is True
            assert wrong_session.status_code == 409
            assert wrong_session.json()["detail"]["code"] == (
                "agent_session_mismatch"
            )
            assert event.status_code == 200
            assert event.json()["provider"] == "internal"
            assert event.json()["provider_action"] == action
            assert event.json()["internal_event"] == "review_requested"
            assert created.status_code == 200
            assert created.json()["stage"] == "placeholder_delivery_pending"
            if action == "retry":
                source_detail = client.get(
                    f"/api/v1/observability/review-runs/{source['id']}"
                )
                assert source_detail.status_code == 200
                assert (
                    source_detail.json()["available_actions"]["retry"][
                        "reason_code"
                    ]
                    == "retry_already_requested"
                )


def test_pi_agent_review_action_tool_fails_closed_without_callback_token(
    tmp_path: Path,
) -> None:
    with make_client_with_settings(
        tmp_path,
        pi_agent_runtime_token=None,
    ) as client:
        response = client.post(
            "/api/v1/internal/agent-tools/review-action",
            json={
                "agent_task_id": "task-id",
                "agent_session_id": "session-id",
                "action": "retry",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "pi-agent Tool callback token is not configured."
    )


def test_retry_rejections_keep_explainable_internal_events(tmp_path: Path) -> None:
    def assert_rejected(client: TestClient, run_id: str, code: str) -> dict:
        response = client.post(f"/api/v1/review-runs/{run_id}/retry")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == code
        event_id = response.json()["detail"]["review_request_event_id"]
        event = client.get(f"/api/v1/observability/provider-events/{event_id}")
        assert event.status_code == 200
        assert event.json()["status"] == "rejected"
        assert event.json()["error_code"] == code
        assert event.json()["source_review_run_id"] == run_id
        assert event.json()["review_run_id"] is None
        run = client.get(f"/api/v1/observability/review-runs/{run_id}")
        assert run.status_code == 200
        assert run.json()["available_actions"]["retry"]["allowed"] is False
        assert run.json()["available_actions"]["retry"]["reason_code"] == code
        return response.json()["detail"]

    base_payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        stale = seed_review_run(
            client,
            {**base_payload, "pull_request_number": 41},
        )
        client.portal.call(
            make_review_run_retryable,
            client,
            stale["id"],
            "c" * 40,
        )
        assert_rejected(client, stale["id"], "revision_not_current")

        closed = seed_review_run(
            client,
            {**base_payload, "pull_request_number": 42},
        )
        client.portal.call(
            make_review_run_retryable,
            client,
            closed["id"],
            None,
            "closed",
        )
        assert_rejected(client, closed["id"], "pull_request_closed")

        blocked = seed_review_run(
            client,
            {**base_payload, "pull_request_number": 43},
        )
        client.portal.call(make_review_run_retryable, client, blocked["id"])
        active = seed_review_run(
            client,
            {**base_payload, "pull_request_number": 43, "force": True},
        )
        rejection = assert_rejected(client, blocked["id"], "active_review_exists")
        assert rejection["existing_review_run_id"] == active["id"]


def test_dashboard_rerun_records_internal_event_and_creates_new_attempt(
    tmp_path: Path,
) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    request_payload = {
        "revision": "same_revision",
        "idempotency_key": "11111111-1111-4111-8111-111111111111",
    }

    async def inspect_created(client: TestClient, run_id: str, event_id: str) -> None:
        async with client.app.state.session_factory() as session:
            review_run = await session.get(ReviewRun, run_id)
            event = await session.get(ProviderEventInbox, event_id)
            slot = (
                await session.execute(
                    select(ReviewCommentSlot).where(
                        ReviewCommentSlot.review_run_id == run_id
                    )
                )
            ).scalar_one()
            delivery = (
                await session.execute(
                    select(DeliveryOutbox).where(
                        DeliveryOutbox.task_id == run_id,
                        DeliveryOutbox.operation == "review_placeholder",
                    )
                )
            ).scalar_one()
            assert review_run is not None
            assert event is not None
            assert review_run.trigger_type == "dashboard_rerun"
            assert review_run.trigger_event_id == event.id
            assert review_run.queue == "manual-review"
            assert review_run.priority == 60
            assert review_run.attempt == 2
            assert review_run.status == "awaiting_delivery"
            assert review_run.stage == "placeholder_delivery_pending"
            assert slot.status == "pending"
            assert delivery.status == "queued"
            assert delivery.payload_json["slot_id"] == slot.id
            assert event.provider == "internal"
            assert event.provider_event == "review_request"
            assert event.provider_action == "rerun"
            assert event.internal_event == "review_requested"
            assert event.status == "queued"
            assert event.review_run_id == review_run.id
            assert event.payload["source_review_run_id"] == original["id"]

    with make_client(tmp_path) as client:
        original = seed_review_run(client, payload)
        client.portal.call(make_review_run_rerunnable, client, original["id"])

        detail = client.get(
            f"/api/v1/observability/review-runs/{original['id']}"
        )
        first = client.post(
            f"/api/v1/review-runs/{original['id']}/rerun",
            json=request_payload,
        )
        duplicate = client.post(
            f"/api/v1/review-runs/{original['id']}/rerun",
            json=request_payload,
        )

        assert detail.status_code == 200
        availability = detail.json()["available_actions"]["rerun"]
        assert availability["allowed"] is True
        assert availability["next_attempt"] == 2
        assert first.status_code == 202
        assert first.json()["source_review_run_id"] == original["id"]
        assert first.json()["attempt"] == 2
        assert first.json()["status"] == "awaiting_delivery"
        assert first.json()["deduplicated"] is False
        assert duplicate.status_code == 202
        assert duplicate.json()["review_run_id"] == first.json()["review_run_id"]
        assert duplicate.json()["review_request_event_id"] == first.json()[
            "review_request_event_id"
        ]
        assert duplicate.json()["deduplicated"] is True
        client.portal.call(
            inspect_created,
            client,
            first.json()["review_run_id"],
            first.json()["review_request_event_id"],
        )


def test_dashboard_rerun_rejects_stale_revision_and_keeps_event(tmp_path: Path) -> None:
    payload = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }

    with make_client(tmp_path) as client:
        original = seed_review_run(client, payload)
        client.portal.call(
            make_review_run_rerunnable,
            client,
            original["id"],
            "c" * 40,
        )
        response = client.post(
            f"/api/v1/review-runs/{original['id']}/rerun",
            json={
                "revision": "same_revision",
                "idempotency_key": "22222222-2222-4222-8222-222222222222",
            },
        )
        events = client.get(
            "/api/v1/observability/provider-events",
            params={"provider": "internal", "internal_event": "review_requested"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "revision_not_current"
    assert events.status_code == 200
    assert events.json()["total"] == 1
    event = events.json()["items"][0]
    assert event["status"] == "rejected"
    assert event["error_code"] == "revision_not_current"
    assert event["review_run_id"] is None
