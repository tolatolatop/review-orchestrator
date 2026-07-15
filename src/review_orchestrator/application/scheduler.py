"""Database-backed priority scheduler and atomic resource leases."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.domain.models import (
    ResourceLease,
    ResourcePool,
    Task,
    utc_now,
)

DEFAULT_RESOURCE_CAPACITIES: dict[str, int] = {
    "user": 4,
    "repository": 4,
    "pr": 1,
    "pr_head": 1,
    "comment": 1,
    "model": 4,
    "concurrency": 1,
}


@dataclass(frozen=True)
class ResourceRequirement:
    key: str
    dimension: str
    units: int = 1
    capacity: int = 1


@dataclass(frozen=True)
class SchedulerPolicy:
    """Configurable scheduling rules independent from task domain types."""

    resource_capacities: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_RESOURCE_CAPACITIES)
    )
    aging_interval_seconds: int = 300
    scan_limit: int = 64

    def requirements(self, task: Task) -> list[ResourceRequirement]:
        values = task.resource_context_json or {}
        requirements: list[ResourceRequirement] = []
        for dimension, raw_value in sorted(values.items()):
            capacity = int(self.resource_capacities.get(dimension, 1))
            for value, units in _resource_demands(raw_value):
                requirements.append(
                    ResourceRequirement(
                        key=f"{dimension}:{value}",
                        dimension=dimension,
                        units=units,
                        capacity=capacity,
                    )
                )
        if task.concurrency_key:
            requirements.append(
                ResourceRequirement(
                    key=f"concurrency:{task.concurrency_key}",
                    dimension="concurrency",
                    capacity=int(self.resource_capacities.get("concurrency", 1)),
                )
            )
        unique: dict[str, ResourceRequirement] = {}
        for requirement in requirements:
            unique[requirement.key] = requirement
        return sorted(unique.values(), key=lambda item: item.key)


def parse_resource_capacities(value: str | None) -> dict[str, int]:
    capacities = dict(DEFAULT_RESOURCE_CAPACITIES)
    if not value:
        return capacities
    for item in value.split(","):
        dimension, separator, raw_capacity = item.strip().partition("=")
        if not separator or not dimension:
            raise ValueError(f"Invalid task resource capacity: {item}")
        capacity = int(raw_capacity)
        if capacity <= 0:
            raise ValueError(f"Resource capacity must be positive: {item}")
        capacities[dimension] = capacity
    return capacities


def policy_from_settings(settings: Any) -> SchedulerPolicy:
    return SchedulerPolicy(
        resource_capacities=parse_resource_capacities(
            getattr(settings, "task_resource_capacities", None)
        ),
        aging_interval_seconds=int(
            getattr(settings, "task_priority_aging_seconds", 300)
        ),
        scan_limit=int(getattr(settings, "task_scheduler_scan_limit", 64)),
    )


async def claim_next_task(
    session: AsyncSession,
    *,
    worker_id: str,
    task_kinds: set[str] | None = None,
    resource_classes: set[str] | None = None,
    lock_seconds: int = 300,
    now: datetime | None = None,
    policy: SchedulerPolicy | None = None,
) -> Task | None:
    """Claim the highest-priority eligible task and all required resources.

    Resource pools are locked in stable key order and leases are inserted only
    after every requested pool has enough capacity. A blocked task is skipped,
    avoiding head-of-line blocking for unrelated work.
    """

    now = now or utc_now()
    policy = policy or SchedulerPolicy()
    await session.execute(delete(ResourceLease).where(ResourceLease.expires_at <= now))
    await _apply_priority_aging(session, now=now, policy=policy)

    predicates = [
        Task.status.in_({"queued", "running"}),
        Task.available_at <= now,
        or_(Task.deadline_at.is_(None), Task.deadline_at > now),
        or_(Task.locked_until.is_(None), Task.locked_until < now),
    ]
    if task_kinds:
        predicates.append(Task.kind.in_(task_kinds))
    if resource_classes:
        predicates.append(Task.resource_class.in_(resource_classes))

    statement = (
        select(Task.id)
        .where(*predicates)
        .order_by(
            Task.effective_priority.desc(),
            case((Task.status == "queued", 0), else_=1),
            Task.available_at,
            Task.created_at,
        )
        .limit(policy.scan_limit)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(
            of=Task.__table__,
            skip_locked=True,
        )

    candidate_ids = list((await session.execute(statement)).scalars())
    expires_at = now + timedelta(seconds=lock_seconds)
    for task_id in candidate_ids:
        task = await session.get(Task, task_id)
        if task is None:
            continue
        if not await _compare_and_swap_task_lock(
            session,
            task_id=task_id,
            worker_id=worker_id,
            expires_at=expires_at,
            now=now,
        ):
            continue
        acquired = await _acquire_resources(
            session,
            task=task,
            worker_id=worker_id,
            stage=task.stage or "start",
            expires_at=expires_at,
            now=now,
            requirements=policy.requirements(task),
        )
        if not acquired:
            task.lock_owner = None
            task.locked_until = None
            task.updated_at = now
            session.add(task)
            await session.flush()
            continue
        if task.status == "queued":
            task.status = "running"
            task.execution_status = "running"
            task.started_at = task.started_at or now
            task.stage = task.stage or "start"
        task.lock_owner = worker_id
        task.locked_until = expires_at
        task.updated_at = now
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

    await session.commit()
    return None


async def _compare_and_swap_task_lock(
    session: AsyncSession,
    *,
    task_id: str,
    worker_id: str,
    expires_at: datetime,
    now: datetime,
) -> bool:
    if session.get_bind().dialect.name != "sqlite":
        return True
    result = await session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            or_(Task.locked_until.is_(None), Task.locked_until < now),
        )
        .values(
            lock_owner=worker_id,
            locked_until=expires_at,
            updated_at=now,
        )
    )
    return result.rowcount == 1


async def release_task_claim(
    session: AsyncSession,
    task_id: str,
    *,
    owner: str | None = None,
    retry_after_seconds: float | None = None,
    now: datetime | None = None,
) -> Task | None:
    now = now or utc_now()
    lease_predicates = [ResourceLease.task_id == task_id]
    if owner is not None:
        lease_predicates.append(ResourceLease.owner == owner)
    await session.execute(delete(ResourceLease).where(*lease_predicates))
    task = await session.get(Task, task_id)
    if task is None:
        await session.commit()
        return None
    if owner is None or task.lock_owner == owner:
        task.lock_owner = None
        task.locked_until = None
        if retry_after_seconds is not None and task.status in {"queued", "running"}:
            task.available_at = now + timedelta(seconds=retry_after_seconds)
        task.updated_at = now
        session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def renew_task_claim(
    session: AsyncSession,
    task_id: str,
    *,
    owner: str,
    lock_seconds: int,
    now: datetime | None = None,
) -> bool:
    now = now or utc_now()
    task = await session.get(Task, task_id)
    if task is None or task.lock_owner != owner:
        return False
    expires_at = now + timedelta(seconds=lock_seconds)
    task.locked_until = expires_at
    task.updated_at = now
    leases = list(
        (
            await session.execute(
                select(ResourceLease).where(
                    ResourceLease.task_id == task_id,
                    ResourceLease.owner == owner,
                )
            )
        ).scalars()
    )
    for lease in leases:
        lease.expires_at = expires_at
        session.add(lease)
    session.add(task)
    await session.commit()
    return True


async def _apply_priority_aging(
    session: AsyncSession,
    *,
    now: datetime,
    policy: SchedulerPolicy,
) -> None:
    if policy.aging_interval_seconds <= 0:
        return
    tasks = list(
        (
            await session.execute(select(Task).where(Task.status == "queued"))
        ).scalars()
    )
    for task in tasks:
        created_at = _aware(task.created_at, now)
        waited_seconds = max(0, int((now - created_at).total_seconds()))
        boost = waited_seconds // policy.aging_interval_seconds
        effective = min(100, task.priority + boost)
        if task.effective_priority != effective:
            task.effective_priority = effective
            session.add(task)


async def _acquire_resources(
    session: AsyncSession,
    *,
    task: Task,
    worker_id: str,
    stage: str,
    expires_at: datetime,
    now: datetime,
    requirements: Iterable[ResourceRequirement],
) -> bool:
    resolved = list(requirements)
    if not resolved:
        return True
    await _ensure_resource_pools(session, resolved)
    keys = [requirement.key for requirement in resolved]
    pool_statement = (
        select(ResourcePool)
        .where(ResourcePool.resource_key.in_(keys))
        .order_by(ResourcePool.resource_key)
    )
    if session.get_bind().dialect.name == "postgresql":
        pool_statement = pool_statement.with_for_update()
    pools = {
        pool.resource_key: pool
        for pool in (await session.execute(pool_statement)).scalars()
    }
    for requirement in resolved:
        pool = pools[requirement.key]
        used = int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(ResourceLease.units), 0)).where(
                        ResourceLease.resource_key == requirement.key,
                        ResourceLease.expires_at > now,
                    )
                )
            ).scalar_one()
        )
        if used + requirement.units > pool.capacity:
            return False
    for requirement in resolved:
        session.add(
            ResourceLease(
                task_id=task.id,
                stage=stage,
                resource_key=requirement.key,
                units=requirement.units,
                owner=worker_id,
                expires_at=expires_at,
            )
        )
    await session.flush()
    return True


async def _ensure_resource_pools(
    session: AsyncSession,
    requirements: list[ResourceRequirement],
) -> None:
    dialect = session.get_bind().dialect.name
    values = [
        {
            "resource_key": requirement.key,
            "dimension": requirement.dimension,
            "capacity": requirement.capacity,
        }
        for requirement in requirements
    ]
    if dialect == "postgresql":
        statement = postgresql_insert(ResourcePool).values(values)
        await session.execute(
            statement.on_conflict_do_nothing(index_elements=["resource_key"])
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(ResourcePool).values(values)
        await session.execute(
            statement.on_conflict_do_nothing(index_elements=["resource_key"])
        )
    else:
        for value in values:
            if await session.get(ResourcePool, value["resource_key"]) is None:
                session.add(ResourcePool(**value))
        await session.flush()


def _resource_demands(value: object) -> list[tuple[str, int]]:
    if value is None:
        return []
    if isinstance(value, dict):
        raw_values = value.get("keys", value.get("values", value.get("value")))
        units = int(value.get("units", 1))
        if units <= 0:
            raise ValueError("Resource request units must be positive.")
        return [(item, units) for item in _resource_values(raw_values)]
    return [(item, 1) for item in _resource_values(value)]


def _resource_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _aware(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo or UTC)
    return value
