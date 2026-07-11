from pathlib import Path


def test_openhands_state_uses_the_configured_file_store_path() -> None:
    compose = (
        Path(__file__).parents[1] / "docker-compose.self_host.yaml"
    ).read_text()

    assert "- openhands_state:/.openhands\n" in compose
    assert "- openhands_state:/.openhands-state\n" not in compose
    assert (
        "SANDBOX_HOST_PORT: "
        "${SANDBOX_HOST_PORT:-${REVIEW_PROXY_PORT:-18080}}"
    ) in compose
