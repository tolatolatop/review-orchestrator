from pathlib import Path

from fastapi.testclient import TestClient

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    return TestClient(create_app(settings))


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


def test_accept_webhook(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/webhooks/github", json={"action": "opened"})

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "provider": "github"}
