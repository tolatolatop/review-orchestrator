from datetime import timedelta
from pathlib import Path

import pytest

from review_orchestrator.application.delivery import (
    claim_next_delivery,
    enqueue_delivery,
    process_next_delivery,
)
from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.models import AgentTask, DeliveryOutbox, utc_now
from review_orchestrator.providers import ProviderError, ProviderRegistry


class DeliveryAdapter:
    provider = "github"

    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def publish_agent_task_comment(self, session, task, *, state: str):
        del session
        self.calls.append(state)
        if self.failures:
            self.failures -= 1
            raise ProviderError("provider temporarily unavailable")
        task.response_comment_id = "comment-1"
        return "comment-1"


async def _session_factory(tmp_path: Path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/outbox.db")
    engine = create_engine(settings)
    await init_models(engine)
    return engine, create_session_factory(engine)


def _task(priority: int = 80) -> AgentTask:
    return AgentTask(
        capability_id="pr-assistant",
        provider="github",
        repo_full_name="owner/repo",
        pull_request_number=42,
        task_type="message_command",
        status="awaiting_delivery",
        execution_status="completed",
        delivery_status="pending",
        queue="interactive",
        priority=priority,
        effective_priority=priority,
    )


async def test_enqueue_delivery_is_idempotent_and_can_supersede(
    tmp_path: Path,
) -> None:
    engine, factory = await _session_factory(tmp_path)
    try:
        async with factory() as session:
            task = _task()
            session.add(task)
            await session.flush()
            first = await enqueue_delivery(
                session,
                task,
                provider="github",
                operation="agent_task_comment",
                destination_key=f"task:{task.id}:comment",
                idempotency_key=f"task:{task.id}:working",
                payload={"state": "working"},
            )
            duplicate = await enqueue_delivery(
                session,
                task,
                provider="github",
                operation="agent_task_comment",
                destination_key=f"task:{task.id}:comment",
                idempotency_key=f"task:{task.id}:working",
                payload={"state": "working"},
            )
            final = await enqueue_delivery(
                session,
                task,
                provider="github",
                operation="agent_task_comment",
                destination_key=f"task:{task.id}:comment",
                idempotency_key=f"task:{task.id}:completed",
                payload={"state": "completed", "final_status": "completed"},
                supersede_pending=True,
            )
            await session.commit()

            assert duplicate.id == first.id
            await session.refresh(first)
            assert first.status == "superseded"
            assert final.status == "queued"

            with pytest.raises(ValueError, match="different payload"):
                await enqueue_delivery(
                    session,
                    task,
                    provider="github",
                    operation="agent_task_comment",
                    destination_key=f"task:{task.id}:comment",
                    idempotency_key=f"task:{task.id}:completed",
                    payload={"state": "failed"},
                )
    finally:
        await engine.dispose()


async def test_delivery_claim_uses_task_priority(tmp_path: Path) -> None:
    engine, factory = await _session_factory(tmp_path)
    try:
        async with factory() as session:
            low = _task(priority=10)
            high = _task(priority=90)
            session.add_all([low, high])
            await session.flush()
            for task in (low, high):
                await enqueue_delivery(
                    session,
                    task,
                    provider="github",
                    operation="agent_task_comment",
                    destination_key=f"task:{task.id}:comment",
                    idempotency_key=f"task:{task.id}:completed",
                    payload={"state": "completed"},
                )
            await session.commit()

            claimed = await claim_next_delivery(session, worker_id="publisher-1")

            assert claimed is not None
            assert claimed.task_id == high.id
            assert claimed.attempt == 1
    finally:
        await engine.dispose()


async def test_delivery_success_finalizes_task_without_rerunning_agent(
    tmp_path: Path,
) -> None:
    engine, factory = await _session_factory(tmp_path)
    adapter = DeliveryAdapter()
    try:
        async with factory() as session:
            task = _task()
            task.result_json = {"outcome": "answered", "answer": "done"}
            session.add(task)
            await session.flush()
            await enqueue_delivery(
                session,
                task,
                provider="github",
                operation="agent_task_comment",
                destination_key=f"task:{task.id}:comment",
                idempotency_key=f"task:{task.id}:completed",
                payload={
                    "state": "completed",
                    "final_status": "completed",
                    "final_stage": "completed",
                },
            )
            await session.commit()

            delivery = await process_next_delivery(
                session,
                worker_id="publisher-1",
                provider_registry=ProviderRegistry([adapter]),
            )
            await session.refresh(task)

            assert delivery is not None
            assert delivery.status == "delivered"
            assert adapter.calls == ["completed"]
            assert task.status == "completed"
            assert task.execution_status == "completed"
            assert task.delivery_status == "delivered"
            assert task.result_json["answer"] == "done"
    finally:
        await engine.dispose()


async def test_delivery_failure_retries_only_outbox_then_fails_delivery(
    tmp_path: Path,
) -> None:
    engine, factory = await _session_factory(tmp_path)
    adapter = DeliveryAdapter(failures=2)
    try:
        async with factory() as session:
            task = _task()
            task.result_json = {"answer": "preserved"}
            session.add(task)
            await session.flush()
            delivery = await enqueue_delivery(
                session,
                task,
                provider="github",
                operation="agent_task_comment",
                destination_key=f"task:{task.id}:comment",
                idempotency_key=f"task:{task.id}:completed",
                payload={"state": "completed", "final_status": "completed"},
                max_attempts=2,
            )
            await session.commit()
            now = utc_now()

            first = await process_next_delivery(
                session,
                worker_id="publisher-1",
                provider_registry=ProviderRegistry([adapter]),
                retry_delay_seconds=1,
                now=now,
            )
            assert first is not None
            assert first.status == "queued"
            assert first.attempt == 1

            second = await process_next_delivery(
                session,
                worker_id="publisher-2",
                provider_registry=ProviderRegistry([adapter]),
                retry_delay_seconds=1,
                now=now + timedelta(seconds=2),
            )
            await session.refresh(task)
            stored = await session.get(DeliveryOutbox, delivery.id)

            assert second is not None
            assert stored is not None
            assert stored.status == "failed"
            assert stored.attempt == 2
            assert task.status == "failed"
            assert task.stage == "delivery_failed"
            assert task.execution_status == "completed"
            assert task.delivery_status == "failed"
            assert task.result_json["answer"] == "preserved"
            assert adapter.calls == ["completed", "completed"]
    finally:
        await engine.dispose()
