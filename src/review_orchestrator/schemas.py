from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ReviewRunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    superseded = "superseded"


class ReviewRunCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    repo_full_name: str = Field(min_length=1, max_length=512)
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=7, max_length=80)
    base_sha: str | None = Field(default=None, max_length=80)
    force: bool = False


class ReviewRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    repo_full_name: str
    pull_request_number: int
    base_sha: str | None
    head_sha: str
    attempt: int
    trigger_type: str
    status: ReviewRunStatus
    stage: str | None
    summary_comment_id: str | None
    workspace_path: str | None
    review_summary: str | None
    review_conclusion: str | None
    risk_level: str | None
    finding_count_total: int
    finding_count_by_severity: dict | None
    failure_code: str | None
    error: str | None
    lock_owner: str | None
    locked_until: datetime | None
    superseded_by_review_run_id: str | None
    soft_timeout_emitted_at: datetime | None
    hard_timeout_emitted_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    deadline_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WebhookAccepted(BaseModel):
    accepted: bool = True
    provider: str
    delivery_id: str | None = None
    status: str = "received"
    internal_event: str | None = None
    review_run_id: str | None = None
    agent_task_id: str | None = None
    duplicate: bool = False


class ReviewRunActionResult(BaseModel):
    review_run_id: str
    status: ReviewRunStatus
