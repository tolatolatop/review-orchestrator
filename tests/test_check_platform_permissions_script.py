import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

SCRIPT_PATH = Path(__file__).parents[1] / "scripts/check_platform_permissions.py"
SPEC = spec_from_file_location("check_platform_permissions", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
script = module_from_spec(SPEC)
SPEC.loader.exec_module(script)

EXIT_DEGRADED = script.EXIT_DEGRADED
EXIT_HEALTHY = script.EXIT_HEALTHY
EXIT_REQUEST_ERROR = script.EXIT_REQUEST_ERROR
main = script.main


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def diagnostic_response(status: str = "healthy", provider: str = "github") -> dict:
    return {
        "provider": provider,
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
        "status": status,
        "token_configured": True,
        "reported_scopes": ["repo"],
        "repository_role": "push",
        "rate_limit_remaining": 4999,
        "checks": [
            {
                "name": "repository_read",
                "status": "passed",
                "required": True,
                "message": "Repository access verified.",
            }
        ],
    }


def test_script_calls_deployed_endpoint_and_returns_healthy(
    monkeypatch,
    capsys,
) -> None:
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(diagnostic_response())

    monkeypatch.setenv("REVIEW_PROXY_TOKEN", "operator-secret")
    with patch.object(script, "urlopen", fake_urlopen):
        exit_code = main(
            [
                "--base-url",
                "https://review.example/",
                "--provider",
                "github",
                "--repository",
                "example/repo",
                "--pull-request",
                "42",
            ]
        )

    request = captured["request"]
    assert exit_code == EXIT_HEALTHY
    assert request.full_url == (
        "https://review.example/api/v1/diagnostics/platform-permissions"
    )
    assert request.headers["X-review-token"] == "operator-secret"
    assert request.headers["User-agent"] == ("review-orchestrator-permission-check/1.0")
    assert json.loads(request.data) == {
        "provider": "github",
        "repo_full_name": "example/repo",
        "pull_request_number": 42,
    }
    assert captured["timeout"] == 15.0
    assert "Status: HEALTHY" in capsys.readouterr().out


def test_script_json_output_and_degraded_exit_code(capsys) -> None:
    with patch.object(
        script,
        "urlopen",
        return_value=FakeResponse(diagnostic_response("degraded")),
    ):
        exit_code = main(
            [
                "--provider",
                "github",
                "--repository",
                "example/repo",
                "--json",
            ]
        )

    assert exit_code == EXIT_DEGRADED
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_script_accepts_a_third_party_provider_key(capsys) -> None:
    with patch.object(
        script,
        "urlopen",
        return_value=FakeResponse(diagnostic_response(provider="gitmilk")),
    ):
        exit_code = main(
            [
                "--provider",
                "GitMilk",
                "--repository",
                "group/project",
                "--json",
            ]
        )

    assert exit_code == EXIT_HEALTHY
    assert json.loads(capsys.readouterr().out)["provider"] == "gitmilk"


def test_script_reports_connection_error_without_traceback(capsys) -> None:
    with patch.object(
        script,
        "urlopen",
        side_effect=URLError("connection refused"),
    ):
        exit_code = main(
            [
                "--provider",
                "gitlab",
                "--repository",
                "group/project",
            ]
        )

    assert exit_code == EXIT_REQUEST_ERROR
    assert "connection refused" in capsys.readouterr().err


def test_script_rejects_malformed_contract(capsys) -> None:
    with patch.object(
        script,
        "urlopen",
        return_value=FakeResponse({"status": "ok"}),
    ):
        exit_code = main(
            [
                "--provider",
                "github",
                "--repository",
                "example/repo",
            ]
        )

    assert exit_code == EXIT_REQUEST_ERROR
    assert "does not match" in capsys.readouterr().err
