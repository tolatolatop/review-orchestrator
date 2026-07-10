from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from review_orchestrator.observability import ObservabilityListEnvelope
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
    openhands_start_task_id: str | None
    openhands_conversation_id: str | None
    openhands_sandbox_id: str | None
    openhands_agent_server_url: str | None
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


class ReviewRunOperationalState(BaseModel):
    lock_state: str
    timeout_state: str
    worker_state: str


class ReviewRunProviderPublishing(BaseModel):
    summary_comment_id: str | None = None
    summary_comment_ref_id: str | None = None
    summary_comment_status: str | None = None
    summary_published: bool = False
    line_comment_count: int = 0
    line_comment_status_counts: dict[str, int] = Field(default_factory=dict)


class ReviewRunListItem(ReviewRunRead):
    operational_state: ReviewRunOperationalState
    provider_publishing: ReviewRunProviderPublishing


class ReviewRunListResponse(BaseModel):
    items: list[ReviewRunListItem]
    total: int
    limit: int
    offset: int


class ReviewRunPullRequestContext(BaseModel):
    id: str | None = None
    title: str | None = None
    author_login: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    head_ref: str | None = None
    head_sha: str | None = None
    head_repo_full_name: str | None = None
    is_fork: bool | None = None
    status: str | None = None
    html_url: str | None = None
    latest_event_id: str | None = None
    closed_at: datetime | None = None
    merged_at: datetime | None = None


class ReviewRunWorkspaceSummary(BaseModel):
    workspace_id: str | None = None
    workspace_path: str | None = None
    status: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    ready_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class ReviewRunSessionSummary(BaseModel):
    id: str
    status: str
    openhands_conversation_id: str | None
    skill_name: str | None
    profile_name: str | None
    result_ref: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ReviewRunFindingsSummary(BaseModel):
    total: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_state: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)


class ReviewRunLinkedEventSummary(BaseModel):
    id: str
    provider_event: str
    provider_action: str | None
    internal_event: str | None
    delivery_id: str
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    processed_at: datetime | None


class ReviewRunLinkedTaskSummary(BaseModel):
    id: str
    provider_event_id: str | None
    task_type: str
    status: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ReviewRunDetail(ReviewRunListItem):
    pull_request_context: ReviewRunPullRequestContext | None = None
    workspace: ReviewRunWorkspaceSummary | None = None
    review_session: ReviewRunSessionSummary | None = None
    findings_summary: ReviewRunFindingsSummary
    validation_warnings: list = Field(default_factory=list)
    validation_errors: list = Field(default_factory=list)
    trigger_event: ReviewRunLinkedEventSummary | None = None
    agent_task: ReviewRunLinkedTaskSummary | None = None


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
    agent_task_id: str | None = None
    duplicate: bool = False


class ProviderEventInboxSummary(BaseModel):
    id: str
    provider: str
    delivery_id: str
    provider_event: str
    provider_action: str | None
    internal_event: str | None
    status: str
    repo_full_name: str | None
    pull_request_number: int | None
    head_sha: str | None
    payload_digest: str
    coalesce_key: str | None
    review_run_id: str | None
    agent_task_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    processed_at: datetime | None


class ProviderEventInboxListResponse(ObservabilityListEnvelope):
    items: list[ProviderEventInboxSummary]


class ProviderEventInboxDetail(ProviderEventInboxSummary):
    dedupe_key: str
    payload: dict | None = None


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
