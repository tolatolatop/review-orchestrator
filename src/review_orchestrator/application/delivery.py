"""Durable, independently scheduled Provider delivery outbox."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.domain.models import (
    AgentTask,
    DeliveryOutbox,
    ReviewCommentSlot,
    ReviewRun,
    Task,
    utc_now,
)
from review_orchestrator.domain.review_results import ChangedFile
from review_orchestrator.integrations.providers import (
    AgentTaskCommentsCapability,
    LineCommentsCapability,
    ProviderError,
    ProviderRegistry,
    ReviewSummaryCapability,
)


async def enqueue_delivery(
    session: AsyncSession,
    task: Task,
    *,
    provider: str,
    operation: str,
    destination_key: str,
    idempotency_key: str,
    payload: dict[str, Any],
    mandatory: bool = True,
    priority: int | None = None,
    max_attempts: int = 5,
    supersede_pending: bool = False,
) -> DeliveryOutbox:
    """Create an idempotent delivery in the caller's transaction."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    existing = (
        await session.execute(
            select(DeliveryOutbox).where(
                DeliveryOutbox.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_digest != digest:
            raise ValueError(
                f"Delivery idempotency key has different payload: {idempotency_key}"
            )
        return existing

    if supersede_pending:
        await session.execute(
            update(DeliveryOutbox)
            .where(
                DeliveryOutbox.task_id == task.id,
                DeliveryOutbox.destination_key == destination_key,
                DeliveryOutbox.status == "queued",
            )
            .values(status="superseded", updated_at=utc_now())
        )
    delivery = DeliveryOutbox(
        task_id=task.id,
        provider=provider,
        operation=operation,
        destination_key=destination_key,
        idempotency_key=idempotency_key,
        payload_json=payload,
        payload_digest=digest,
        mandatory=mandatory,
        status="queued",
        queue=f"provider-{provider}",
        priority=task.priority if priority is None else priority,
        max_attempts=max_attempts,
    )
    if mandatory:
        task.delivery_status = "pending"
        session.add(task)
    session.add(delivery)
    await session.flush()
    return delivery


async def claim_next_delivery(
    session: AsyncSession,
    *,
    worker_id: str,
    lock_seconds: int = 60,
    now: datetime | None = None,
) -> DeliveryOutbox | None:
    now = now or utc_now()
    await reclaim_expired_deliveries(session, now=now)
    statement = (
        select(DeliveryOutbox)
        .where(
            DeliveryOutbox.status == "queued",
            DeliveryOutbox.available_at <= now,
            or_(
                DeliveryOutbox.locked_until.is_(None),
                DeliveryOutbox.locked_until < now,
            ),
        )
        .order_by(
            DeliveryOutbox.priority.desc(),
            DeliveryOutbox.available_at,
            DeliveryOutbox.created_at,
        )
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    delivery = (await session.execute(statement)).scalar_one_or_none()
    if delivery is None:
        return None
    delivery.status = "running"
    delivery.lock_owner = f"{worker_id}:{uuid4()}"
    delivery.locked_until = now + timedelta(seconds=lock_seconds)
    delivery.attempt += 1
    delivery.updated_at = now
    session.add(delivery)
    await session.commit()
    await session.refresh(delivery)
    return delivery


async def process_next_delivery(
    session: AsyncSession,
    *,
    worker_id: str,
    provider_registry: ProviderRegistry,
    lock_seconds: int = 60,
    retry_delay_seconds: int = 30,
    now: datetime | None = None,
) -> DeliveryOutbox | None:
    now = now or utc_now()
    delivery = await claim_next_delivery(
        session,
        worker_id=worker_id,
        lock_seconds=lock_seconds,
        now=now,
    )
    if delivery is None:
        return None
    lease_token = delivery.lock_owner
    task = await session.get(Task, delivery.task_id)
    try:
        if task is None:
            raise ProviderError(
                f"Delivery task no longer exists: {delivery.task_id}",
                provider=delivery.provider,
                operation=delivery.operation,
            )
        provider_message_id = await _execute_delivery(
            session,
            task=task,
            delivery=delivery,
            provider_registry=provider_registry,
        )
    except Exception as exc:
        await _record_delivery_failure(
            session,
            delivery=delivery,
            task=task,
            error=exc,
            retry_delay_seconds=retry_delay_seconds,
            now=now,
        )
        return delivery

    await session.refresh(delivery)
    if delivery.status != "running" or delivery.lock_owner != lease_token:
        # Another worker reclaimed the expired lease while the provider call was
        # in flight. The provider effect is recovered by the slot marker; this
        # stale worker must not advance task state.
        return delivery
    if task is not None:
        # Provider adapters may commit while publishing. Reload the aggregate so
        # a concurrent cancellation/result cannot be overwritten by stale state.
        await session.refresh(task)
    delivery.status = "delivered"
    delivery.provider_message_id = provider_message_id
    delivery.last_error = None
    delivery.delivered_at = now
    delivery.lock_owner = None
    delivery.locked_until = None
    delivery.updated_at = now
    session.add(delivery)
    if task is not None:
        await _apply_delivery_success(session, task=task, delivery=delivery, now=now)
    await session.commit()
    await session.refresh(delivery)
    return delivery


async def _execute_delivery(
    session: AsyncSession,
    *,
    task: Task,
    delivery: DeliveryOutbox,
    provider_registry: ProviderRegistry,
) -> str | None:
    payload = delivery.payload_json
    if delivery.operation == "agent_task_comment":
        if not isinstance(task, AgentTask):
            raise TypeError("agent_task_comment requires an AgentTask.")
        adapter = provider_registry.require_capability(
            delivery.provider,
            AgentTaskCommentsCapability,
            operation=delivery.operation,
        )
        return await adapter.publish_agent_task_comment(
            session,
            task,
            state=str(payload["state"]),
        )
    if delivery.operation in {
        "review_placeholder",
        "review_progress",
        "review_result",
        "review_summary",
    }:
        if not isinstance(task, ReviewRun):
            raise TypeError("review_summary requires a ReviewRun.")
        adapter = provider_registry.require_capability(
            delivery.provider,
            ReviewSummaryCapability,
            operation=delivery.operation,
        )
        if delivery.operation != "review_summary":
            slot = await _delivery_comment_slot(session, delivery)
            if slot is None:
                raise ProviderError(
                    "Review comment delivery is missing its comment slot.",
                    provider=delivery.provider,
                    operation=delivery.operation,
                )
            if delivery.operation != "review_placeholder" and (
                not slot.provider_comment_id
                or slot.status not in {"ready", "finalizing", "finalized"}
            ):
                raise ProviderError(
                    "Review result is waiting for its placeholder comment.",
                    provider=delivery.provider,
                    operation=delivery.operation,
                )
            if delivery.operation != "review_placeholder":
                requested_version = payload.get("result_version")
                if not isinstance(requested_version, int) or requested_version < 1:
                    raise ProviderError(
                        "Review comment delivery has an invalid result version.",
                        provider=delivery.provider,
                        operation=delivery.operation,
                    )
                if requested_version < slot.result_version:
                    # A newer progress/result was requested while this delivery
                    # was retrying. Mark this event delivered without allowing it
                    # to overwrite the newer provider-visible state.
                    return slot.provider_comment_id
                if requested_version > slot.result_version:
                    raise ProviderError(
                        "Review comment delivery is ahead of its comment slot.",
                        provider=delivery.provider,
                        operation=delivery.operation,
                    )
        original_failure_code = task.failure_code
        original_error = task.error
        result = await adapter.publish_summary_comment(
            session,
            task,
            status_text=str(payload["status_text"]),
            finding_stats=task.finding_count_by_severity,
        )
        if result is None and task.failure_code == "provider_permission_denied":
            delivery_error = task.error or "Provider summary publication failed."
            task.failure_code = original_failure_code
            task.error = original_error
            session.add(task)
            await session.commit()
            raise ProviderError(
                delivery_error,
                provider=delivery.provider,
                operation=delivery.operation,
            )
        return None if result is None else result.provider_comment_id
    if delivery.operation == "review_line_comments":
        if not isinstance(task, ReviewRun):
            raise TypeError("review_line_comments requires a ReviewRun.")
        adapter = provider_registry.require_capability(
            delivery.provider,
            LineCommentsCapability,
            operation=delivery.operation,
        )
        changed_files = [
            ChangedFile.model_validate(item)
            for item in payload.get("changed_files", [])
        ]
        stats = await adapter.publish_line_comments(
            session,
            task,
            changed_files=changed_files,
        )
        if stats.get("failed", 0):
            raise ProviderError(
                f"{stats['failed']} line comments failed to publish.",
                provider=delivery.provider,
                operation=delivery.operation,
            )
        return None
    raise ValueError(f"Unknown delivery operation: {delivery.operation}")


async def _apply_delivery_success(
    session: AsyncSession,
    *,
    task: Task,
    delivery: DeliveryOutbox,
    now: datetime,
) -> None:
    payload = delivery.payload_json
    apply_transition = True
    if isinstance(task, ReviewRun) and delivery.operation in {
        "review_placeholder",
        "review_progress",
        "review_result",
    }:
        slot = await _delivery_comment_slot(session, delivery)
        if slot is None:
            raise ProviderError(
                "Review comment delivery is missing its comment slot.",
                provider=delivery.provider,
                operation=delivery.operation,
            )
        if delivery.operation in {"review_progress", "review_result"}:
            apply_transition = payload.get("result_version") == slot.result_version
        if delivery.provider_message_id:
            slot.provider_comment_id = delivery.provider_message_id
        if apply_transition:
            slot.last_error = None
        if delivery.operation == "review_placeholder":
            if not slot.provider_comment_id:
                raise ProviderError(
                    "Placeholder delivery did not bind a provider comment.",
                    provider=delivery.provider,
                    operation=delivery.operation,
                )
            slot.status = "ready"
            slot.bound_at = now
        elif apply_transition and delivery.operation == "review_result":
            slot.status = "finalized"
            slot.finalized_at = now
        session.add(slot)
    next_stage = payload.get("success_stage")
    allow_execution = not (
        isinstance(task, ReviewRun)
        and delivery.operation == "review_placeholder"
        and task.execution_status in {"completed", "failed", "cancelled"}
    )
    if (
        apply_transition
        and allow_execution
        and isinstance(next_stage, str)
        and next_stage
    ):
        task.stage = next_stage
    success_status = payload.get("success_status")
    if (
        apply_transition
        and allow_execution
        and isinstance(success_status, str)
        and success_status
    ):
        task.status = success_status
    pending_mandatory = int(
        (
            await session.execute(
                select(func.count()).where(
                    DeliveryOutbox.task_id == task.id,
                    DeliveryOutbox.id != delivery.id,
                    DeliveryOutbox.mandatory.is_(True),
                    DeliveryOutbox.status.in_({"queued", "running"}),
                )
            )
        ).scalar_one()
    )
    if delivery.mandatory and pending_mandatory == 0:
        task.delivery_status = "delivered"
        final_status = payload.get("final_status")
        if apply_transition and isinstance(final_status, str) and final_status:
            task.status = final_status
            task.stage = str(payload.get("final_stage") or final_status)
            task.completed_at = now
    task.updated_at = now
    session.add(task)


async def _record_delivery_failure(
    session: AsyncSession,
    *,
    delivery: DeliveryOutbox,
    task: Task | None,
    error: Exception,
    retry_delay_seconds: int,
    now: datetime,
) -> None:
    delivery.last_error = str(error)
    delivery.lock_owner = None
    delivery.locked_until = None
    delivery.updated_at = now
    if isinstance(task, ReviewRun) and delivery.operation in {
        "review_placeholder",
        "review_progress",
        "review_result",
    }:
        slot = await _delivery_comment_slot(session, delivery)
        if slot is not None:
            if delivery.operation == "review_placeholder":
                slot.status = "retry_wait"
            elif delivery.operation == "review_result":
                slot.status = "finalizing"
            slot.last_error = str(error)
            session.add(slot)
    exhausted = delivery.max_attempts > 0 and delivery.attempt >= delivery.max_attempts
    if exhausted:
        delivery.status = "failed"
        if task is not None and delivery.mandatory:
            task.delivery_status = "failed"
            task.status = "failed"
            task.stage = "delivery_failed"
            task.completed_at = now
            task.updated_at = now
            session.add(task)
    else:
        delivery.status = "queued"
        delivery.available_at = now + timedelta(
            seconds=retry_delay_seconds
            * (2 ** min(8, max(0, delivery.attempt - 1)))
        )
    session.add(delivery)
    await session.commit()
    await session.refresh(delivery)


async def reclaim_expired_deliveries(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Return abandoned delivery leases to the runnable queue."""

    now = now or utc_now()
    result = await session.execute(
        update(DeliveryOutbox)
        .where(
            DeliveryOutbox.status == "running",
            DeliveryOutbox.locked_until.is_not(None),
            DeliveryOutbox.locked_until < now,
        )
        .values(
            status="queued",
            lock_owner=None,
            locked_until=None,
            available_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    return int(result.rowcount or 0)


async def _delivery_comment_slot(
    session: AsyncSession,
    delivery: DeliveryOutbox,
) -> ReviewCommentSlot | None:
    slot_id = delivery.payload_json.get("slot_id")
    if not isinstance(slot_id, str) or not slot_id:
        return None
    slot = await session.get(ReviewCommentSlot, slot_id)
    if slot is None or slot.review_run_id != delivery.task_id:
        return None
    return slot
