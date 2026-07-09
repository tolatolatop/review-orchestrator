from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ReviewResultErrorCode(StrEnum):
    json_parse_error = "json_parse_error"
    schema_error = "schema_error"
    location_error = "location_error"


class ReviewResultError(ValueError):
    def __init__(
        self,
        code: ReviewResultErrorCode,
        message: str,
        *,
        finding_index: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.finding_index = finding_index
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.code,
            "message": self.message,
            "finding_index": self.finding_index,
            "retryable": self.retryable,
        }


class ReviewSkillInput(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    repo_full_name: str = Field(min_length=1, max_length=512)
    pr_number: int = Field(gt=0)
    base_sha: str = Field(min_length=7, max_length=80)
    head_sha: str = Field(min_length=7, max_length=80)
    workspace_path: str = Field(min_length=1)
    review_mode: str = Field(default="pull_request_review")


class FindingSeverity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    file: str = Field(min_length=1, max_length=1024)
    line: int | None = Field(default=None, gt=0)
    line_end: int | None = Field(default=None, gt=0)
    severity: FindingSeverity
    category: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=1200)
    suggestion: str | None = Field(default=None, max_length=1200)
    confidence: float = Field(ge=0, le=1)

    @field_validator("file")
    @classmethod
    def reject_absolute_paths(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("file must be a repository-relative path")
        return value


class ReviewResult(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=8000)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=100)


class ChangedFile(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    commentable_lines: set[int] = Field(default_factory=set)


class PublishableFinding(BaseModel):
    finding: ReviewFinding
    fingerprint: str
    publish_as_line_comment: bool
    reason: str | None = None


class ParsedReviewResult(BaseModel):
    result: ReviewResult
    findings: list[PublishableFinding]
    summary_only_findings: list[ReviewFinding]


def parse_review_result(
    raw_output: str | dict[str, Any],
    *,
    changed_files: list[ChangedFile] | None = None,
    provider: str,
    repo_full_name: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> ParsedReviewResult:
    data = _load_json(raw_output)
    result = _validate_schema(data)
    changed_file_map = {item.path: item for item in changed_files or []}

    publishable: list[PublishableFinding] = []
    summary_only: list[ReviewFinding] = []
    for finding in result.findings:
        fingerprint = build_fingerprint(
            provider=provider,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
            finding=finding,
        )
        reason = _line_comment_blocker(finding, changed_file_map)
        if reason:
            summary_only.append(finding)
        publishable.append(
            PublishableFinding(
                finding=finding,
                fingerprint=fingerprint,
                publish_as_line_comment=reason is None,
                reason=reason,
            )
        )

    return ParsedReviewResult(
        result=result,
        findings=publishable,
        summary_only_findings=summary_only,
    )


def build_fingerprint(
    *,
    provider: str,
    repo_full_name: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    finding: ReviewFinding,
) -> str:
    stable_parts = [
        provider,
        repo_full_name,
        str(pr_number),
        base_sha,
        head_sha,
        _normalize_path(finding.file),
        finding.severity,
        _normalize_text(finding.message),
    ]
    digest = hashlib.sha256("\n".join(stable_parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _load_json(raw_output: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw_output, dict):
        return raw_output
    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ReviewResultError(
            ReviewResultErrorCode.json_parse_error,
            f"Review result is not valid JSON: {exc.msg}",
        ) from exc
    if not isinstance(data, dict):
        raise ReviewResultError(
            ReviewResultErrorCode.json_parse_error,
            "Review result must be a JSON object.",
        )
    return data


def _validate_schema(data: dict[str, Any]) -> ReviewResult:
    try:
        return ReviewResult.model_validate(data)
    except ValidationError as exc:
        raise ReviewResultError(
            ReviewResultErrorCode.schema_error,
            exc.errors()[0]["msg"],
        ) from exc


def _line_comment_blocker(
    finding: ReviewFinding,
    changed_files: dict[str, ChangedFile],
) -> str | None:
    if finding.line is None:
        return "line_not_provided"
    if not changed_files:
        return None
    changed_file = changed_files.get(finding.file)
    if changed_file is None:
        return "file_not_changed"
    if (
        changed_file.commentable_lines
        and finding.line not in changed_file.commentable_lines
    ):
        return "line_not_commentable"
    return None


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip().lower()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", text.lower())).strip()
