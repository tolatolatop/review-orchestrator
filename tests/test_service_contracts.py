from datetime import UTC, datetime, timedelta

import pytest

from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import github_pull_request_snapshot
from review_orchestrator.gitlab import gitlab_pull_request_snapshot
from review_orchestrator.models import ReviewCommentRef, ReviewRun
from review_orchestrator.services import (
    _agent_task_filters,
    _build_coalesce_key,
    _finding_count_by_severity,
    _lock_state,
    _provider_event_filters,
    _provider_publishing_from_refs,
    _review_run_filters,
    _safe_error_message,
    _timeout_state,
    _worker_state,
    get_or_create_review_config,
)


def make_review_run(**overrides) -> ReviewRun:
    values = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "head_sha": "b" * 40,
        "status": "queued",
    }
    values.update(overrides)
    return ReviewRun(**values)


@pytest.mark.parametrize(
    ("run", "lock_state", "timeout_state", "worker_state"),
    [
        (make_review_run(), "unlocked", "none", "waiting_for_worker"),
        (
            make_review_run(
                status="running",
                lock_owner="worker",
                locked_until=datetime.now(UTC) + timedelta(minutes=1),
            ),
            "locked",
            "none",
            "locked_by_worker",
        ),
        (
            make_review_run(
                status="running",
                lock_owner="worker",
                locked_until=datetime.now(UTC) - timedelta(minutes=1),
            ),
            "expired",
            "none",
            "worker_lock_expired",
        ),
        (
            make_review_run(
                status="running",
                deadline_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
            "unlocked",
            "deadline_elapsed",
            "waiting_for_agent",
        ),
        (
            make_review_run(
                status="running",
                agent_session_id="session-1",
                soft_timeout_emitted_at=datetime.now(UTC),
            ),
            "unlocked",
            "soft_timeout",
            "running_in_pi_agent",
        ),
        (
            make_review_run(
                status="failed",
                hard_timeout_emitted_at=datetime.now(UTC),
            ),
            "unlocked",
            "hard_timeout",
            "terminal",
        ),
    ],
)
def test_review_run_operational_state_matrix(
    run: ReviewRun,
    lock_state: str,
    timeout_state: str,
    worker_state: str,
) -> None:
    assert _lock_state(run) == lock_state
    assert _timeout_state(run) == timeout_state
    assert _worker_state(run) == worker_state


def test_naive_utc_deadlines_are_supported() -> None:
    run = make_review_run(
        deadline_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
    )
    assert _timeout_state(run) == "deadline_elapsed"


def test_provider_publishing_summary_and_line_counts() -> None:
    refs = [
        ReviewCommentRef(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            comment_type="summary",
            provider_comment_id="summary-1",
            status="active",
        ),
        ReviewCommentRef(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            comment_type="line",
            provider_comment_id="line-1",
            status="active",
        ),
        ReviewCommentRef(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            comment_type="line",
            provider_comment_id="line-2",
            status="stale",
        ),
    ]

    publishing = _provider_publishing_from_refs(refs)

    assert publishing.summary_published is True
    assert publishing.summary_comment_id == "summary-1"
    assert publishing.line_comment_count == 2
    assert publishing.line_comment_status_counts == {"active": 1, "stale": 1}
    assert _provider_publishing_from_refs([]).summary_published is False


def test_github_and_gitlab_pull_request_identity_normalization() -> None:
    github = github_pull_request_snapshot(
        {
            "repository": {"id": 1, "full_name": "example/repo"},
            "pull_request": {
                "id": 2,
                "number": 42,
                "state": "closed",
                "merged": True,
                "title": "Improve review",
                "user": {"login": "alice"},
                "base": {
                    "ref": "main",
                    "sha": "a" * 40,
                    "repo": {"full_name": "example/repo"},
                },
                "head": {
                    "ref": "feature",
                    "sha": "b" * 40,
                    "repo": {"full_name": "fork/repo"},
                },
            },
        },
    )
    gitlab = gitlab_pull_request_snapshot(
        {
            "project": {"id": 3, "path_with_namespace": "group/project"},
            "user": {"name": "Bob"},
            "object_attributes": {
                "id": 4,
                "iid": 7,
                "title": "Review MR",
                "state": "opened",
                "target_branch": "main",
                "target_branch_sha": "c" * 40,
                "source_branch": "feature",
                "last_commit": {"id": "d" * 40},
                "target": {"path_with_namespace": "group/project"},
                "source": {"path_with_namespace": "fork/project"},
            },
        },
    )

    assert github is not None
    assert github.status == "merged"
    assert github.author_login == "alice"
    assert github.provider_repo_id == "1"
    assert gitlab is not None
    assert gitlab.repository == "group/project"
    assert gitlab.head_sha == "d" * 40
    assert gitlab.author_login == "Bob"


@pytest.mark.parametrize(
    ("normalizer", "payload"),
    [
        (github_pull_request_snapshot, {}),
        (
            github_pull_request_snapshot,
            {"repository": {}, "pull_request": {"number": 1}},
        ),
        (gitlab_pull_request_snapshot, {}),
        (
            gitlab_pull_request_snapshot,
            {"project": {"path_with_namespace": "group/repo"}, "object_attributes": {}},
        ),
    ],
)
def test_incomplete_provider_payloads_have_no_identity(
    normalizer,
    payload: dict,
) -> None:
    assert normalizer(payload) is None


def test_coalesce_keys_separate_review_heads_from_lifecycle_events() -> None:
    assert _build_coalesce_key("github", None, 42, "b" * 40) is None
    assert _build_coalesce_key("github", "example/repo", None, "b" * 40) is None
    assert _build_coalesce_key("github", "example/repo", 42, None) == (
        "github:example/repo:42:lifecycle"
    )
    assert _build_coalesce_key("github", "example/repo", 42, "b" * 40) == (
        f"github:example/repo:42:{'b' * 40}:review"
    )


def test_filter_builders_cover_every_supported_filter() -> None:
    now = datetime.now(UTC)
    assert len(
        _agent_task_filters(
            status="queued",
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            task_type="message_command",
            created_from=now,
            created_to=now,
        )
    ) == 7
    assert len(
        _provider_event_filters(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            internal_event="pr_opened",
            status="queued",
            delivery_id="delivery-1",
            created_from=now,
            created_to=now,
        )
    ) == 8
    common = {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "status": "running",
        "stage": "analyzing",
        "head_sha": "b" * 40,
        "trigger_type": "webhook",
    }
    assert len(_review_run_filters(**common, lock_state="unlocked")) == 8
    assert len(_review_run_filters(**common, lock_state="locked")) == 9
    assert len(_review_run_filters(**common, lock_state="expired")) == 9


def test_safe_error_and_finding_count_helpers() -> None:
    assert _safe_error_message(None) is None
    assert _safe_error_message("first\nsecret second") == "first"
    assert _safe_error_message("x" * 1001) == f"{'x' * 1000}..."
    findings = [
        type("Finding", (), {"severity": "high"})(),
        type("Finding", (), {"severity": "high"})(),
        object(),
    ]
    assert _finding_count_by_severity(findings) == {"high": 2, "unknown": 1}


async def test_review_config_get_or_create_is_idempotent(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/service-contract.db",
    )
    engine = create_engine(settings)
    await init_models(engine)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            first = await get_or_create_review_config(
                session,
                provider="github",
                repo_full_name="example/repo",
                default_skill="security-review",
                default_profile="strict",
            )
            second = await get_or_create_review_config(
                session,
                provider="github",
                repo_full_name="example/repo",
            )
    finally:
        await engine.dispose()

    assert second.id == first.id
    assert second.default_review_skill == "security-review"
    assert second.default_review_profile == "strict"
