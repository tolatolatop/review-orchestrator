from review_orchestrator.config import Settings
from review_orchestrator.github import GitHubAdapter
from review_orchestrator.gitlab import GitLabAdapter
from review_orchestrator.providers import ProviderRegistry, ProviderRuntime
from review_orchestrator.worker_cli import run_worker


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback


class FakeGitHubClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeGitLabClient:
    pass


async def test_worker_builds_and_reuses_one_provider_registry(monkeypatch) -> None:
    from review_orchestrator import worker_cli

    engine = FakeEngine()
    github_client = FakeGitHubClient()
    gitlab_client = FakeGitLabClient()
    provider_registry = ProviderRegistry(
        runtimes=[
            ProviderRuntime(
                GitHubAdapter(github_client),
                close=github_client.aclose,
            ),
            ProviderRuntime(GitLabAdapter(gitlab_client)),
        ]
    )
    registries = []

    async def init_models(fake_engine) -> None:
        assert fake_engine is engine

    async def process_timeouts(session, **kwargs):
        del session
        registries.append(kwargs["provider_registry"])
        return []

    async def process_agent_task(session, **kwargs):
        del session
        registries.append(kwargs["provider_registry"])
        return None

    async def process_review_run(session, **kwargs):
        del session
        registries.append(kwargs["provider_registry"])
        return None

    monkeypatch.setattr(worker_cli, "create_engine", lambda settings: engine)
    monkeypatch.setattr(worker_cli, "init_models", init_models)
    monkeypatch.setattr(
        worker_cli,
        "create_session_factory",
        lambda fake_engine: lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(
        worker_cli,
        "PiAgentClient",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        worker_cli,
        "create_provider_registry",
        lambda settings: provider_registry,
    )
    monkeypatch.setattr(worker_cli, "process_review_run_timeouts", process_timeouts)
    monkeypatch.setattr(worker_cli, "process_agent_task_timeouts", process_timeouts)
    monkeypatch.setattr(worker_cli, "process_next_agent_task", process_agent_task)
    monkeypatch.setattr(worker_cli, "process_next_review_run", process_review_run)

    await run_worker(settings=Settings(), once=True, worker_id="worker-test")

    assert len(registries) == 4
    assert registries[0] is registries[1] is registries[2] is registries[3]
    assert registries[0].require("github").client is github_client
    assert registries[0].require("gitlab").client is gitlab_client
    assert github_client.closed is True
    assert engine.disposed is True
