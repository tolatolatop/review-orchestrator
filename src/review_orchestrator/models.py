from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from review_orchestrator.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class PullRequestContext(Base):
    __tablename__ = "pull_request_context"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repository",
            "pull_request_number",
            name="uq_pull_request_context",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repository: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
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
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_event: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_action: Mapped[str | None] = mapped_column(String(128), nullable=True)
    internal_event: Mapped[str | None] = mapped_column(String(128), nullable=True)
    repository: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    pull_request_number: Mapped[int | None] = mapped_column(nullable=True, index=True)
    head_sha: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
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
            "repository",
            "pull_request_number",
            "head_sha",
            name="uq_review_run_head",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repository: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    pull_request_number: Mapped[int] = mapped_column(nullable=False, index=True)
    base_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    head_sha: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    summary_comment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
