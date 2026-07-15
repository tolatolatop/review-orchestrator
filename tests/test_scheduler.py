from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from review_orchestrator.application.scheduler import (
    SchedulerPolicy,
    _resource_demands,
    claim_next_task,
    parse_resource_capacities,
    policy_from_settings,
    release_task_claim,
    renew_task_claim,
)
from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.models import AgentTask, ResourceLease, ResourcePool, utc_now


async def _session_factory(tmp_path: Path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/scheduler.db")
    engine = create_engine(settings)
    await init_models(engine)
    return engine, create_session_factory(engine)


def _task(
    number: int,
    *,
    priority: int,
    resource_context: dict | None = None,
    created_at=None,
) -> AgentTask:
    return AgentTask(
        capability_id="pr-assistant",
        provider="github",
        repo_full_name="owner/repo",
        pull_request_number=number,
        task_type="message_command",
        status="queued",
        stage="ready",
        queue="interactive",
        priority=priority,
        effective_priority=priority,
        resource_context_json=resource_context,
        created_at=created_at or utc_now(),
    )


async def test_scheduler_postgres_locks_only_the_task_base_table() -> None:
    class EmptyResult:
        def scalars(self):
            return []

    class RecordingSession:
        def __init__(self) -> None:
            self.statements = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement):
            self.statements.append(statement)
            return EmptyResult()

        async def commit(self) -> None:
            return None

    session = RecordingSession()

    claimed = await claim_next_task(session, worker_id="worker-1")  # type: ignore[arg-type]

    assert claimed is None
    sql = str(
        session.statements[-1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR UPDATE OF task SKIP LOCKED" in sql
    assert "FOR UPDATE OF review_run" not in sql
    assert "FOR UPDATE OF agent_task" not in sql


async def test_scheduler_claims_highest_priority_task(tmp_path: Path) -> None:
    engine, factory = await _session_factory(tmp_path)
    try:
        async with factory() as session:
            low = _task(1, priority=20)
            high = _task(2, priority=80)
            session.add_all([low, high])
            await session.commit()

            claimed = await claim_next_task(
                session,
                worker_id="worker-1",
                task_kinds={"agent"},
            )

            assert claimed is not None
            assert claimed.id == high.id
            assert claimed.status == "running"
            assert claimed.lock_owner == "worker-1"
    finally:
        await engine.dispose()


async def test_scheduler_priority_aging_prevents_starvation(tmp_path: Path) -> None:
    engine, factory = await _session_factory(tmp_path)
    now = utc_now()
    try:
        async with factory() as session:
            old = _task(
                1,
                priority=10,
                created_at=now - timedelta(minutes=20),
            )
            recent = _task(2, priority=20, created_at=now)
            session.add_all([old, recent])
            await session.commit()
            claim_time = utc_now()

            claimed = await claim_next_task(
                session,
                worker_id="worker-1",
                task_kinds={"agent"},
                now=claim_time,
                policy=SchedulerPolicy(aging_interval_seconds=60),
            )

            assert claimed is not None
            assert claimed.id == old.id
            assert claimed.effective_priority == 30
    finally:
        await engine.dispose()


async def test_scheduler_skips_resource_blocked_task_atomically(
    tmp_path: Path,
) -> None:
    engine, factory = await _session_factory(tmp_path)
    policy = SchedulerPolicy(
        resource_capacities={"pr": 1, "model": 1},
        aging_interval_seconds=300,
    )
    try:
        async with factory() as session:
            first = _task(
                1,
                priority=90,
                resource_context={"pr": "owner/repo/1", "model": "gpt"},
            )
            blocked = _task(
                1,
                priority=80,
                resource_context={"pr": "owner/repo/1", "model": "other"},
            )
            unrelated = _task(
                2,
                priority=40,
                resource_context={"pr": "owner/repo/2", "model": "third"},
            )
            session.add_all([first, blocked, unrelated])
            await session.commit()

            claimed_first = await claim_next_task(
                session,
                worker_id="worker-1",
                task_kinds={"agent"},
                policy=policy,
            )
            claimed_second = await claim_next_task(
                session,
                worker_id="worker-2",
                task_kinds={"agent"},
                policy=policy,
            )

            assert claimed_first is not None
            assert claimed_first.id == first.id
            assert claimed_second is not None
            assert claimed_second.id == unrelated.id
            blocked_leases = (
                await session.execute(
                    select(func.count()).select_from(ResourceLease).where(
                        ResourceLease.task_id == blocked.id
                    )
                )
            ).scalar_one()
            assert blocked_leases == 0

            claimed_first.status = "completed"
            claimed_first.execution_status = "completed"
            session.add(claimed_first)
            await session.commit()
            await release_task_claim(session, first.id, owner="worker-1")
            claimed_after_release = await claim_next_task(
                session,
                worker_id="worker-3",
                task_kinds={"agent"},
                policy=policy,
            )
            assert claimed_after_release is not None
            assert claimed_after_release.id == blocked.id
    finally:
        await engine.dispose()


async def test_scheduler_counted_capacity_and_lease_renewal(tmp_path: Path) -> None:
    engine, factory = await _session_factory(tmp_path)
    policy = SchedulerPolicy(resource_capacities={"model": 2})
    try:
        async with factory() as session:
            first = _task(1, priority=80, resource_context={"model": "gpt"})
            second = _task(2, priority=70, resource_context={"model": "gpt"})
            third = _task(3, priority=60, resource_context={"model": "gpt"})
            session.add_all([first, second, third])
            await session.commit()
            claim_time = utc_now()

            assert (
                await claim_next_task(
                    session,
                    worker_id="worker-1",
                    task_kinds={"agent"},
                    policy=policy,
                    now=claim_time,
                    lock_seconds=30,
                )
            ).id == first.id
            assert (
                await claim_next_task(
                    session,
                    worker_id="worker-2",
                    task_kinds={"agent"},
                    policy=policy,
                    now=claim_time,
                    lock_seconds=30,
                )
            ).id == second.id
            assert (
                await claim_next_task(
                    session,
                    worker_id="worker-3",
                    task_kinds={"agent"},
                    policy=policy,
                    now=claim_time,
                    lock_seconds=30,
                )
                is None
            )

            pool = await session.get(ResourcePool, "model:gpt")
            assert pool is not None
            pool.capacity = 3
            session.add(pool)
            await session.commit()
            claimed_third = await claim_next_task(
                session,
                worker_id="worker-3",
                task_kinds={"agent"},
                policy=policy,
                now=claim_time,
                lock_seconds=30,
            )
            assert claimed_third is not None
            assert claimed_third.id == third.id
            await session.refresh(pool)
            assert pool.capacity == 3

            renewed = await renew_task_claim(
                session,
                first.id,
                owner="worker-1",
                lock_seconds=120,
                now=claim_time,
            )
            assert renewed is True
            leases = list(
                (
                    await session.execute(
                        select(ResourceLease).where(ResourceLease.task_id == first.id)
                    )
                ).scalars()
            )
            assert leases
            assert all(
                lease.expires_at > claim_time.replace(tzinfo=None) for lease in leases
            )
    finally:
        await engine.dispose()


async def test_scheduler_supports_multi_unit_resource_requests(tmp_path: Path) -> None:
    engine, factory = await _session_factory(tmp_path)
    policy = SchedulerPolicy(resource_capacities={"model": 3})
    try:
        async with factory() as session:
            first = _task(
                1,
                priority=80,
                resource_context={"model": {"keys": ["gpt"], "units": 2}},
            )
            blocked = _task(
                2,
                priority=70,
                resource_context={"model": {"keys": ["gpt"], "units": 2}},
            )
            session.add_all([first, blocked])
            await session.commit()

            claimed = await claim_next_task(
                session,
                worker_id="worker-1",
                task_kinds={"agent"},
                policy=policy,
            )
            unavailable = await claim_next_task(
                session,
                worker_id="worker-2",
                task_kinds={"agent"},
                policy=policy,
            )
            leases = list(
                (
                    await session.execute(
                        select(ResourceLease).where(
                            ResourceLease.task_id == first.id
                        )
                    )
                ).scalars()
            )

            assert claimed is not None
            assert claimed.id == first.id
            assert unavailable is None
            assert [lease.units for lease in leases] == [2]
    finally:
        await engine.dispose()


def test_parse_resource_capacities_extends_defaults() -> None:
    parsed = parse_resource_capacities("model=8,custom=3")

    assert parsed["model"] == 8
    assert parsed["custom"] == 3
    assert parsed["comment"] == 1


@pytest.mark.parametrize("value", ["model", "model=0"])
def test_parse_resource_capacities_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_resource_capacities(value)


def test_policy_and_resource_request_parsing_support_future_dimensions() -> None:
    settings = Settings(
        task_resource_capacities="custom=7",
        task_priority_aging_seconds=45,
        task_scheduler_scan_limit=9,
    )

    policy = policy_from_settings(settings)

    assert policy.resource_capacities["custom"] == 7
    assert policy.aging_interval_seconds == 45
    assert policy.scan_limit == 9
    assert _resource_demands(None) == []
    assert _resource_demands({"value": "gpu", "units": 2}) == [("gpu", 2)]
    with pytest.raises(ValueError, match="units must be positive"):
        _resource_demands({"keys": ["gpu"], "units": 0})


async def test_scheduler_missing_claim_release_and_renew_are_noops(
    tmp_path: Path,
) -> None:
    engine, factory = await _session_factory(tmp_path)
    try:
        async with factory() as session:
            assert await release_task_claim(session, "missing") is None
            assert (
                await renew_task_claim(
                    session,
                    "missing",
                    owner="worker",
                    lock_seconds=30,
                )
                is False
            )
    finally:
        await engine.dispose()
