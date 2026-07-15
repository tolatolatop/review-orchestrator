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
    preparing = "preparing"
    running = "running"
    waiting_for_input = "waiting_for_input"
    validating_result = "validating_result"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


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


class AgentPresetResourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)


class AgentDomainPresetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, min_length=1, max_length=64)
    id: str | None = Field(default=None, min_length=1, max_length=128)
    thinking_level: Literal["minimal", "low", "medium", "high", "xhigh"] | None = (
        None
    )


class AgentDomainPresetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_turns: int | None = Field(
        default=None, gt=0, validation_alias="max_turns", serialization_alias="maxTurns"
    )
    max_tool_calls: int | None = Field(
        default=None,
        gt=0,
        validation_alias="max_tool_calls",
        serialization_alias="maxToolCalls",
    )
    max_result_bytes: int | None = Field(
        default=None,
        gt=0,
        validation_alias="max_result_bytes",
        serialization_alias="maxResultBytes",
    )


class AgentDomainPresetOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: AgentDomainPresetModel | None = None
    tools: list[str] | None = None
    limits: AgentDomainPresetLimits | None = None


class AgentDomainPreset(BaseModel):
    """Only domain-owned selectors accepted by the thin Runtime."""

    agent_id: str = Field(min_length=1, max_length=128)
    task_type: str = Field(min_length=1, max_length=128)
    repository_skills: list[str] = Field(default_factory=list)
    # These fields are deliberately excluded from the legacy three-selector
    # model_dump contract; PiAgentClient serializes them into trusted nested
    # request fields explicitly.
    resource: AgentPresetResourceReference | None = Field(default=None, exclude=True)
    overrides: AgentDomainPresetOverrides | None = Field(default=None, exclude=True)


class PiAgentRuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    at: str
    type: str
    stage: str
    tool: str | None = None


class PiAgentSession(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["review", "instruction", "agent"] | None = None
    agent_id: str | None = None
    agent_version: str | None = None
    title: str | None = None
    status: PiAgentSessionStatus
    stage: str
    workspace_path: str | None = None
    provider: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    skills: list[str] = Field(default_factory=list)
    skill_digests: dict[str, str] = Field(default_factory=dict)
    profile: str | None = None
    tools: list[str] = Field(default_factory=list)
    execution_limits: dict[str, int] = Field(default_factory=dict)
    execution_counters: dict[str, int] = Field(default_factory=dict)
    resolved_preset: dict[str, Any] | None = None
    execution_environment: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    session_archive: dict[str, Any] | None = None
    error: str | None = None
    session_file: str | None = None
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
        preset: AgentDomainPreset,
    ) -> PiAgentSession:
        return await self.start_agent_session(
            preset=preset,
            workspace_path=review_input.workspace_path,
            input_data=review_input.model_dump(),
            title=(
                f"Review PR #{review_input.pr_number}: {review_input.repo_full_name}"
            ),
        )

    async def start_instruction_session(
        self,
        instruction: AgentInstructionInput,
        *,
        preset: AgentDomainPreset,
    ) -> PiAgentSession:
        instruction_payload: dict[str, Any] = {
            "text": instruction.text,
            "author_login": instruction.author_login,
            "history": [item.model_dump() for item in instruction.history],
        }
        if instruction.source_url:
            instruction_payload["source_url"] = instruction.source_url
        return await self.start_agent_session(
            preset=preset,
            workspace_path=instruction.workspace_path,
            input_data={
                "repository_context": instruction.repository_context.model_dump(),
                "instruction": instruction_payload,
            },
            idempotency_key=instruction.idempotency_key,
            title=(
                f"PR #{instruction.repository_context.pr_number} command from "
                f"{instruction.author_login}"
            ),
        )

    async def start_agent_session(
        self,
        *,
        preset: AgentDomainPreset,
        workspace_path: str,
        input_data: dict[str, Any],
        idempotency_key: str | None = None,
        title: str | None = None,
    ) -> PiAgentSession:
        response = await self._request(
            "POST",
            "/v1/sessions",
            json=self._agent_start_payload(
                preset=preset,
                workspace_path=workspace_path,
                input_data=input_data,
                idempotency_key=idempotency_key,
                title=title,
            ),
        )
        return PiAgentSession.model_validate(response)

    async def get_session(self, session_id: str) -> PiAgentSession:
        response = await self._request("GET", f"/v1/sessions/{session_id}")
        return PiAgentSession.model_validate(response)

    async def cancel_session(self, session_id: str) -> PiAgentSession:
        response = await self._request("DELETE", f"/v1/sessions/{session_id}")
        return PiAgentSession.model_validate(response)

    def _start_payload(
        self,
        review_input: ReviewSkillInput,
        *,
        preset: AgentDomainPreset,
    ) -> dict[str, Any]:
        return self._agent_start_payload(
            preset=preset,
            workspace_path=review_input.workspace_path,
            input_data=review_input.model_dump(),
            title=(
                f"Review PR #{review_input.pr_number}: {review_input.repo_full_name}"
            ),
        )

    def _instruction_start_payload(
        self,
        instruction: AgentInstructionInput,
        *,
        preset: AgentDomainPreset,
    ) -> dict[str, Any]:
        instruction_payload: dict[str, Any] = {
            "text": instruction.text,
            "author_login": instruction.author_login,
            "history": [item.model_dump() for item in instruction.history],
        }
        if instruction.source_url:
            instruction_payload["source_url"] = instruction.source_url
        return self._agent_start_payload(
            preset=preset,
            workspace_path=instruction.workspace_path,
            input_data={
                "repository_context": instruction.repository_context.model_dump(),
                "instruction": instruction_payload,
            },
            idempotency_key=instruction.idempotency_key,
            title=(
                f"PR #{instruction.repository_context.pr_number} command from "
                f"{instruction.author_login}"
            ),
        )

    def _agent_start_payload(
        self,
        *,
        preset: AgentDomainPreset,
        workspace_path: str,
        input_data: dict[str, Any],
        idempotency_key: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_id": preset.agent_id,
            "task_type": preset.task_type,
            "repository_skills": preset.repository_skills,
            "workspace_path": workspace_path,
            "input": input_data,
        }
        if preset.resource is not None:
            payload["preset_resource"] = preset.resource.model_dump()
        if preset.overrides is not None:
            payload["preset_overrides"] = preset.overrides.model_dump(
                by_alias=True,
                exclude_none=True,
            )
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if title:
            payload["title"] = title
        return payload

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
