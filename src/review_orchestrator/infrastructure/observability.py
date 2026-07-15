"""Shared observability pagination and redaction helpers."""

import re
from typing import Any

from pydantic import BaseModel, Field

REDACTED = "[redacted]"
REDACTED_STACK_TRACE = "[redacted stack trace]"

DEFAULT_OBSERVABILITY_LIMIT = 50
MAX_OBSERVABILITY_LIMIT = 200
DEFAULT_OBSERVABILITY_SORT = "-created_at"

SENSITIVE_KEY_TERMS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "hook",
    "installation",
    "password",
    "private_key",
    "secret",
    "set-cookie",
    "signature",
    "token",
    "x-gitlab-token",
    "x-hub-signature-256",
}

PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)
TOKEN_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
AUTH_HEADER_PATTERN = re.compile(
    r"\b(Bearer|Basic|token)\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
STACK_TRACE_PATTERN = re.compile(
    r"(Traceback \(most recent call last\):|^\s+at [\w.$]+\(.*:\d+\)|"
    r"^\s+File \".+\", line \d+)",
    re.MULTILINE,
)


class ObservabilityPage(BaseModel):
    limit: int = Field(
        default=DEFAULT_OBSERVABILITY_LIMIT,
        ge=1,
        le=MAX_OBSERVABILITY_LIMIT,
    )
    offset: int = Field(default=0, ge=0)
    sort: str = DEFAULT_OBSERVABILITY_SORT


class ObservabilityListEnvelope(BaseModel):
    total: int
    limit: int
    offset: int
    sort: str = DEFAULT_OBSERVABILITY_SORT


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("_", "-")
    compact = lowered.replace("-", "_")
    return any(term in lowered or term in compact for term in SENSITIVE_KEY_TERMS)


def redact_text(value: str) -> str:
    if STACK_TRACE_PATTERN.search(value):
        return REDACTED_STACK_TRACE

    redacted = PRIVATE_KEY_PATTERN.sub(REDACTED, value)
    redacted = JWT_PATTERN.sub(REDACTED, redacted)
    redacted = TOKEN_PATTERN.sub(REDACTED, redacted)
    redacted = AUTH_HEADER_PATTERN.sub(
        lambda match: f"{match.group(1)} {REDACTED}",
        redacted,
    )
    return redacted
