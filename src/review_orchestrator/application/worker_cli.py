"""Background worker process entry point."""

from __future__ import annotations

import argparse
import asyncio
import socket

from review_orchestrator.application.delivery import process_next_delivery
from review_orchestrator.application.worker import (
    process_agent_task_timeouts,
    process_next_agent_task,
    process_next_review_run,
    process_review_run_timeouts,
)
from review_orchestrator.infrastructure.config import Settings, get_settings
from review_orchestrator.infrastructure.db import (
    create_engine,
    create_session_factory,
    init_models,
)
from review_orchestrator.integrations.pi_agent import PiAgentClient
from review_orchestrator.integrations.provider_plugins import create_provider_registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Review Orchestrator worker.")
    parser.add_argument("--once", action="store_true", help="Process one polling pass.")
    parser.add_argument("--worker-id", default=None)
    args = parser.parse_args()
    asyncio.run(
        run_worker(
            settings=get_settings(),
            once=args.once,
            worker_id=args.worker_id,
        )
    )


async def run_worker(
    *,
    settings: Settings,
    once: bool = False,
    worker_id: str | None = None,
) -> None:
    engine = create_engine(settings)
    provider_registry = None
    try:
        await init_models(engine)
        session_factory = create_session_factory(engine)
        resolved_worker_id = worker_id or f"worker-{socket.gethostname()}"
        pi_agent_client = PiAgentClient(
            base_url=settings.pi_agent_base_url or "http://localhost:3210",
            api_token=settings.pi_agent_runtime_token,
            timeout=settings.pi_agent_timeout_seconds,
        )
        provider_registry = create_provider_registry(settings)

        while True:
            async with session_factory() as session:
                delivery = await process_next_delivery(
                    session,
                    worker_id=f"{resolved_worker_id}:delivery",
                    provider_registry=provider_registry,
                    retry_delay_seconds=settings.retry_initial_delay_seconds,
                )
            async with session_factory() as session:
                await process_agent_task_timeouts(
                    session,
                    settings=settings,
                    pi_agent_client=pi_agent_client,
                    provider_registry=provider_registry,
                )
            async with session_factory() as session:
                await process_review_run_timeouts(
                    session,
                    settings=settings,
                    pi_agent_client=pi_agent_client,
                    provider_registry=provider_registry,
                )
            async with session_factory() as session:
                agent_task = await process_next_agent_task(
                    session,
                    settings=settings,
                    pi_agent_client=pi_agent_client,
                    worker_id=resolved_worker_id,
                    provider_registry=provider_registry,
                )
            async with session_factory() as session:
                review_run = await process_next_review_run(
                    session,
                    settings=settings,
                    pi_agent_client=pi_agent_client,
                    worker_id=resolved_worker_id,
                    provider_registry=provider_registry,
                )
            if once:
                return
            if delivery is None and agent_task is None and review_run is None:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        try:
            if provider_registry is not None:
                await provider_registry.aclose()
        finally:
            await engine.dispose()


if __name__ == "__main__":
    main()
