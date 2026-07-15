import json

import httpx
import pytest

from review_orchestrator.config import Settings
from review_orchestrator.pi_agent import (
    AgentInstructionInput,
    AgentInstructionRepositoryContext,
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


def test_runtime_token_uses_public_environment_name(monkeypatch) -> None:
    monkeypatch.setenv("PI_AGENT_RUNTIME_TOKEN", "runtime-secret")

    settings = Settings(_env_file=None)

    assert settings.pi_agent_runtime_token == "runtime-secret"


def test_start_payload_keeps_isolated_workspace_and_llm_configuration() -> None:
    client = PiAgentClient(base_url="http://pi-agent:3210")

    payload = client._start_payload(
        review_input(),
        skill="security-review",
        profile="strict",
        provider="openai",
        model="gpt-5.4",
        thinking_level="high",
        model_base_url="https://llm-gateway.example/v1",
    )

    assert payload["workspace_path"] == review_input().workspace_path
    assert payload["review"]["base_sha"] == "a" * 40
    assert payload["review"]["head_sha"] == "b" * 40
    assert payload["skills"] == ["security-review"]
    assert payload["profile"] == "strict"
    assert payload["model"] == {
        "provider": "openai",
        "id": "gpt-5.4",
        "thinking_level": "high",
        "base_url": "https://llm-gateway.example/v1",
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
        skill="pr-assistant",
        profile="default",
        provider="openai",
        model="gpt-5.4",
        thinking_level="high",
        model_base_url=None,
    )

    assert payload["kind"] == "instruction"
    assert payload["idempotency_key"] == "agent-task:task-1:attempt:1"
    assert payload["workspace_path"] == instruction.workspace_path
    assert payload["repository_context"]["head_sha"] == "b" * 40
    assert payload["instruction"]["text"] == "Explain why this retry is safe."
    assert payload["instruction"]["history"][0]["answer"] == "In src/retry.py."
    assert payload["skills"] == ["pr-assistant"]


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
        skill="pr-assistant",
        profile="default",
        provider="openai",
        model="gpt-5.4",
        thinking_level="high",
    )

    assert session.id == "instruction-session-1"
    assert session.kind == "instruction"
    assert json.loads(requests[0].content)["instruction"]["text"] == (
        "Explain the retry."
    )


@pytest.mark.asyncio
async def test_client_starts_syncs_and_steers_session() -> None:
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
                    "status": "waiting_for_input",
                    "stage": "waiting_for_human",
                    "pending_input": {
                        "id": "question-1",
                        "question": "Is this flag intentionally public?",
                    },
                },
            )
        return httpx.Response(
            202,
            json={
                "id": "session-1",
                "status": "running",
                "stage": "analyzing",
            },
        )

    client = PiAgentClient(
        base_url="http://pi-agent:3210",
        api_token="service-secret",
        transport=httpx.MockTransport(handler),
    )

    started = await client.start_session(
        review_input(),
        skill="code-review",
        profile="default",
        provider="openai",
        model="gpt-5.4",
        thinking_level="high",
    )
    waiting = await client.get_session(started.id)
    resumed = await client.send_message(
        started.id,
        "Yes, it is intentional.",
        delivery="answer",
    )

    assert started.status == PiAgentSessionStatus.running
    assert waiting.status == PiAgentSessionStatus.waiting_for_input
    assert waiting.pending_input is not None
    assert resumed.status == PiAgentSessionStatus.running
    assert requests[0].headers["Authorization"] == "Bearer service-secret"
    assert json.loads(requests[2].content) == {
        "message": "Yes, it is intentional.",
        "delivery": "answer",
    }


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
