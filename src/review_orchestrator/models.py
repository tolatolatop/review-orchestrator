from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from review_orchestrator.db import Base


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


class ReviewRun(Base):
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
        String(36), primary_key=True, default=lambda: str(uuid4())
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
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    summary_comment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ReviewSession(Base):
    __tablename__ = "review_session"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    review_run_id: Mapped[str] = mapped_column(
        ForeignKey("review_run.id"), nullable=False, unique=True, index=True
    )
    openhands_conversation_id: Mapped[str | None] = mapped_column(
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
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_start: Mapped[int | None] = mapped_column(nullable=True)
    line_end: Mapped[int | None] = mapped_column(nullable=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
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
    default_review_profile: Mapped[str] = mapped_column(
        String(128), nullable=False, default="default"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
