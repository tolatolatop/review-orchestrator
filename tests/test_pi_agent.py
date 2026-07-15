import json

import httpx
import pytest

from review_orchestrator.config import Settings
from review_orchestrator.pi_agent import (
    AgentDomainPreset,
    AgentDomainPresetLimits,
    AgentDomainPresetModel,
    AgentDomainPresetOverrides,
    AgentInstructionInput,
    AgentInstructionRepositoryContext,
    AgentPresetResourceReference,
    PiAgentClient,
    PiAgentClientError,
    PiAgentSessionStatus,
)
from review_orchestrator.review_results import ReviewSkillInput


def review_input() -> ReviewSkillInput:
    return ReviewSkillInput(
        provider="github",
        repo_full_name="example/repo",
        pr_number=42,
        base_sha="a" * 40,
        head_sha="b" * 40,
        workspace_path="/workspaces/github/repo/pr-42/head/repo",
    )


def review_preset() -> AgentDomainPreset:
    return AgentDomainPreset(
        agent_id="code-review",
        task_type="code-review",
        repository_skills=["builtin:code-review"],
    )


def test_runtime_token_uses_public_environment_name(monkeypatch) -> None:
    monkeypatch.setenv("PI_AGENT_RUNTIME_TOKEN", "runtime-secret")

    settings = Settings(_env_file=None)

    assert settings.pi_agent_runtime_token == "runtime-secret"


def test_start_payload_keeps_workspace_and_only_domain_preset_selectors() -> None:
    client = PiAgentClient(base_url="http://pi-agent:3210")

    payload = client._start_payload(
        review_input(),
        preset=review_preset(),
    )

    assert payload["workspace_path"] == review_input().workspace_path
    assert payload["agent_id"] == "code-review"
    assert payload["input"]["base_sha"] == "a" * 40
    assert payload["input"]["head_sha"] == "b" * 40
    assert payload["task_type"] == "code-review"
    assert payload["repository_skills"] == ["builtin:code-review"]
    assert "profile" not in payload
    assert "model" not in payload
    assert "agent_version" not in payload


def test_start_payload_serializes_database_resource_overrides() -> None:
    client = PiAgentClient(base_url="http://pi-agent:3210")
    preset = AgentDomainPreset(
        agent_id="code-review",
        task_type="code-review",
        repository_skills=["code-review", "security-analysis"],
        resource=AgentPresetResourceReference(
            id="preset-1",
            name="security-review",
            revision=3,
        ),
        overrides=AgentDomainPresetOverrides(
            model=AgentDomainPresetModel(
                provider="company-openai",
                id="review-model",
                thinking_level="medium",
            ),
            tools=["repository.git-diff", "repository.read-file"],
            limits=AgentDomainPresetLimits(
                max_turns=12,
                max_tool_calls=40,
                max_result_bytes=120000,
            ),
        ),
    )

    payload = client._start_payload(review_input(), preset=preset)

    assert payload["preset_resource"] == {
        "id": "preset-1",
        "name": "security-review",
        "revision": 3,
    }
    assert payload["preset_overrides"] == {
        "model": {
            "provider": "company-openai",
            "id": "review-model",
            "thinking_level": "medium",
        },
        "tools": ["repository.git-diff", "repository.read-file"],
        "limits": {
            "maxTurns": 12,
            "maxToolCalls": 40,
            "maxResultBytes": 120000,
        },
    }


def test_instruction_payload_keeps_command_context_history_and_idempotency() -> None:
    client = PiAgentClient(base_url="http://pi-agent:3210")
    instruction = AgentInstructionInput(
        idempotency_key="agent-task:task-1:attempt:1",
        workspace_path="/workspaces/github/repo/pr-42/head/repo",
        repository_context=AgentInstructionRepositoryContext(
            provider="github",
            repo_full_name="example/repo",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        ),
        text="Explain why this retry is safe.",
        author_login="alice",
        source_url="https://github.com/example/repo/pull/42#issuecomment-123",
        history=[
            {
                "author_login": "alice",
                "command": "Where is retry configured?",
                "answer": "In src/retry.py.",
                "outcome": "answered",
                "head_sha": "b" * 40,
            }
        ],
    )

    payload = client._instruction_start_payload(
        instruction,
        preset=AgentDomainPreset(
            agent_id="pr-assistant",
            task_type="message-command",
            repository_skills=["npm:@example/pr-skill"],
        ),
    )

    assert payload["agent_id"] == "pr-assistant"
    assert payload["idempotency_key"] == "agent-task:task-1:attempt:1"
    assert payload["workspace_path"] == instruction.workspace_path
    assert payload["input"]["repository_context"]["head_sha"] == "b" * 40
    assert payload["input"]["instruction"]["text"] == (
        "Explain why this retry is safe."
    )
    assert payload["input"]["instruction"]["history"][0]["answer"] == (
        "In src/retry.py."
    )
    assert payload["task_type"] == "message-command"
    assert payload["repository_skills"] == ["npm:@example/pr-skill"]


