import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app
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
