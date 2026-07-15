import pytest

from review_orchestrator.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolate_settings_from_developer_env(monkeypatch: pytest.MonkeyPatch):
    """Keep tests independent from credentials and paths in the local .env file."""
    get_settings.cache_clear()
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    yield
    get_settings.cache_clear()
