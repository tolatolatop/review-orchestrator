"""Client and contracts for the isolated pi-agent runtime."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from review_orchestrator.domain.review_results import ReviewSkillInput


class PiAgentClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def infrastructure_failure(self) -> bool:
        return self.status_code is None or self.status_code >= 500


class PiAgentSessionStatus(StrEnum):
    starting = "starting"
    running = "running"
    waiting_for_input = "waiting_for_input"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class PiAgentPendingInput(BaseModel):
    id: str
    question: str
    choices: list[str] | None = None


class AgentInstructionRepositoryContext(BaseModel):
    provider: str
    repo_full_name: str
    pr_number: int = Field(gt=0)
    base_sha: str
    head_sha: str


class AgentInstructionHistoryItem(BaseModel):
    author_login: str
    command: str
    answer: str
    outcome: Literal["answered", "needs_clarification", "refused"]
    head_sha: str


class AgentInstructionInput(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    workspace_path: str = Field(min_length=1)
    repository_context: AgentInstructionRepositoryContext
    text: str = Field(min_length=1, max_length=8000)
    author_login: str = Field(min_length=1, max_length=255)
    source_url: str | None = None
    history: list[AgentInstructionHistoryItem] = Field(
        default_factory=list,
        max_length=6,
    )


class AgentTaskReference(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_line_order(self) -> AgentTaskReference:
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class AgentTaskResult(BaseModel):
    outcome: Literal["answered", "needs_clarification", "refused"]
    answer: str = Field(min_length=1, max_length=30000)
    references: list[AgentTaskReference] = Field(default_factory=list, max_length=50)


class PiAgentRuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    at: str
    type: str
    stage: str
    tool: str | None = None


class PiAgentSession(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["review", "instruction"] | None = None
    title: str | None = None
    status: PiAgentSessionStatus
    stage: str
    workspace_path: str | None = None
    provider: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    skills: list[str] = Field(default_factory=list)
    profile: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    session_file: str | None = None
    pending_input: PiAgentPendingInput | None = None
    events: list[PiAgentRuntimeEvent] = Field(default_factory=list)


class PiAgentClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.transport = transport

    async def start_session(
        self,
        review_input: ReviewSkillInput,
        *,
        skill: str,
        profile: str,
        provider: str,
        model: str,
        thinking_level: str,
        model_base_url: str | None = None,
    ) -> PiAgentSession:
        response = await self._request(
            "POST",
            "/v1/sessions",
            json=self._start_payload(
                review_input,
                skill=skill,
                profile=profile,
                provider=provider,
                model=model,
                thinking_level=thinking_level,
                model_base_url=model_base_url,
            ),
        )
        return PiAgentSession.model_validate(response)

    async def start_instruction_session(
        self,
        instruction: AgentInstructionInput,
        *,
        skill: str,
        profile: str,
        provider: str,
        model: str,
        thinking_level: str,
        model_base_url: str | None = None,
    ) -> PiAgentSession:
        response = await self._request(
            "POST",
            "/v1/sessions",
            json=self._instruction_start_payload(
                instruction,
                skill=skill,
                profile=profile,
                provider=provider,
                model=model,
                thinking_level=thinking_level,
                model_base_url=model_base_url,
            ),
        )
        return PiAgentSession.model_validate(response)

    async def get_session(self, session_id: str) -> PiAgentSession:
        response = await self._request("GET", f"/v1/sessions/{session_id}")
        return PiAgentSession.model_validate(response)

    async def send_message(
        self,
        session_id: str,
        message: str,
        *,
        delivery: Literal["answer", "steer", "follow_up"] = "steer",
    ) -> PiAgentSession:
        response = await self._request(
            "POST",
            f"/v1/sessions/{session_id}/messages",
            json={"message": message, "delivery": delivery},
        )
        return PiAgentSession.model_validate(response)

    async def cancel_session(self, session_id: str) -> PiAgentSession:
        response = await self._request("DELETE", f"/v1/sessions/{session_id}")
        return PiAgentSession.model_validate(response)

    def _start_payload(
        self,
        review_input: ReviewSkillInput,
        *,
        skill: str,
        profile: str,
        provider: str,
        model: str,
        thinking_level: str,
        model_base_url: str | None,
    ) -> dict[str, Any]:
        model_config = {
            "provider": provider,
            "id": model,
            "thinking_level": thinking_level,
        }
        if model_base_url:
            model_config["base_url"] = model_base_url
        return {
            "title": (
                f"Review PR #{review_input.pr_number}: {review_input.repo_full_name}"
            ),
            "workspace_path": review_input.workspace_path,
            "review": review_input.model_dump(),
            "model": model_config,
            "skills": [skill],
            "profile": profile,
        }

    def _instruction_start_payload(
        self,
        instruction: AgentInstructionInput,
        *,
        skill: str,
        profile: str,
        provider: str,
        model: str,
        thinking_level: str,
        model_base_url: str | None,
    ) -> dict[str, Any]:
        model_config = {
            "provider": provider,
            "id": model,
            "thinking_level": thinking_level,
        }
        if model_base_url:
            model_config["base_url"] = model_base_url
        instruction_payload: dict[str, Any] = {
            "text": instruction.text,
            "author_login": instruction.author_login,
            "history": [item.model_dump() for item in instruction.history],
        }
        if instruction.source_url:
            instruction_payload["source_url"] = instruction.source_url
        return {
            "kind": "instruction",
            "idempotency_key": instruction.idempotency_key,
            "title": (
                f"PR #{instruction.repository_context.pr_number} command from "
                f"{instruction.author_login}"
            ),
            "workspace_path": instruction.workspace_path,
            "repository_context": instruction.repository_context.model_dump(),
            "instruction": instruction_payload,
            "model": model_config,
            "skills": [skill],
            "profile": profile,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=headers,
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, json=json)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise PiAgentClientError(
                "pi-agent runtime request failed "
                f"({exc.response.status_code} {method} {path}): "
                f"{exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise PiAgentClientError(
                f"pi-agent runtime request failed ({method} {path}): {exc}"
            ) from exc

        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PiAgentClientError(
                f"pi-agent runtime returned invalid JSON ({method} {path}).",
                status_code=response.status_code,
            ) from exc