@pytest.mark.asyncio
async def test_client_starts_instruction_session() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202,
            json={
                "id": "instruction-session-1",
                "kind": "instruction",
                "status": "running",
                "stage": "analyzing",
                "provider": "openai",
                "model": "gpt-5.4",
            },
        )

    client = PiAgentClient(
        base_url="http://pi-agent:3210",
        transport=httpx.MockTransport(handler),
    )
    instruction = AgentInstructionInput(
        idempotency_key="agent-task:task-1:attempt:1",
        workspace_path="/workspaces/github/repo/pr-42/head/repo",
        repository_context=AgentInstructionRepositoryContext(
            provider="github",
            repo_full_name="example/repo",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        ),
        text="Explain the retry.",
        author_login="alice",
    )

    session = await client.start_instruction_session(
        instruction,
        preset=AgentDomainPreset(
            agent_id="pr-assistant",
            task_type="message-command",
            repository_skills=["builtin:pr-assistant"],
        ),
    )

    assert session.id == "instruction-session-1"
    assert session.kind == "instruction"
    assert json.loads(requests[0].content)["input"]["instruction"]["text"] == (
        "Explain the retry."
    )


@pytest.mark.asyncio
async def test_client_starts_a_generic_domain_preset() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202,
            json={
                "id": "assistant-session-1",
                "kind": "agent",
                "agent_id": "pr-assistant",
                "agent_version": "1.0.0",
                "status": "preparing",
                "stage": "preparing",
            },
        )

    client = PiAgentClient(
        base_url="http://pi-agent:3210",
        transport=httpx.MockTransport(handler),
    )
    session = await client.start_agent_session(
        preset=AgentDomainPreset(
            agent_id="pr-assistant",
            task_type="message-command",
            repository_skills=["builtin:pr-assistant"],
        ),
        workspace_path="/workspaces/example/repo",
        input_data={
            "repository_context": {
                "provider": "github",
                "repo_full_name": "example/repo",
                "pr_number": 42,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            "instruction": {"text": "Explain this", "author_login": "alice"},
        },
    )

    assert session.kind == "agent"
    assert session.agent_id == "pr-assistant"
    payload = json.loads(requests[0].content)
    assert "agent_version" not in payload
    assert payload["task_type"] == "message-command"


@pytest.mark.asyncio
async def test_client_starts_syncs_and_cancels_session() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(
                202,
                json={
                    "id": "session-1",
                    "status": "running",
                    "stage": "analyzing",
                    "provider": "openai",
                    "model": "gpt-5.4",
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "session-1",
                    "status": "running",
                    "stage": "analyzing",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "session-1",
                "status": "cancelled",
                "stage": "cancelled",
            },
        )

    client = PiAgentClient(
        base_url="http://pi-agent:3210",
        api_token="service-secret",
        transport=httpx.MockTransport(handler),
    )

    started = await client.start_session(
        review_input(),
        preset=review_preset(),
    )
    running = await client.get_session(started.id)
    cancelled = await client.cancel_session(started.id)

    assert started.status == PiAgentSessionStatus.running
    assert running.status == PiAgentSessionStatus.running
    assert cancelled.status == PiAgentSessionStatus.cancelled
    assert requests[0].headers["Authorization"] == "Bearer service-secret"
    assert requests[2].method == "DELETE"


@pytest.mark.asyncio
async def test_client_classifies_transport_and_http_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="invalid model")

    client = PiAgentClient(
        base_url="http://pi-agent:3210",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PiAgentClientError) as captured:
        await client.get_session("missing")

    assert captured.value.status_code == 422
    assert not captured.value.infrastructure_failure
