from pathlib import Path

import pytest

from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.models import Finding
from review_orchestrator.review_results import parse_review_result
from review_orchestrator.schemas import ReviewRunCreate
from review_orchestrator.services import create_review_run
from review_orchestrator.worker import (
    acquire_next_review_run,
    emit_timeout_event,
    release_review_run_lock,
)


@pytest.fixture
async def session_factory(tmp_path: Path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    engine = create_engine(settings)
    await init_models(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_worker_acquires_and_releases_review_run_lock(session_factory) -> None:
    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )

        acquired = await acquire_next_review_run(session, worker_id="worker-1")
        assert acquired is not None
        assert acquired.id == review_run.id
        assert acquired.status == "running"
        assert acquired.lock_owner == "worker-1"
        assert acquired.locked_until is not None

        released = await release_review_run_lock(session, acquired.id)
        assert released is not None
        assert released.lock_owner is None
        assert released.locked_until is None


async def test_timeout_events_are_emitted_once(session_factory) -> None:
    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        await acquire_next_review_run(session, worker_id="worker-1")

        soft = await emit_timeout_event(session, review_run.id, timeout_kind="soft")
        duplicate_soft = await emit_timeout_event(
            session, review_run.id, timeout_kind="soft"
        )
        hard = await emit_timeout_event(session, review_run.id, timeout_kind="hard")

        assert soft is not None
        assert duplicate_soft is not None
        assert duplicate_soft.id == soft.id
        assert hard is not None
        assert hard.internal_event == "review_run.hard_timeout"

        refreshed = await session.get(type(review_run), review_run.id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.failure_code == "hard_timeout"


async def test_reconciliation_marks_new_existing_and_resolved(session_factory) -> None:
    from review_orchestrator.reconciliation import persist_and_reconcile_findings

    async with session_factory() as session:
        first_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        first_result = parse_review_result(
            {
                "summary": "Two findings.",
                "findings": [
                    {
                        "file": "src/auth.py",
                        "line": 42,
                        "severity": "high",
                        "message": "Token expiry is ignored.",
                        "confidence": 0.9,
                    },
                    {
                        "file": "src/api.py",
                        "line": 10,
                        "severity": "medium",
                        "message": "Error response lacks context.",
                        "confidence": 0.8,
                    },
                ],
            },
            provider="github",
            repo_full_name="example/repo",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        first_stats = await persist_and_reconcile_findings(
            session, first_run, first_result
        )
        first_run.status = "completed"
        session.add(first_run)
        await session.commit()

        second_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="c" * 40,
            ),
        )
        second_result = parse_review_result(
            {
                "summary": "Two findings.",
                "findings": [
                    {
                        "file": "src/auth.py",
                        "line": 42,
                        "severity": "high",
                        "message": "Token expiry is ignored.",
                        "confidence": 0.9,
                    },
                    {
                        "file": "src/cache.py",
                        "line": 25,
                        "severity": "low",
                        "message": "Cache timeout is undocumented.",
                        "confidence": 0.7,
                    },
                ],
            },
            provider="github",
            repo_full_name="example/repo",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

        second_stats = await persist_and_reconcile_findings(
            session, second_run, second_result
        )

    assert first_stats.new == 2
    assert second_stats.existing == 1
    assert second_stats.new == 1
    assert second_stats.resolved == 1


async def test_comment_refs_upsert_summary_and_dedupe_line_comments(
    session_factory,
) -> None:
    from review_orchestrator.comments import (
        build_summary_comment_body,
        ensure_line_comment_ref,
        upsert_summary_comment_ref,
    )

    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        body = build_summary_comment_body(
            review_run,
            status_text="completed",
            finding_stats={"new": 1},
        )
        summary_ref = await upsert_summary_comment_ref(
            session,
            review_run,
            provider_comment_id="summary-1",
            body=body,
        )
        updated_ref = await upsert_summary_comment_ref(
            session,
            review_run,
            provider_comment_id="summary-1",
            body=body + "\nupdated",
        )

        finding = Finding(
            review_run_id=review_run.id,
            fingerprint="finding-1",
            file_path="src/app.py",
            line_start=12,
            severity="high",
            message="Auth check is skipped.",
        )
        session.add(finding)
        await session.commit()
        await session.refresh(finding)

        first_line_ref, first_created = await ensure_line_comment_ref(
            session,
            review_run,
            finding,
            provider_comment_id="line-1",
            body="line body",
        )
        second_line_ref, second_created = await ensure_line_comment_ref(
            session,
            review_run,
            finding,
            provider_comment_id="line-2",
            body="line body",
        )

    assert updated_ref.id == summary_ref.id
    assert first_created is True
    assert second_created is False
    assert second_line_ref.id == first_line_ref.id
