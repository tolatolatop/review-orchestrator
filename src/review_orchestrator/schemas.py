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
    summary_comment_id: str | None
    workspace_path: str | None
    review_summary: str | None
    review_conclusion: str | None
    risk_level: str | None
    finding_count_total: int
    finding_count_by_severity: dict | None
    failure_code: str | None
    error: str | None
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
    duplicate: bool = False


class ReviewRunActionResult(BaseModel):
    review_run_id: str
    status: ReviewRunStatus


class WorkspaceStatus(StrEnum):
    preparing = "preparing"
    ready = "ready"
    leased = "leased"
    idle = "idle"
    cleaning = "cleaning"
    deleted = "deleted"
    failed = "failed"


class WorkspaceRepository(BaseModel):
    full_name: str = Field(min_length=1, max_length=512)
    clone_url: str = Field(min_length=1, max_length=2048)


class WorkspacePullRequest(BaseModel):
    number: int = Field(gt=0)
    base_sha: str = Field(min_length=7, max_length=80)
    head_sha: str = Field(min_length=7, max_length=80)
    is_fork: bool = False


class WorkspaceAuth(BaseModel):
    token_ref: str | None = Field(default=None, max_length=255)


class WorkspacePrepareOptions(BaseModel):
    use_git_cache: bool = True
    force_refresh: bool = False
    enable_submodules: bool = False
    enable_lfs: bool = False


class WorkspacePrepareRequest(BaseModel):
    provider: str = Field(default="github", min_length=1, max_length=64)
    repository: WorkspaceRepository
    pull_request: WorkspacePullRequest
    auth: WorkspaceAuth | None = None
    options: WorkspacePrepareOptions = Field(default_factory=WorkspacePrepareOptions)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workspace_id: str
    provider: str
    repository: str
    repository_clone_url: str
    repo_hash: str
    pull_request_number: int
    base_sha: str
    head_sha: str
    workspace_path: str
    cache_path: str | None
    status: WorkspaceStatus
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    ready_at: datetime | None
    last_used_at: datetime | None
    expires_at: datetime | None


class WorkspacePrepareResponse(BaseModel):
    workspace_id: str
    workspace_path: str
    base_sha: str
    head_sha: str
    status: WorkspaceStatus
    from_cache: bool = False
    failure_code: str | None = None
    failure_message: str | None = None


class WorkspaceLeaseRequest(BaseModel):
    review_run_id: str | None = Field(default=None, max_length=36)
    session_id: str | None = Field(default=None, max_length=128)


class WorkspaceLeaseRead(BaseModel):
    lease_id: str
    workspace_id: str
    workspace_path: str
    status: WorkspaceStatus


class WorkspaceCleanupRequest(BaseModel):
    force: bool = False


class PullRequestWorkspaceCleanupRequest(BaseModel):
    provider: str = Field(default="github", min_length=1, max_length=64)
    repository: str = Field(min_length=1, max_length=512)
    pull_request_number: int = Field(gt=0)
    force: bool = False


class CleanupSummary(BaseModel):
    deleted: int = 0
    skipped_locked: int = 0
    failed: int = 0
