from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from review_orchestrator.review_results import ChangedFile, ParsedReviewResult


class ReviewRunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    superseded = "superseded"


class ReviewRunCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    repository: str = Field(min_length=1, max_length=512)
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=7, max_length=80)
    base_sha: str | None = Field(default=None, max_length=80)


class ReviewRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    repository: str
    pull_request_number: int
    base_sha: str | None
    head_sha: str
    status: ReviewRunStatus
    summary_comment_id: str | None
    workspace_path: str | None
    openhands_start_task_id: str | None
    openhands_conversation_id: str | None
    openhands_sandbox_id: str | None
    openhands_agent_server_url: str | None
    result_summary: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class ReviewSessionStart(BaseModel):
    workspace_path: str | None = Field(default=None, min_length=1)


class ReviewSessionCancel(BaseModel):
    reason: str = Field(default="cancelled", min_length=1, max_length=1000)


class ReviewResultCollect(BaseModel):
    raw_output: str | dict
    changed_files: list[ChangedFile] = Field(default_factory=list)


class ReviewResultCollectResponse(BaseModel):
    review_run: ReviewRunRead
    parsed: ParsedReviewResult


class WebhookAccepted(BaseModel):
    accepted: bool = True
    provider: str
    delivery_id: str | None = None
    status: str = "received"
    internal_event: str | None = None
    review_run_id: str | None = None
    duplicate: bool = False
