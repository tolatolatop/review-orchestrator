import pytest

from review_orchestrator.review_results import (
    ChangedFile,
    ReviewResultError,
    ReviewResultErrorCode,
    ReviewSkillInput,
    build_fingerprint,
    parse_review_result,
)


def test_review_skill_input_is_commit_range_reference() -> None:
    payload = ReviewSkillInput(
        provider="github",
        repo_full_name="owner/repo",
        pr_number=123,
        base_sha="a" * 40,
        head_sha="b" * 40,
        workspace_path="/workspace/repo",
    )

    assert payload.review_mode == "pull_request_review"
    assert payload.base_sha == "a" * 40
    assert payload.head_sha == "b" * 40


def test_parse_review_result_marks_commentable_findings() -> None:
    parsed = parse_review_result(
        {
            "summary": "One high risk issue found.",
            "findings": [
                {
                    "file": "src/app.py",
                    "line": 12,
                    "severity": "high",
                    "message": "The new branch skips auth checks.",
                    "suggestion": "Require the auth guard before returning data.",
                    "confidence": 0.91,
                }
            ],
        },
        changed_files=[ChangedFile(path="src/app.py", commentable_lines={12, 13})],
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert parsed.result.summary == "One high risk issue found."
    assert parsed.findings[0].publish_as_line_comment is True
    assert parsed.findings[0].fingerprint.startswith("sha256:")
    assert parsed.summary_only_findings == []


def test_parse_review_result_downgrades_uncommentable_line_to_summary() -> None:
    parsed = parse_review_result(
        {
            "summary": "One issue found.",
            "findings": [
                {
                    "file": "src/app.py",
                    "line": 99,
                    "severity": "medium",
                    "message": "The branch leaves stale state behind.",
                    "confidence": 0.75,
                }
            ],
        },
        changed_files=[ChangedFile(path="src/app.py", commentable_lines={12, 13})],
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert parsed.findings[0].publish_as_line_comment is False
    assert parsed.findings[0].reason == "line_not_commentable"
    assert parsed.summary_only_findings[0].line == 99


def test_parse_review_result_accepts_summary_only_finding_without_line() -> None:
    parsed = parse_review_result(
        {
            "summary": "One repo-level issue found.",
            "findings": [
                {
                    "file": "src/app.py",
                    "severity": "info",
                    "category": "maintainability",
                    "message": "The new module lacks a clear ownership boundary.",
                    "confidence": 0.7,
                }
            ],
        },
        changed_files=[ChangedFile(path="src/app.py", commentable_lines={12, 13})],
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert parsed.findings[0].publish_as_line_comment is False
    assert parsed.findings[0].reason == "line_not_provided"
    assert parsed.summary_only_findings[0].line is None


def test_parse_review_result_rejects_invalid_json() -> None:
    with pytest.raises(ReviewResultError) as error:
        parse_review_result(
            "not json",
            provider="github",
            repo_full_name="owner/repo",
            pr_number=10,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

    assert error.value.code == ReviewResultErrorCode.json_parse_error


def test_parse_review_result_rejects_invalid_schema() -> None:
    with pytest.raises(ReviewResultError) as error:
        parse_review_result(
            {
                "summary": "Bad finding.",
                "findings": [
                    {
                        "file": "src/app.py",
                        "line": 12,
                        "severity": "urgent",
                        "message": "Invalid severity.",
                        "confidence": 0.9,
                    }
                ],
            },
            provider="github",
            repo_full_name="owner/repo",
            pr_number=10,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

    assert error.value.code == ReviewResultErrorCode.schema_error


def test_parse_review_result_rejects_unsafe_file_path() -> None:
    with pytest.raises(ReviewResultError) as error:
        parse_review_result(
            {
                "summary": "Bad path.",
                "findings": [
                    {
                        "file": "../secret.py",
                        "line": 12,
                        "severity": "high",
                        "message": "Unsafe path.",
                        "confidence": 0.9,
                    }
                ],
            },
            provider="github",
            repo_full_name="owner/repo",
            pr_number=10,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

    assert error.value.code == ReviewResultErrorCode.schema_error


def test_fingerprint_is_stable_for_whitespace_and_punctuation_changes() -> None:
    parsed = parse_review_result(
        {
            "summary": "Issues found.",
            "findings": [
                {
                    "file": "SRC/App.py",
                    "line": 12,
                    "severity": "high",
                    "message": "Auth check is skipped!",
                    "confidence": 0.9,
                },
                {
                    "file": "src/app.py",
                    "line": 13,
                    "severity": "high",
                    "message": "auth   check is skipped",
                    "confidence": 0.9,
                },
            ],
        },
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    first = build_fingerprint(
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
        finding=parsed.result.findings[0],
    )
    second = build_fingerprint(
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
        finding=parsed.result.findings[1],
    )

    assert first == second


def test_fingerprint_changes_across_commit_ranges() -> None:
    parsed = parse_review_result(
        {
            "summary": "Issue found.",
            "findings": [
                {
                    "file": "src/app.py",
                    "line": 12,
                    "severity": "high",
                    "message": "Auth check is skipped.",
                    "confidence": 0.9,
                }
            ],
        },
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    finding = parsed.result.findings[0]

    old_fingerprint = build_fingerprint(
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="b" * 40,
        finding=finding,
    )
    new_fingerprint = build_fingerprint(
        provider="github",
        repo_full_name="owner/repo",
        pr_number=10,
        base_sha="a" * 40,
        head_sha="c" * 40,
        finding=finding,
    )

    assert old_fingerprint != new_fingerprint
