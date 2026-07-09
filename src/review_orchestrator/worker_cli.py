from __future__ import annotations

import argparse
import asyncio
import socket

from review_orchestrator.config import Settings, get_settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import GitHubClient
from review_orchestrator.gitlab import GitLabClient
from review_orchestrator.openhands import OpenHandsClient
from review_orchestrator.worker import process_next_agent_task, process_next_review_run


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
    await init_models(engine)
    session_factory = create_session_factory(engine)
    resolved_worker_id = worker_id or f"worker-{socket.gethostname()}"
    openhands_client = OpenHandsClient(
        base_url=settings.openhands_base_url or "http://localhost:3000",
        api_key=settings.openhands_api_token,
        timeout=settings.openhands_timeout_seconds,
    )
    github_client = GitHubClient(
        api_base_url=settings.github_api_base_url,
        token=settings.github_installation_token,
        timeout=settings.openhands_timeout_seconds,
    )
    gitlab_client = GitLabClient(
        api_base_url=settings.gitlab_api_base_url,
        token=settings.gitlab_api_token,
        timeout=settings.openhands_timeout_seconds,
    )

    try:
        while True:
            async with session_factory() as session:
                agent_task = await process_next_agent_task(
                    session,
                    worker_id=resolved_worker_id,
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                )
            async with session_factory() as session:
                review_run = await process_next_review_run(
                    session,
                    settings=settings,
                    openhands_client=openhands_client,
                    worker_id=resolved_worker_id,
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                )
            if once:
                return
            if agent_task is None and review_run is None:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
