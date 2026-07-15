from importlib import import_module
from pathlib import Path

from review_orchestrator.config import Settings
from review_orchestrator.main import create_app


def test_public_route_inventory_survives_directory_refactors() -> None:
    app = create_app(Settings(_env_file=None))
    pending = list(app.routes)
    routes: set[tuple[str, str]] = set()
    while pending:
        route = pending.pop()
        pending.extend(getattr(route, "routes", []))
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending.extend(original_router.routes)
        path = getattr(route, "path", None)
        if path is not None:
            routes.update((method, path) for method in getattr(route, "methods", set()))
    required_routes = {
        ("GET", "/health"),
        ("GET", "/dashboard/"),
        ("GET", "/reviews/"),
        ("GET", "/api/v1/providers"),
        ("POST", "/api/v1/webhooks/{provider}"),
        ("GET", "/api/v1/provider-events"),
        ("GET", "/api/v1/agent-tasks"),
        ("POST", "/api/v1/agent-tasks/{task_id}/cancel"),
        ("POST", "/api/v1/agent-tasks/{task_id}/retry"),
        ("POST", "/api/v1/review-runs"),
        ("GET", "/api/v1/review-runs"),
        ("GET", "/api/v1/review-runs/{review_run_id}"),
        ("POST", "/api/v1/review-runs/{review_run_id}/session/start"),
        ("POST", "/api/v1/review-runs/{review_run_id}/session/sync"),
        ("POST", "/api/v1/review-runs/{review_run_id}/session/messages"),
        ("POST", "/api/v1/review-runs/{review_run_id}/session/cancel"),
        ("POST", "/api/v1/review-runs/{review_run_id}/result"),
        ("POST", "/api/v1/review-runs/{review_run_id}/retry"),
        ("POST", "/api/v1/review-runs/{review_run_id}/cancel"),
        ("POST", "/api/v1/workspaces/prepare"),
        ("GET", "/api/v1/workspaces/{workspace_id}"),
        ("POST", "/api/v1/workspaces/{workspace_id}/lease"),
        ("POST", "/api/v1/workspace-leases/{lease_id}/release"),
        ("POST", "/api/v1/workspaces/{workspace_id}/cleanup"),
        ("POST", "/api/v1/workspaces/cleanup/pr"),
        ("POST", "/api/v1/workspaces/cleanup/expired"),
    }

    assert required_routes <= routes


def test_legacy_modules_are_true_aliases_of_layered_implementations() -> None:
    aliases = {
        "review_orchestrator.api": "review_orchestrator.presentation.api",
        "review_orchestrator.comments": "review_orchestrator.integrations.comments",
        "review_orchestrator.config": "review_orchestrator.infrastructure.config",
        "review_orchestrator.dashboard": "review_orchestrator.presentation.dashboard",
        "review_orchestrator.db": "review_orchestrator.infrastructure.db",
        "review_orchestrator.github_auth": (
            "review_orchestrator.integrations.github_auth"
        ),
        "review_orchestrator.gitlab": "review_orchestrator.integrations.gitlab",
        "review_orchestrator.main": "review_orchestrator.presentation.main",
        "review_orchestrator.services": "review_orchestrator.application.services",
        "review_orchestrator.worker": "review_orchestrator.application.worker",
        "review_orchestrator.worker_cli": (
            "review_orchestrator.application.worker_cli"
        ),
        "review_orchestrator.models": "review_orchestrator.domain.models",
        "review_orchestrator.observability": (
            "review_orchestrator.infrastructure.observability"
        ),
        "review_orchestrator.schemas": "review_orchestrator.domain.schemas",
        "review_orchestrator.github": "review_orchestrator.integrations.github",
        "review_orchestrator.pi_agent": "review_orchestrator.integrations.pi_agent",
        "review_orchestrator.platform_diagnostics": (
            "review_orchestrator.integrations.platform_diagnostics"
        ),
        "review_orchestrator.providers": (
            "review_orchestrator.integrations.providers"
        ),
        "review_orchestrator.provider_plugins": (
            "review_orchestrator.integrations.provider_plugins"
        ),
        "review_orchestrator.reconciliation": (
            "review_orchestrator.domain.reconciliation"
        ),
        "review_orchestrator.review_results": (
            "review_orchestrator.domain.review_results"
        ),
        "review_orchestrator.reviews_dashboard": (
            "review_orchestrator.presentation.reviews_dashboard"
        ),
        "review_orchestrator.workspaces": (
            "review_orchestrator.infrastructure.workspaces"
        ),
    }

    for legacy_name, implementation_name in aliases.items():
        assert import_module(legacy_name) is import_module(implementation_name)


def test_layered_implementations_do_not_import_legacy_aliases() -> None:
    package_root = Path(__file__).parents[1] / "src" / "review_orchestrator"
    legacy_modules = {
        "api",
        "comments",
        "config",
        "dashboard",
        "db",
        "github",
        "github_auth",
        "gitlab",
        "main",
        "models",
        "observability",
        "pi_agent",
        "platform_diagnostics",
        "providers",
        "provider_plugins",
        "reconciliation",
        "review_results",
        "reviews_dashboard",
        "schemas",
        "services",
        "worker",
        "worker_cli",
        "workspaces",
    }
    forbidden = tuple(
        f"review_orchestrator.{module}" for module in sorted(legacy_modules)
    )

    for layer in (
        "presentation",
        "application",
        "domain",
        "integrations",
        "infrastructure",
    ):
        for source_path in (package_root / layer).glob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            assert not any(
                f"from {name} import" in source or f"import {name}" in source
                for name in forbidden
            ), source_path


def test_core_execution_paths_do_not_branch_on_builtin_platform_names() -> None:
    package_root = Path(__file__).parents[1] / "src" / "review_orchestrator"
    core_paths = [
        package_root / "application" / "services.py",
        package_root / "application" / "worker.py",
        package_root / "infrastructure" / "workspaces.py",
        package_root / "presentation" / "api.py",
    ]

    for path in core_paths:
        source = path.read_text(encoding="utf-8").lower()
        assert "github" not in source, path
        assert "gitlab" not in source, path
