import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app


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
        "repository": "example/repo",
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
