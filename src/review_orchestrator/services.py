from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.github import (
    NormalizedGitHubEvent,
    parse_github_datetime,
    payload_digest,
)
from review_orchestrator.models import (
    AgentTask,
    ProviderEventInbox,
    PullRequestContext,
    ReviewConfig,
    ReviewRun,
    utc_now,
)
from review_orchestrator.schemas import ReviewRunCreate, WebhookAccepted

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "superseded"}
CANCELLABLE_STATUSES = {"queued", "running"}


async def create_review_run(
    session: AsyncSession,
    payload: ReviewRunCreate,
    *,
    trigger_type: str = "manual",
    trigger_event_id: str | None = None,
) -> ReviewRun:
    latest = await get_latest_review_run_by_head(
        session,
        provider=payload.provider,
        repo_full_name=payload.repo_full_name,
        pull_request_number=payload.pull_request_number,
        head_sha=payload.head_sha,
    )
    if latest and not payload.force:
        return latest

    next_attempt = 1 if latest is None else latest.attempt + 1

    values = payload.model_dump(exclude={"force"})
    review_run = ReviewRun(
        **values,
        status="queued",
        trigger_type=trigger_type,
        trigger_event_id=trigger_event_id,
        attempt=next_attempt,
    )
    session.add(review_run)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await get_latest_review_run_by_head(
            session,
            provider=payload.provider,
            repo_full_name=payload.repo_full_name,
            pull_request_number=payload.pull_request_number,
            head_sha=payload.head_sha,
        )
        if existing is None:
            raise
        return existing
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def get_review_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    return await session.get(ReviewRun, review_run_id)


async def get_review_run_by_head(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str,
    pull_request_number: int,
    head_sha: str,
    attempt: int,
) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == provider,
            ReviewRun.repo_full_name == repo_full_name,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.head_sha == head_sha,
            ReviewRun.attempt == attempt,
        )
    )
    return result.scalar_one_or_none()


