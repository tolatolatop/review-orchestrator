from pathlib import Path


def test_nginx_resolves_api_service_dynamically() -> None:
    config = (
        Path(__file__).parents[1]
        / "deploy"
        / "nginx"
        / "review-orchestrator.conf.template"
    ).read_text()

    assert "resolver 127.0.0.11 ipv6=off valid=10s;" in config
    assert "set $review_orchestrator_upstream review-orchestrator:8000;" in config
    assert config.count("proxy_pass http://$review_orchestrator_upstream;") == 3
    assert "proxy_pass http://review-orchestrator:8000;" not in config


def test_nginx_token_validation_can_be_disabled_and_fails_closed_by_default() -> None:
    config = (
        Path(__file__).parents[1]
        / "deploy"
        / "nginx"
        / "review-orchestrator.conf.template"
    ).read_text()

    assert (
        'map "${REVIEW_PROXY_TOKEN_ENABLED}" $review_proxy_token_enabled {'
        in config
    )
    assert 'map "${REVIEW_PROXY_TOKEN}" $review_proxy_token_configured {' in config
    assert (
        'map "$review_proxy_token_enabled$review_token_valid" '
        "$review_access_allowed {" in config
    )
    assert "if ($review_access_allowed = 0)" in config

    enabled_map = config.split(
        'map "${REVIEW_PROXY_TOKEN_ENABLED}" $review_proxy_token_enabled {', 1
    )[1].split("}", 1)[0]
    configured_map = config.split(
        'map "${REVIEW_PROXY_TOKEN}" $review_proxy_token_configured {', 1
    )[1].split("}", 1)[0]
    access_map = config.split(
        'map "$review_proxy_token_enabled$review_token_valid" '
        "$review_access_allowed {",
        1,
    )[1].split("}", 1)[0]

    assert "default 1;" in enabled_map
    assert '"false" 0;' in enabled_map
    assert '"" 0;' in configured_map
    assert "~^0 1;" in access_map
    assert '"11" 1;' in access_map
