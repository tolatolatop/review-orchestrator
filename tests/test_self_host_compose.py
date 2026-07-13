from pathlib import Path


def test_openhands_state_uses_the_configured_file_store_path() -> None:
    compose = (
        Path(__file__).parents[1] / "docker-compose.self_host.yaml"
    ).read_text()

    assert "- openhands_state:/.openhands\n" in compose
    assert "- openhands_state:/.openhands-state\n" not in compose
    assert (
        "SANDBOX_HOST_PORT: ${SANDBOX_HOST_PORT:-3000}"
    ) in compose
    assert "OH_SANDBOX_KIND: DockerSandboxServiceInjector" in compose
    assert '"host.docker.internal":"192.168.176.10"' in compose
    assert "ipv4_address: 192.168.176.10" in compose
    assert "subnet: 192.168.176.0/24" in compose


def test_github_app_private_key_is_mounted_only_into_orchestrator_services() -> None:
    compose = (Path(__file__).parents[1] / "docker-compose.self_host.yaml").read_text()

    assert compose.count("- ./secrets:/run/secrets:ro") == 2
    openhands_section = compose.split("  openhands:", 1)[1].split(
        "  review-orchestrator:", 1
    )[0]
    assert "/run/secrets" not in openhands_section
