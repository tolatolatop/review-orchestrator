from pathlib import Path


def test_openhands_state_uses_the_configured_file_store_path() -> None:
    compose = (
        Path(__file__).parents[1] / "docker-compose.self_host.yaml"
    ).read_text()

    assert "- openhands_state:/.openhands\n" in compose
    assert "- openhands_state:/.openhands-state\n" not in compose
    assert ("SANDBOX_HOST_PORT: ${SANDBOX_HOST_PORT:-3000}") in compose
    assert "OH_SANDBOX_KIND: DockerSandboxServiceInjector" in compose
    assert '"host.docker.internal":"192.168.176.10"' in compose
    assert "ipv4_address: 192.168.176.10" in compose
    assert "subnet: 192.168.176.0/24" in compose


def test_openhands_uses_a_separate_provisioned_postgres_database() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "docker-compose.self_host.yaml").read_text()
    example_env = (root / ".env.example").read_text()

    assert "openhands-db-init:" in compose
    assert "openhands-db-migrate:" in compose
    assert "DB_HOST: postgres" in compose
    assert "DB_NAME: ${OPENHANDS_DB_NAME:-openhands}" in compose
    assert "DB_USER: ${OPENHANDS_DB_USER:-openhands}" in compose
    assert "DB_PASS: ${OPENHANDS_DB_PASSWORD:-openhands}" in compose
    assert "OPENHANDS_IMAGE=docker.openhands.dev/openhands/openhands:1.8" in example_env
    assert (
        "openhands-db-migrate:\n        condition: service_completed_successfully"
        in compose
    )


def test_openhands_migration_is_fail_closed_and_keeps_a_backup() -> None:
    migration = (
        Path(__file__).parents[1]
        / "deploy"
        / "openhands"
        / "migrate_sqlite_to_postgres.py"
    ).read_text()

    assert 'f".db.pre-postgres-{_sqlite_digest(temporary)}.bak"' in migration
    assert "CREATE TYPE eventcallbackstatus" in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "Refusing conflicting migration" in migration
    assert "missing_keys = source_rows.keys() - target_rows.keys()" in migration
    assert (
        "with source_engine.connect() as source, target_engine.begin() as target"
        in migration
    )


def test_github_app_private_key_is_mounted_only_into_orchestrator_services() -> None:
    compose = (Path(__file__).parents[1] / "docker-compose.self_host.yaml").read_text()

    assert compose.count("- ./secrets:/run/secrets:ro") == 2
    openhands_section = compose.split("  openhands:", 1)[1].split(
        "  review-orchestrator:", 1
    )[0]
    assert "/run/secrets" not in openhands_section
