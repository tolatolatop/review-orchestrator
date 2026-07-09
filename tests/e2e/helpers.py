from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from copy import deepcopy
from dataclasses import dataclass
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

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
WEBHOOK_SECRET = "e2e-secret"


class FakeOpenHandsClient:
    def __init__(self) -> None:
        self.started_inputs: list[Any] = []
        self.deleted_conversation_ids: list[str] = []
        self.start_task = OpenHandsStartTask(
            id="task-e2e-1",
            status=OpenHandsStartTaskStatus.ready,
            app_conversation_id="conversation-e2e-1",
            sandbox_id="sandbox-e2e-1",
            agent_server_url="http://openhands.test/agent",
        )
        self.conversation = OpenHandsConversation(
            id="conversation-e2e-1",
            sandbox_status="RUNNING",
            execution_status="FINISHED",
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


@dataclass(frozen=True)
class TempPullRequestRepo:
    clone_url: str
    base_sha: str
    head_sha: str
    second_head_sha: str


def make_e2e_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/e2e.db",
        github_webhook_secret=WEBHOOK_SECRET,
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    client = TestClient(create_app(settings))
    return client


def create_temp_pull_request_repo(tmp_path: Path) -> TempPullRequestRepo:
    repo = tmp_path / "source-repo"
    repo.mkdir()
    _run_git(["git", "init", "-b", "main"], repo)
    _run_git(["git", "config", "user.email", "reviewer@example.com"], repo)
    _run_git(["git", "config", "user.name", "Review Bot"], repo)

    auth_file = repo / "src" / "auth.py"
    auth_file.parent.mkdir()
    auth_file.write_text(
        "def validate_token(token: str) -> bool:\n"
        "    return token == 'prod-token'\n",
        encoding="utf-8",
    )
    _run_git(["git", "add", "."], repo)
    _run_git(["git", "commit", "-m", "base auth implementation"], repo)
    base_sha = _git_output(["git", "rev-parse", "HEAD"], repo)

    auth_file.write_text(
        "def validate_token(token: str) -> bool:\n"
        "    return token in {'prod-token', 'demo-token'}\n",
        encoding="utf-8",
    )
    _run_git(["git", "add", "."], repo)
    _run_git(["git", "commit", "-m", "allow demo token"], repo)
    head_sha = _git_output(["git", "rev-parse", "HEAD"], repo)

    auth_file.write_text(
        "def validate_token(token: str) -> bool:\n"
        "    if token == 'demo-token':\n"
        "        return False\n"
        "    return token == 'prod-token'\n",
        encoding="utf-8",
    )
    _run_git(["git", "add", "."], repo)
    _run_git(["git", "commit", "-m", "reject demo token"], repo)
    second_head_sha = _git_output(["git", "rev-parse", "HEAD"], repo)

    return TempPullRequestRepo(
        clone_url=str(repo),
        base_sha=base_sha,
        head_sha=head_sha,
        second_head_sha=second_head_sha,
    )


def given_github_pr_webhook(
    fixture_name: str,
    repo: TempPullRequestRepo,
    *,
    head_sha: str | None = None,
) -> dict[str, Any]:
    payload = load_json_fixture(fixture_name)
    replacements = {
        "__CLONE_URL__": repo.clone_url,
        "__BASE_SHA__": repo.base_sha,
        "__HEAD_SHA__": head_sha or repo.head_sha,
    }
    return _replace_tokens(payload, replacements)


def when_github_delivers(
    client: TestClient,
    payload: dict[str, Any],
    *,
    delivery_id: str,
    event: str = "pull_request",
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers=github_headers(body, delivery_id=delivery_id, event=event),
    )
    assert response.status_code == 200, response.text
    return response.json()


def load_json_fixture(name: str) -> Any:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def github_headers(
    body: bytes,
    *,
    delivery_id: str,
    event: str,
) -> dict[str, str]:
    signature = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }


def _replace_tokens(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        replaced = value
        for token, replacement in replacements.items():
            replaced = replaced.replace(token, replacement)
        return replaced
    if isinstance(value, list):
        return [_replace_tokens(item, replacements) for item in value]
    if isinstance(value, dict):
        clone = deepcopy(value)
        return {
            key: _replace_tokens(item, replacements) for key, item in clone.items()
        }
    return value


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _git_output(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
