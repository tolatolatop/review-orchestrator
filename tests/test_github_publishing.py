from pathlib import Path

import pytest

from review_orchestrator.comments import (
    publish_github_line_comments,
    publish_github_summary_comment,
)
from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import fetch_changed_files, parse_commentable_lines
from review_orchestrator.models import Finding
from review_orchestrator.review_results import ChangedFile
from tests.factories import ReviewRunCreate, create_review_run


class FakeGitHubClient:
    def __init__(self) -> None:
        self.issue_comments = []
        self.review_comments = []
        self.files = [
            {
                "filename": "src/app.py",
                "patch": "@@ -1,2 +1,3 @@\n old\n+new\n context\n",
            }
        ]

    async def list_pull_request_files(self, repo_full_name, pull_request_number):
        return self.files

    async def list_issue_comments(self, repo_full_name, pull_request_number):
        return self.issue_comments

    async def create_issue_comment(self, repo_full_name, pull_request_number, body):
        self.issue_comments.append(
            type("Comment", (), {"id": "summary-1", "body": body})
        )
        return "summary-1"

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        return comment_id

    async def create_review_comment(
        self,
        repo_full_name,
        pull_request_number,
        *,
        body,
        commit_id,
        path,
        line,
    ):
        self.review_comments.append(
            {"body": body, "commit_id": commit_id, "path": path, "line": line}
        )
        return f"line-{len(self.review_comments)}"


@pytest.fixture
async def session_factory(tmp_path: Path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    engine = create_engine(settings)
    await init_models(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_parse_commentable_lines_from_patch() -> None:
    patch = "@@ -10,2 +20,4 @@\n context\n-old\n+new\n+newer\n"

    assert parse_commentable_lines(patch) == {21, 22}


async def test_fetch_changed_files_uses_github_patch(session_factory) -> None:
    client = FakeGitHubClient()

    changed_files = await fetch_changed_files(
        client,
        repo_full_name="example/repo",
        pull_request_number=42,
    )

    assert changed_files == [ChangedFile(path="src/app.py", commentable_lines={2})]


async def test_publish_summary_and_line_comments(session_factory) -> None:
    client = FakeGitHubClient()
    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        finding = Finding(
            review_run_id=review_run.id,
            fingerprint="finding-1",
            file_path="src/app.py",
            line_start=2,
            severity="high",
            message="Auth check is skipped.",
        )
        session.add(finding)
        await session.commit()

        summary_ref = await publish_github_summary_comment(
            session,
            review_run,
            github_client=client,
            status_text="completed",
            finding_stats={"high": 1},
        )
        line_stats = await publish_github_line_comments(
            session,
            review_run,
            github_client=client,
            changed_files=[ChangedFile(path="src/app.py", commentable_lines={2})],
        )

    assert summary_ref is not None
    assert summary_ref.provider_comment_id == "summary-1"
    assert line_stats["published"] == 1
    assert client.review_comments[0]["path"] == "src/app.py"


async def test_empty_commentable_lines_falls_back_to_summary_only(
    session_factory,
) -> None:
    client = FakeGitHubClient()
    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        finding = Finding(
            review_run_id=review_run.id,
            fingerprint="finding-1",
            file_path="src/app.py",
            line_start=2,
            severity="high",
            message="Auth check is skipped.",
        )
        session.add(finding)
        await session.commit()

        line_stats = await publish_github_line_comments(
            session,
            review_run,
            github_client=client,
            changed_files=[ChangedFile(path="src/app.py", commentable_lines=set())],
        )

    assert line_stats["summary_only"] == 1
    assert line_stats["published"] == 0
    assert client.review_comments == []
