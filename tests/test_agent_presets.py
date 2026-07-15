from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.main import create_app
from review_orchestrator.models import AgentPreset, ReviewConfig, Task
from review_orchestrator.pi_agent import PiAgentSession
from tests.factories import ReviewRunCreate, create_review_run


class RecordingPiAgentClient:
    def __init__(self) -> None:
        self.presets: list[Any] = []

    async def start_session(self, _review_input, *, preset):
        self.presets.append(preset)
        return PiAgentSession(
            id="preset-session-1",
            status="running",
            stage="analyzing",
            provider="company-openai",
            model="review-model",
            thinking_level="medium",
        )


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                _env_file=None,
                database_url=f"sqlite+aiosqlite:///{tmp_path}/presets.db",
            )
        )
    )


def repository_preset_payload() -> dict:
    return {
        "name": "owner-repo-review",
        "description": "Security-focused repository review.",
        "task_kind": "review",
        "scope": "repository",
        "provider": "github",
        "repo_full_name": "owner/repo",
        "agent_id": "code-review",
        "task_type": "code-review",
        "repository_skills": ["code-review", "security-analysis"],
        "model": {
            "provider": "company-openai",
            "id": "review-model",
            "thinking_level": "medium",
        },
        "tools": [
            "repository.list-files",
            "repository.read-file",
            "repository.search-code",
            "repository.git-diff",
        ],
        "limits": {
            "max_turns": 12,
            "max_tool_calls": 40,
            "max_result_bytes": 120000,
        },
        "enabled": True,
    }


async def task_preset_snapshot(client: TestClient, task_id: str) -> dict:
    async with client.app.state.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.resolved_preset_json is not None
        return task.resolved_preset_json


async def create_preset_review_run(client: TestClient) -> str:
    async with client.app.state.session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="owner/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        return review_run.id


def test_agent_preset_resource_crud_and_scope_uniqueness(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        defaults = client.get("/api/v1/agent-presets")
        assert defaults.status_code == 200
        assert defaults.json()["total"] == 2
        assert {item["name"] for item in defaults.json()["items"]} == {
            "default-review",
            "default-agent-task",
        }

        created = client.post(
            "/api/v1/agent-presets",
            json=repository_preset_payload(),
        )
        assert created.status_code == 201
        preset = created.json()
        assert preset["scope"] == "repository"
        assert preset["revision"] == 1
        assert preset["limits"]["max_turns"] == 12

        fetched = client.get(f"/api/v1/agent-presets/{preset['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == preset

        filtered = client.get(
            "/api/v1/agent-presets",
            params={
                "task_kind": "review",
                "provider": "github",
                "repo_full_name": "owner/repo",
            },
        )
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()["items"]] == [preset["id"]]

        conflict_payload = repository_preset_payload()
        conflict_payload["name"] = "another-owner-repo-review"
        conflict = client.post("/api/v1/agent-presets", json=conflict_payload)
        assert conflict.status_code == 409

        updated = client.patch(
            f"/api/v1/agent-presets/{preset['id']}",
            json={"enabled": False, "model": None, "limits": {"max_turns": 8}},
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert updated.json()["enabled"] is False
        assert updated.json()["model"] is None
        assert updated.json()["limits"] == {
            "max_turns": 8,
            "max_tool_calls": None,
            "max_result_bytes": None,
        }

        deleted = client.delete(f"/api/v1/agent-presets/{preset['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/agent-presets/{preset['id']}").status_code == 404


def test_agent_preset_validation_rejects_invalid_scope_and_empty_patch(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        invalid = repository_preset_payload()
        invalid.update({"scope": "global", "repo_full_name": None})
        assert client.post("/api/v1/agent-presets", json=invalid).status_code == 422

        preset_id = client.get(
            "/api/v1/agent-presets",
            params={"task_kind": "review", "scope": "global"},
        ).json()["items"][0]["id"]
        empty_patch = client.patch(
            f"/api/v1/agent-presets/{preset_id}",
            json={},
        )
        assert empty_patch.status_code == 422
        incomplete_scope = client.patch(
            f"/api/v1/agent-presets/{preset_id}",
            json={"scope": "repository"},
        )
        assert incomplete_scope.status_code == 422


def test_review_uses_repository_preset_resource_and_records_snapshot(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        created_preset = client.post(
            "/api/v1/agent-presets",
            json=repository_preset_payload(),
        )
        assert created_preset.status_code == 201
        preset_resource = created_preset.json()

        review_run_id = client.portal.call(create_preset_review_run, client)
        runtime = RecordingPiAgentClient()
        client.app.state.pi_agent_client = runtime
        started = client.post(
            f"/api/v1/review-runs/{review_run_id}/session/start",
            json={"workspace_path": str(tmp_path / "workspace")},
        )
        assert started.status_code == 200

        selected = runtime.presets[0]
        assert selected.repository_skills == ["code-review", "security-analysis"]
        assert selected.resource.id == preset_resource["id"]
        assert selected.resource.revision == 1
        assert selected.overrides.model.provider == "company-openai"
        assert selected.overrides.limits.max_turns == 12

        snapshot = client.portal.call(task_preset_snapshot, client, review_run_id)
        assert snapshot["resource"] == {
            "id": preset_resource["id"],
            "name": "owner-repo-review",
            "revision": 1,
        }
        assert snapshot["overrides"]["limits"]["maxTurns"] == 12


async def test_preset_table_migration_preserves_repository_skill_selections(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/preset-migration.db",
    )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await init_models(engine, settings)
        async with factory() as session:
            session.add(
                ReviewConfig(
                    provider="github",
                    repo_full_name="legacy/repo",
                    default_review_skill="security-analysis",
                    default_agent_command_skill="pr-assistant",
                )
            )
            await session.commit()
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE agent_preset"))

        await init_models(engine, settings)

        async with factory() as session:
            migrated = list(
                (
                    await session.execute(
                        select(AgentPreset).where(
                            AgentPreset.scope == "repository",
                            AgentPreset.provider == "github",
                            AgentPreset.repo_full_name == "legacy/repo",
                        )
                    )
                ).scalars()
            )
        assert {item.task_kind for item in migrated} == {"review", "agent_task"}
        review = next(item for item in migrated if item.task_kind == "review")
        assert review.repository_skills_json == ["security-analysis"]
    finally:
        await engine.dispose()
