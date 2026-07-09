from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.github import (
    NormalizedGitHubEvent,
    parse_github_datetime,
    payload_digest,
)
from review_orchestrator.models import (
    ProviderEventInbox,
    PullRequestContext,
    ReviewRun,
    utc_now,
)
from review_orchestrator.schemas import ReviewRunCreate, WebhookAccepted


async def create_review_run(
    session: AsyncSession,
    payload: ReviewRunCreate,
) -> ReviewRun:
    existing = await get_review_run_by_head(
        session,
        provider=payload.provider,
        repository=payload.repository,
        pull_request_number=payload.pull_request_number,
        head_sha=payload.head_sha,
    )
    if existing:
        return existing

    review_run = ReviewRun(**payload.model_dump(), status="queued")
    session.add(review_run)
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
    repository: str,
    pull_request_number: int,
    head_sha: str,
) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == provider,
            ReviewRun.repository == repository,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.head_sha == head_sha,
        )
    )
    return result.scalar_one_or_none()


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
            duplicate=True,
        )

    event = ProviderEventInbox(
        provider="github",
        delivery_id=delivery_id,
        provider_event=provider_event,
        provider_action=normalized_event.provider_action,
        internal_event=normalized_event.internal_event,
        repository=normalized_event.repository,
        pull_request_number=normalized_event.pull_request_number,
        head_sha=normalized_event.head_sha,
        payload_digest=payload_digest(raw_body),
        payload=payload,
        status=normalized_event.status,
    )
    session.add(event)
    await session.flush()

    review_run_id: str | None = None
    if normalized_event.should_update_context:
        await upsert_pull_request_context(session, event, payload)

    if normalized_event.should_create_review_run:
        review_run = await create_review_run_from_github_payload(session, payload)
        review_run_id = review_run.id
        event.review_run_id = review_run_id
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
            PullRequestContext.repository == repository_name,
            PullRequestContext.pull_request_number == pull_request_number,
        )
    )
    context = result.scalar_one_or_none()
    if context is None:
        context = PullRequestContext(
            provider="github",
            repository=repository_name,
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


async def create_review_run_from_github_payload(
    session: AsyncSession,
    payload: dict[str, Any],
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
            repository=repository_name,
            pull_request_number=pull_request_number,
            base_sha=_base_sha(pull_request),
            head_sha=head_sha,
        ),
    )


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
