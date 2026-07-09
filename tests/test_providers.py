import json
from pathlib import Path

from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.gitlab import normalize_gitlab_event
from review_orchestrator.main import create_app


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        gitlab_webhook_secret="secret",
    )
    return TestClient(create_app(settings))


def gitlab_payload(action: str = "open") -> dict:
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
            "url": "https://gitlab.com/example/repo/-/merge_requests/42",
        },
    }


def test_gitlab_adapter_normalizes_merge_request_actions() -> None:
    opened = normalize_gitlab_event("Merge Request Hook", gitlab_payload("open"))
    updated = normalize_gitlab_event("Merge Request Hook", gitlab_payload("update"))
    merged = normalize_gitlab_event("Merge Request Hook", gitlab_payload("merge"))

    assert opened.internal_event == "pr_opened"
    assert opened.should_create_review_run is True
    assert updated.internal_event == "pr_updated"
    assert merged.internal_event == "pr_merged"
    assert merged.should_create_review_run is False


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