async def get_latest_review_run_by_head(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str,
    pull_request_number: int,
    head_sha: str,
) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun)
        .where(
            ReviewRun.provider == provider,
            ReviewRun.repo_full_name == repo_full_name,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.head_sha == head_sha,
        )
        .order_by(ReviewRun.attempt.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def retry_review_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        return None
    if review_run.status != "failed":
        return review_run
    payload = ReviewRunCreate(
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
        pull_request_number=review_run.pull_request_number,
        base_sha=review_run.base_sha,
        head_sha=review_run.head_sha,
        force=True,
    )
    return await create_review_run(session, payload, trigger_type="retry")


async def cancel_review_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        return None
    if review_run.status in CANCELLABLE_STATUSES:
        review_run.status = "cancelled"
        review_run.failure_code = "cancelled"
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
    return review_run


async def accept_github_webhook(
    session: AsyncSession,
    *,
    delivery_id: str,
    provider_event: str,
    normalized_event: NormalizedGitHubEvent,
    payload: dict[str, Any],
    raw_body: bytes,
) -> WebhookAccepted:
    existing_event = await get_provider_event(session, "github", delivery_id)
    if existing_event:
        return WebhookAccepted(
            provider="github",
            delivery_id=delivery_id,
            status=existing_event.status,
            internal_event=existing_event.internal_event,
            review_run_id=existing_event.review_run_id,
            agent_task_id=await _get_agent_task_id_for_event(
                session, existing_event.id
            ),
            duplicate=True,
        )

    event = ProviderEventInbox(
        provider="github",
        delivery_id=delivery_id,
        provider_event=provider_event,
        provider_action=normalized_event.provider_action,
        internal_event=normalized_event.internal_event,
        repo_full_name=normalized_event.repository,
        pull_request_number=normalized_event.pull_request_number,
        head_sha=normalized_event.head_sha,
        dedupe_key=f"github:{delivery_id}",
        coalesce_key=_build_coalesce_key(
            "github",
            normalized_event.repository,
            normalized_event.pull_request_number,
            normalized_event.head_sha,
        ),
        payload_digest=payload_digest(raw_body),
        payload=payload,
        status=normalized_event.status,
    )
    session.add(event)
    await session.flush()

    review_run_id: str | None = None
    agent_task_id: str | None = None
    context: PullRequestContext | None = None
    if normalized_event.should_update_context:
        context = await upsert_pull_request_context(session, event, payload)

    if normalized_event.internal_event in {"pr_closed", "pr_merged"}:
        await cancel_active_review_runs_for_pr(
            session,
            provider="github",
            repo_full_name=event.repo_full_name,
            pull_request_number=event.pull_request_number,
            failure_code=normalized_event.internal_event,
        )

    if normalized_event.should_create_review_run:
        review_run = await create_review_run_from_github_payload(
            session,
            payload,
            trigger_event_id=event.id,
        )
        await supersede_older_review_runs(session, review_run)
        review_run_id = review_run.id
        event.review_run_id = review_run_id
        event.status = "queued"
        if context is not None:
            context.latest_review_run_id = review_run_id

    if normalized_event.should_create_agent_task:
        agent_task = await create_agent_task_from_event(
            session,
            event,
            payload=payload,
            context=context,
        )
        agent_task_id = agent_task.id
        event.status = "queued"

    if event.status == "received":
        event.status = "processed"
    event.processed_at = utc_now()

    await session.commit()
    return WebhookAccepted(
        provider="github",
        delivery_id=delivery_id,
        status=event.status,
        internal_event=event.internal_event,
        review_run_id=review_run_id,
        agent_task_id=agent_task_id,
    )


async def get_provider_event(
    session: AsyncSession,
    provider: str,
    delivery_id: str,
) -> ProviderEventInbox | None:
    result = await session.execute(
        select(ProviderEventInbox).where(
            ProviderEventInbox.provider == provider,
            ProviderEventInbox.delivery_id == delivery_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_pull_request_context(
    session: AsyncSession,
    event: ProviderEventInbox,
    payload: dict[str, Any],
) -> PullRequestContext | None:
    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        return None

    repository_name = _str_or_none(repository.get("full_name"))
    pull_request_number = pull_request.get("number")
    if not repository_name or not isinstance(pull_request_number, int):
        return None

    result = await session.execute(
        select(PullRequestContext).where(
            PullRequestContext.provider == "github",
            PullRequestContext.repo_full_name == repository_name,
            PullRequestContext.pull_request_number == pull_request_number,
        )
    )
    context = result.scalar_one_or_none()
    if context is None:
        context = PullRequestContext(
            provider="github",
            repo_full_name=repository_name,
            pull_request_number=pull_request_number,
            head_sha=_head_sha(pull_request) or "",
        )
        session.add(context)

    base = pull_request.get("base")
    head = pull_request.get("head")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None

    context.provider_repo_id = _id_to_str(repository.get("id"))
    context.provider_pr_id = _id_to_str(pull_request.get("id"))
    context.title = _str_or_none(pull_request.get("title"))
    context.author_login = _login(pull_request.get("user"))
    context.base_ref = _ref(base)
    context.base_sha = _sha(base)
    context.head_ref = _ref(head)
    context.head_sha = _head_sha(pull_request) or context.head_sha
    context.head_repo_full_name = _repo_full_name(head_repo)
    context.is_fork = bool(
        context.head_repo_full_name
        and context.head_repo_full_name != _repo_full_name(base_repo)
    )
    context.status = _pull_request_status(pull_request)
    context.html_url = _str_or_none(pull_request.get("html_url"))
    context.latest_event_id = event.id
    context.closed_at = parse_github_datetime(pull_request.get("closed_at"))
    context.merged_at = parse_github_datetime(pull_request.get("merged_at"))

    return context


async def create_agent_task_from_event(
    session: AsyncSession,
    event: ProviderEventInbox,
    *,
    payload: dict[str, Any],
    context: PullRequestContext | None = None,
) -> AgentTask:
    if not event.repo_full_name or event.pull_request_number is None:
        raise ValueError("Agent task event is missing PR identity fields.")

    if context is None:
        result = await session.execute(
            select(PullRequestContext).where(
                PullRequestContext.provider == event.provider,
                PullRequestContext.repo_full_name == event.repo_full_name,
                PullRequestContext.pull_request_number == event.pull_request_number,
            )
        )
        context = result.scalar_one_or_none()

    agent_task = AgentTask(
        provider_event_id=event.id,
        pull_request_context_id=context.id if context else None,
        provider=event.provider,
        repo_full_name=event.repo_full_name,
        pull_request_number=event.pull_request_number,
        task_type="mention",
        status="queued",
        input_json={
            "internal_event": event.internal_event,
            "provider_action": event.provider_action,
            "payload": payload,
        },
    )
    session.add(agent_task)
    await session.flush()
    return agent_task


async def _get_agent_task_id_for_event(
    session: AsyncSession,
    provider_event_id: str,
) -> str | None:
    result = await session.execute(
        select(AgentTask.id)
        .where(AgentTask.provider_event_id == provider_event_id)
        .order_by(AgentTask.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def cancel_active_review_runs_for_pr(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str | None,
    pull_request_number: int | None,
    failure_code: str,
) -> list[ReviewRun]:
    if repo_full_name is None or pull_request_number is None:
        return []

    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == provider,
            ReviewRun.repo_full_name == repo_full_name,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.status.in_(CANCELLABLE_STATUSES),
        )
    )
    review_runs = list(result.scalars().all())
    now = utc_now()
    for review_run in review_runs:
        review_run.status = "cancelled"
        review_run.stage = "cleanup"
        review_run.failure_code = failure_code
        review_run.completed_at = now
        session.add(review_run)
    return review_runs


async def supersede_older_review_runs(
    session: AsyncSession,
    current_run: ReviewRun,
) -> list[ReviewRun]:
    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == current_run.provider,
            ReviewRun.repo_full_name == current_run.repo_full_name,
            ReviewRun.pull_request_number == current_run.pull_request_number,
            ReviewRun.id != current_run.id,
            ReviewRun.head_sha != current_run.head_sha,
            ReviewRun.status.in_(CANCELLABLE_STATUSES),
        )
    )
    older_runs = list(result.scalars().all())
    now = utc_now()
    for review_run in older_runs:
        review_run.status = "superseded"
        review_run.stage = "cleanup"
        review_run.failure_code = "superseded_by_new_head"
        review_run.superseded_by_review_run_id = current_run.id
        review_run.completed_at = now
        session.add(review_run)
    return older_runs


async def create_review_run_from_github_payload(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    trigger_event_id: str | None = None,
) -> ReviewRun:
    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        raise ValueError("GitHub pull_request payload is missing required objects.")

    repository_name = _str_or_none(repository.get("full_name"))
    pull_request_number = pull_request.get("number")
    head_sha = _head_sha(pull_request)
    if not repository_name or not isinstance(pull_request_number, int) or not head_sha:
        raise ValueError("GitHub pull_request payload is missing PR identity fields.")

    return await create_review_run(
        session,
        ReviewRunCreate(
            provider="github",
            repo_full_name=repository_name,
            pull_request_number=pull_request_number,
            base_sha=_base_sha(pull_request),
            head_sha=head_sha,
        ),
        trigger_type="webhook",
        trigger_event_id=trigger_event_id,
    )


async def get_or_create_review_config(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str,
) -> ReviewConfig:
    result = await session.execute(
        select(ReviewConfig).where(
            ReviewConfig.provider == provider,
            ReviewConfig.repo_full_name == repo_full_name,
        )
    )
    config = result.scalar_one_or_none()
    if config is not None:
        return config

    config = ReviewConfig(provider=provider, repo_full_name=repo_full_name)
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


def _build_coalesce_key(
    provider: str,
    repo_full_name: str | None,
    pull_request_number: int | None,
    head_sha: str | None,
) -> str | None:
    if repo_full_name is None or pull_request_number is None:
        return None
    if head_sha:
        return f"{provider}:{repo_full_name}:{pull_request_number}:{head_sha}:review"
    return f"{provider}:{repo_full_name}:{pull_request_number}:lifecycle"


def _pull_request_status(pull_request: dict[str, Any]) -> str:
    if pull_request.get("merged") is True:
        return "merged"
    state = pull_request.get("state")
    return state if isinstance(state, str) and state else "open"


def _base_sha(pull_request: dict[str, Any]) -> str | None:
    base = pull_request.get("base")
    return _sha(base)


def _head_sha(pull_request: dict[str, Any]) -> str | None:
    head = pull_request.get("head")
    return _sha(head)


def _sha(ref_object: Any) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _str_or_none(ref_object.get("sha"))


def _ref(ref_object: Any) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _str_or_none(ref_object.get("ref"))


def _repo_full_name(repo: Any) -> str | None:
    if not isinstance(repo, dict):
        return None
    return _str_or_none(repo.get("full_name"))


def _login(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return _str_or_none(user.get("login"))


def _id_to_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
