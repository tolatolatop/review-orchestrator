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
    compose = Path("docker-compose.self_host.yaml").read_text()
    example_env = Path(".env.example").read_text()

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
    migration = Path("deploy/openhands/migrate_sqlite_to_postgres.py").read_text()

    assert 'with_suffix(".db.pre-postgres.bak")' in migration
    assert "CREATE TYPE eventcallbackstatus" in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "Refusing partial migration" in migration
    assert (
        "with source_engine.connect() as source, target_engine.begin() as target"
        in migration
    )
