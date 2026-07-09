from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from review_orchestrator.review_results import ReviewSkillInput


class OpenHandsClientError(RuntimeError):
    pass


class OpenHandsStartTaskStatus(StrEnum):
    working = "WORKING"
    waiting_for_sandbox = "WAITING_FOR_SANDBOX"
    preparing_repository = "PREPARING_REPOSITORY"
    running_setup_script = "RUNNING_SETUP_SCRIPT"
    setting_up_git_hooks = "SETTING_UP_GIT_HOOKS"
    setting_up_skills = "SETTING_UP_SKILLS"
    starting_conversation = "STARTING_CONVERSATION"
    ready = "READY"
    error = "ERROR"


class OpenHandsStartTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    status: OpenHandsStartTaskStatus
    detail: str | None = None
    app_conversation_id: str | None = None
    sandbox_id: str | None = None
    agent_server_url: str | None = None


class OpenHandsConversation(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    sandbox_status: str | None = None
    execution_status: str | None = None


class OpenHandsEventPage(BaseModel):
    model_config = ConfigDict(extra="allow")

    items: list[dict[str, Any]] = Field(default_factory=list)
    next_page_id: str | None = None


class OpenHandsClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def start_conversation(
        self,
        review_input: ReviewSkillInput,
    ) -> OpenHandsStartTask:
        response = await self._request(
            "POST",
            "/api/v1/app-conversations",
            json=self._start_payload(review_input),
        )
        return OpenHandsStartTask.model_validate(response)

    async def get_start_task(self, task_id: str) -> OpenHandsStartTask:
        response = await self._request(
            "GET",
            "/api/v1/app-conversations/start-tasks",
            params={"ids": task_id},
        )
        task = _first_item(response)
        if task is None:
            raise OpenHandsClientError(f"OpenHands start task not found: {task_id}")
        return OpenHandsStartTask.model_validate(task)

    async def get_conversation(self, conversation_id: str) -> OpenHandsConversation:
        response = await self._request(
            "GET",
            "/api/v1/app-conversations",
            params={"ids": conversation_id},
        )
        conversation = _first_item(response)
        if conversation is None:
            raise OpenHandsClientError(
                f"OpenHands conversation not found: {conversation_id}"
            )
        return OpenHandsConversation.model_validate(conversation)

    async def list_events(
        self,
        conversation_id: str,
        *,
        page_id: str | None = None,
        limit: int = 100,
    ) -> OpenHandsEventPage:
        params: dict[str, Any] = {"limit": limit}
        if page_id:
            params["page_id"] = page_id
        response = await self._request(
            "GET",
            f"/api/v1/conversation/{conversation_id}/events/search",
            params=params,
        )
        return OpenHandsEventPage.model_validate(response)

    async def delete_conversation(self, conversation_id: str) -> None:
        await self._request(
            "DELETE",
            f"/api/v1/app-conversations/{conversation_id}",
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=headers,
            ) as client:
                response = await client.request(method, path, json=json, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenHandsClientError(
                "OpenHands request failed "
                f"({exc.response.status_code} {method} {path}): "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.RequestError as exc:
            raise OpenHandsClientError(
                f"OpenHands request failed ({method} {path}): {exc}"
            ) from exc

        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _start_payload(self, review_input: ReviewSkillInput) -> dict[str, Any]:
        prompt_input = review_input.model_copy(
            update={"workspace_path": _openhands_workspace_path(review_input)}
        )
        prompt = json.dumps(prompt_input.model_dump(), ensure_ascii=False, indent=2)
        return {
            "title": (
                f"Review PR #{review_input.pr_number}: {review_input.repo_full_name}"
            ),
            "trigger": "automation",
            "selected_repository": review_input.repo_full_name,
            "pr_number": [review_input.pr_number],
            "initial_message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Run a pull request review for this commit range. "
                            "Inspect the local workspace and return only the "
                            "configured JSON review result.\n\n"
                            f"{prompt}"
                        ),
                    }
                ],
            },
            "system_message_suffix": (
                "You are running inside an automated PR review session. "
                "Use the workspace path and base/head commit range from the "
                "user message. If the workspace path is unavailable, use the "
                "current repository checkout. Do not publish to GitHub. Return "
                "only JSON with `summary` and `findings` matching the Review "
                "Orchestrator schema."
            ),
        }


def _openhands_workspace_path(review_input: ReviewSkillInput) -> str:
    repo_name = review_input.repo_full_name.rsplit("/", 1)[-1]
    return f"/workspace/project/{repo_name}"


def _first_item(response: Any) -> dict[str, Any] | None:
    if isinstance(response, list):
        return response[0] if response else None
    if isinstance(response, dict):
        items = response.get("items")
        if isinstance(items, list):
            return items[0] if items else None
    return None
