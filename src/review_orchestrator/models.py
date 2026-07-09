from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, Text, UniqueConstraint
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

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repository: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    pull_request_number: Mapped[int] = mapped_column(nullable=False, index=True)
    base_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    head_sha: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
