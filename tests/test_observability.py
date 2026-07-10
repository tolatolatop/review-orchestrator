from review_orchestrator.observability import (
    REDACTED,
    REDACTED_STACK_TRACE,
    ObservabilityListEnvelope,
    ObservabilityPage,
    redact_value,
)


def test_redact_value_redacts_sensitive_keys_recursively() -> None:
    payload = {
        "repository": {"full_name": "example/repo"},
        "headers": {
            "X-Hub-Signature-256": "sha256=abc123",
            "Authorization": "Bearer ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        },
        "installation": {"id": 100, "token": "sensitive"},
        "items": [{"client_secret": "secret-value"}, {"safe": "visible"}],
    }

    redacted = redact_value(payload)

    assert redacted["repository"]["full_name"] == "example/repo"
    assert redacted["headers"]["X-Hub-Signature-256"] == REDACTED
    assert redacted["headers"]["Authorization"] == REDACTED
    assert redacted["installation"] == REDACTED
    assert redacted["items"][0]["client_secret"] == REDACTED
    assert redacted["items"][1]["safe"] == "visible"


def test_redact_value_redacts_token_shapes_and_private_keys_in_text() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
        "-----END PRIVATE KEY-----"
    )
    payload = {
        "message": (
            "failed with Bearer ghp_abcdefghijklmnopqrstuvwxyz1234567890 "
            "and jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        ),
        "key_text": private_key,
    }

    redacted = redact_value(payload)

    assert "ghp_" not in redacted["message"]
    assert "eyJ" not in redacted["message"]
    assert redacted["message"].count(REDACTED) == 2
    assert redacted["key_text"] == REDACTED


def test_redact_value_replaces_stack_traces() -> None:
    payload = {
        "error": 'Traceback (most recent call last):\n  File "app.py", line 10'
    }

    assert redact_value(payload)["error"] == REDACTED_STACK_TRACE


def test_observability_page_and_envelope_defaults() -> None:
    page = ObservabilityPage()
    envelope = ObservabilityListEnvelope(total=0, limit=page.limit, offset=page.offset)

    assert page.limit == 50
    assert page.offset == 0
    assert envelope.sort == "-created_at"
