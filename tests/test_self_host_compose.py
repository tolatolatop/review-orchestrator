from pathlib import Path


def root_files() -> tuple[str, str]:
    root = Path(__file__).parents[1]
    return (
        (root / "docker-compose.self_host.yaml").read_text(),
        (root / ".env.example").read_text(),
    )


def test_pi_agent_runtime_is_isolated_and_workspace_is_read_only() -> None:
    compose, _ = root_files()
    runtime = compose.split("  pi-agent-runtime:", 1)[1].split(
        "  review-orchestrator:", 1
    )[0]

    assert "review-orchestrator-pi-agent:0.80.7" in runtime
    assert "review_orchestrator_data:/var/lib/review-orchestrator:ro" in runtime
    assert "pi_agent_state:/var/lib/pi-agent" in runtime
    assert "read_only: true" in runtime
    assert "cap_drop:\n      - ALL" in runtime
    assert "no-new-privileges:true" in runtime
    assert "/var/run/docker.sock" not in runtime
    assert "/run/secrets" not in runtime
    assert "GITHUB_" not in runtime
    assert "POSTGRES_" not in runtime


def test_pi_agent_runtime_supports_configurable_models_and_skills() -> None:
    compose, example_env = root_files()

    assert "PI_AGENT_PROVIDER: ${PI_AGENT_PROVIDER:-openai}" in compose
    assert "PI_AGENT_MODEL: ${PI_AGENT_MODEL:-gpt-5.4}" in compose
    assert "PI_AGENT_THINKING_LEVEL: ${PI_AGENT_THINKING_LEVEL:-high}" in compose
    assert (
        "${PI_AGENT_SKILLS_PATH:-./pi-agent-runtime/skills}:"
        "/opt/pi-agent/skills:ro"
    ) in compose
    assert (
        "${PI_AGENT_CONFIG_PATH:-./pi-agent-runtime/config}:"
        "/etc/pi-agent:ro"
    ) in compose
    assert "PI_AGENT_PROVIDER=openai" in example_env
    assert "PI_AGENT_SKILLS_PATH=./pi-agent-runtime/skills" in example_env
    assert "PI_AGENT_COMMAND_SKILL: ${AGENT_COMMAND_SKILL:-pr-assistant}" in compose
    assert "AGENT_COMMAND_SKILL=pr-assistant" in example_env


def test_worker_receives_message_command_timeout_and_history_configuration() -> None:
    compose, example_env = root_files()
    worker = compose.split("  review-orchestrator-worker:", 1)[1].split(
        "  nginx:", 1
    )[0]

    assert "AGENT_TASK_SOFT_TIMEOUT_SECONDS:" in worker
    assert "AGENT_TASK_TIMEOUT_SECONDS:" in worker
    assert "AGENT_TASK_MAX_HISTORY_TURNS:" in worker
    assert "AGENT_TASK_MAX_HISTORY_CHARS:" in worker
    assert "AGENT_TASK_ALLOWED_ASSOCIATIONS:" in worker
    assert "AGENT_TASK_SOFT_TIMEOUT_SECONDS=120" in example_env
    assert "AGENT_TASK_TIMEOUT_SECONDS=600" in example_env
    assert "AGENT_TASK_MAX_HISTORY_TURNS=6" in example_env
    assert "AGENT_TASK_MAX_HISTORY_CHARS=24000" in example_env


def test_pi_agent_runtime_defaults_to_loopback_port_3210() -> None:
    compose, example_env = root_files()

    assert '"127.0.0.1:${PI_AGENT_RUNTIME_PORT:-3210}:3210"' in compose
    assert "PI_AGENT_RUNTIME_PORT=3210" in example_env


def test_orchestrator_has_a_loopback_only_tokenless_local_port() -> None:
    compose, example_env = root_files()

    assert '"127.0.0.1:${REVIEW_LOCAL_PORT:-18000}:8000"' in compose
    assert '"${REVIEW_LOCAL_PORT:-18000}:8000"' not in compose
    assert "REVIEW_LOCAL_PORT=18000" in example_env


def test_nginx_token_gate_defaults_on_and_allows_an_empty_disabled_token() -> None:
    compose, example_env = root_files()

    assert (
        'REVIEW_PROXY_TOKEN_ENABLED: "${REVIEW_PROXY_TOKEN_ENABLED:-true}"'
        in compose
    )
    assert 'REVIEW_PROXY_TOKEN: "${REVIEW_PROXY_TOKEN:-}"' in compose
    assert "REVIEW_PROXY_TOKEN:?" not in compose
    assert "REVIEW_PROXY_TOKEN_ENABLED=true" in example_env


def test_openhands_services_and_privileged_runtime_are_removed() -> None:
    compose, example_env = root_files()

    assert "openhands:" not in compose.lower()
    assert "OPENHANDS_" not in example_env
    assert "DockerSandboxServiceInjector" not in compose
    assert "/var/run/docker.sock" not in compose


def test_github_app_private_key_is_mounted_only_into_orchestrator_services() -> None:
    compose, _ = root_files()

    assert compose.count("- ./secrets:/run/secrets:ro") == 2
    runtime = compose.split("  pi-agent-runtime:", 1)[1].split(
        "  review-orchestrator:", 1
    )[0]
    assert "/run/secrets" not in runtime
