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
