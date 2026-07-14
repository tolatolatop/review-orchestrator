import json

import httpx
import pytest

from review_orchestrator.config import Settings
from review_orchestrator.pi_agent import (
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
