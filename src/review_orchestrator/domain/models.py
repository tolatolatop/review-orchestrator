"""Persistent review domain entities."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from review_orchestrator.infrastructure.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class PullRequestContext(Base):
    __tablename__ = "pull_request_context"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repo_full_name",
            "pull_request_number",
            name="uq_pull_request_context",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repo_full_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    provider_repo_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pull_request_number: Mapped[int] = mapped_column(nullable=False, index=True)
    provider_pr_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    head_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    head_sha: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    head_repo_full_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_fork: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    html_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_comment_provider_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    latest_review_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    latest_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ProviderEventInbox(Base):
    __tablename__ = "provider_event_inbox"
    __table_args__ = (
        UniqueConstraint("provider", "delivery_id", name="uq_provider_delivery"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_event: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_action: Mapped[str | None] = mapped_column(String(128), nullable=True)
    internal_event: Mapped[str | None] = mapped_column(String(128), nullable=True)
    repo_full_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    pull_request_number: Mapped[int | None] = mapped_column(nullable=True, index=True)
    head_sha: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    coalesce_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Task(Base):
    """Unified durable task control plane.

    Domain-specific task types use joined-table inheritance. Scheduling,
    leasing and lifecycle state live here so workers do not maintain separate
    queue implementations for reviews and message commands.
    """

    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint("priority >= 0 AND priority <= 100", name="ck_task_priority"),
        CheckConstraint(
            "effective_priority >= 0 AND effective_priority <= 100",
            name="ck_task_effective_priority",
        ),
        UniqueConstraint("dedupe_key", name="uq_task_dedupe_key"),
        Index(
            "ix_task_claim",
            "status",
            "queue",
            "effective_priority",
            "available_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    capability_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default="generic"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    execution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    delivery_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required"
    )
    queue: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    priority: Mapped[int] = mapped_column(nullable=False, default=0)
    effective_priority: Mapped[int] = mapped_column(nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    concurrency_key: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    resource_class: Mapped[str] = mapped_column(
        String(64), nullable=False, default="agent-standard", index=True
    )
    resource_context_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolved_preset_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=2)
    lock_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __mapper_args__ = {
        "polymorphic_on": kind,
        "polymorphic_identity": "task",
        "with_polymorphic": "*",
    }


class TaskAttempt(Base):
    __tablename__ = "task_attempt"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no", name="uq_task_attempt"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_preset_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionArchive(Base):
    __tablename__ = "session_archive"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "agent_run_id", name="uq_session_archive_agent_run"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_attempt.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    session_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    task_metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    workspace_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_diff_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    redaction_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ResourcePool(Base):
    __tablename__ = "resource_pool"

    resource_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capacity: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AgentPreset(Base):
    """Database-backed, operator-managed pi-agent preset selection.

    A preset owns only the runtime selectors and bounded execution overrides.
    Agent code, schemas, prompts, versions, and Tool implementations remain
    installed Runtime resources.
    """

    __tablename__ = "agent_preset"
    __table_args__ = (
        UniqueConstraint("name", name="uq_agent_preset_name"),
        UniqueConstraint(
            "task_kind",
            "scope_key",
            name="uq_agent_preset_task_scope",
        ),
        CheckConstraint("revision > 0", name="ck_agent_preset_revision"),
        Index("ix_agent_preset_resolution", "task_kind", "scope_key", "enabled"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(768), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repo_full_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(128), nullable=False)
    repository_skills_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    model_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tools_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    limits_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ResourceLease(Base):
    __tablename__ = "resource_lease"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "stage", "resource_key", name="uq_task_resource_lease"
        ),
        Index("ix_resource_lease_active", "resource_key", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_key: Mapped[str] = mapped_column(
        ForeignKey("resource_pool.resource_key", ondelete="CASCADE"),
        nullable=False,
    )
    units: Mapped[int] = mapped_column(nullable=False, default=1)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DeliveryOutbox(Base):
    __tablename__ = "delivery_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_delivery_idempotency_key"),
        Index(
            "ix_delivery_claim",
            "status",
            "queue",
            "priority",
            "available_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    destination_key: Mapped[str] = mapped_column(
        String(1024), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    queue: Mapped[str] = mapped_column(
        String(64), nullable=False, default="provider-delivery"
    )
    priority: Mapped[int] = mapped_column(nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    attempt: Mapped[int] = mapped_column(nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=5)
    lock_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ReviewRun(Task):
    __tablename__ = "review_run"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repo_full_name",
            "pull_request_number",
            "head_sha",
            "attempt",
            name="uq_review_run_head",
        ),
    )

    id: Mapped[str] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), primary_key=True
    )
    pull_request_context_id: Mapped[str | None] = mapped_column(
        ForeignKey("pull_request_context.id"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repo_full_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    pull_request_number: Mapped[int] = mapped_column(nullable=False, index=True)
    base_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    head_sha: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    trigger_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual"
    )
    trigger_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_event_inbox.id"), nullable=True, index=True
    )
    summary_comment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_session_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    agent_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_thinking_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    review_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_conclusion: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finding_count_total: Mapped[int] = mapped_column(nullable=False, default=0)
    finding_count_by_severity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    validation_errors_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by_review_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    soft_timeout_emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hard_timeout_emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __mapper_args__ = {"polymorphic_identity": "review"}


class ReviewSession(Base):
    __tablename__ = "review_session"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    review_run_id: Mapped[str] = mapped_column(
        ForeignKey("review_run.id"), nullable=False, unique=True, index=True
    )
    agent_session_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    skill_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Finding(Base):
    __tablename__ = "finding"
    __table_args__ = (
        UniqueConstraint("review_run_id", "fingerprint", name="uq_finding_run"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    review_run_id: Mapped[str] = mapped_column(
        ForeignKey("review_run.id"), nullable=False, index=True
    )
    pull_request_context_id: Mapped[str | None] = mapped_column(
        ForeignKey("pull_request_context.id"), nullable=True, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_start: Mapped[int | None] = mapped_column(nullable=True)
    line_end: Mapped[int | None] = mapped_column(nullable=True)
    diff_hunk_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    first_seen_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    last_seen_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    resolved_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    raw_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ReviewCommentRef(Base):
    __tablename__ = "review_comment_ref"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repo_full_name",
            "pull_request_number",
            "provider_comment_id",
            name="uq_provider_comment",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repo_full_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    pull_request_number: Mapped[int] = mapped_column(nullable=False, index=True)
    review_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("review_run.id"), nullable=True, index=True
    )
    finding_id: Mapped[str | None] = mapped_column(
        ForeignKey("finding.id"), nullable=True, index=True
    )
    comment_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_comment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_published_body_hash: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class RetryJob(Base):
    __tablename__ = "retry_job"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    review_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("review_run.id"), nullable=True, index=True
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=2)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AgentTask(Task):
    __tablename__ = "agent_task"

    id: Mapped[str] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), primary_key=True
    )
    provider_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_event_inbox.id"), nullable=True, index=True
    )
    pull_request_context_id: Mapped[str | None] = mapped_column(
        ForeignKey("pull_request_context.id"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repo_full_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    pull_request_number: Mapped[int] = mapped_column(nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="mention"
    )
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_comment_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_author_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    command_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_comment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_body_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    publish_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_session_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    agent_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_thinking_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    agent_start_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    soft_timeout_emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hard_timeout_emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    __mapper_args__ = {"polymorphic_identity": "agent"}


class ReviewConfig(Base):
    __tablename__ = "review_config"
    __table_args__ = (
        UniqueConstraint("provider", "repo_full_name", name="uq_review_config_repo"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_full_name: Mapped[str] = mapped_column(String(512), nullable=False)
    review_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    line_comments_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    min_severity_for_summary: Mapped[str] = mapped_column(
        String(32), nullable=False, default="info"
    )
    max_findings_per_run: Mapped[int] = mapped_column(nullable=False, default=50)
    large_pr_file_limit: Mapped[int] = mapped_column(nullable=False, default=100)
    large_pr_patch_bytes_limit: Mapped[int] = mapped_column(
        nullable=False, default=500000
    )
    auto_retry_invalid_agent_result: Mapped[bool] = mapped_column(
        nullable=False, default=False
    )
    auto_retry_infra_failure: Mapped[bool] = mapped_column(nullable=False, default=True)
    default_review_skill: Mapped[str] = mapped_column(
        String(128), nullable=False, default="code-review"
    )
    agent_commands_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    default_agent_command_skill: Mapped[str] = mapped_column(
        String(128), nullable=False, default="pr-assistant"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Workspace(Base):
    __tablename__ = "workspace"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repository",
            "pull_request_number",
            "head_sha",
            name="uq_workspace_head",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    workspace_id: Mapped[str] = mapped_column(
        String(768),
        nullable=False,
        unique=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repository: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    repository_clone_url: Mapped[str] = mapped_column(Text, nullable=False)
    repo_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pull_request_number: Mapped[int] = mapped_column(nullable=False, index=True)
    base_sha: Mapped[str] = mapped_column(String(80), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    cache_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="preparing")
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class WorkspaceLease(Base):
    __tablename__ = "workspace_lease"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspace.workspace_id"),
        nullable=False,
        index=True,
    )
    review_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    leased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
