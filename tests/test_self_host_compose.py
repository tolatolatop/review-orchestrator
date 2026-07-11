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
    assert '"host.docker.internal":"172.28.0.10"' in compose
    assert "ipv4_address: 172.28.0.10" in compose
    assert "subnet: 172.28.0.0/24" in compose
