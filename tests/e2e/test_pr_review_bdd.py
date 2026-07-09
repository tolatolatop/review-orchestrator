from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select

from review_orchestrator.comments import (
    build_summary_comment_body,
    ensure_line_comment_ref,
    upsert_summary_comment_ref,
)
from review_orchestrator.models import (
    Finding,
    ProviderEventInbox,
    PullRequestContext,
    ReviewCommentRef,
    ReviewRun,
    Workspace,
)
from review_orchestrator.reconciliation import persist_and_reconcile_findings
from review_orchestrator.review_results import ChangedFile, parse_review_result
from review_orchestrator.worker import acquire_next_review_run

from .helpers import (
    FakeOpenHandsClient,
    create_temp_pull_request_repo,
    given_github_pr_webhook,
    load_json_fixture,
    make_e2e_client,
    when_github_delivers,
)


def test_p0_pr_opened_runs_review_and_reconciles_published_state(
    tmp_path,
) -> None:
    repo = create_temp_pull_request_repo(tmp_path)
    payload = given_github_pr_webhook("github_pr_opened.json", repo)
    raw_result = load_json_fixture("openhands_review_result.json")
    changed_files = load_json_fixture("changed_files.json")
    fake_openhands = FakeOpenHandsClient()

    with make_e2e_client(tmp_path) as client:
        client.app.state.openhands_client = fake_openhands

        accepted = when_github_delivers(
            client,
            payload,
            delivery_id="delivery-p0-opened",
        )
        duplicate = when_github_delivers(
            client,
            payload,
            delivery_id="delivery-p0-opened",
        )
        acquired = asyncio.run(
            _when_worker_acquires_next_run(client.app.state.session_factory)
        )

        workspace_response = client.post(
            "/api/v1/workspaces/prepare",
            json={
                "provider": "github",
                "repository": {
                    "full_name": "example/repo",
                    "clone_url": repo.clone_url,
                },
                "pull_request": {
                    "number": 42,
                    "base_sha": repo.base_sha,
                    "head_sha": repo.head_sha,
                    "is_fork": False,
                },
                "options": {
                    "use_git_cache": False,
                    "force_refresh": False,
                    "enable_submodules": False,
                    "enable_lfs": False,
                },
            },
        )
        assert workspace_response.status_code == 200, workspace_response.text
        workspace = workspace_response.json()

        start_response = client.post(
            f"/api/v1/review-runs/{accepted['review_run_id']}/session/start",
            json={"workspace_path": workspace["workspace_path"]},
        )
        assert start_response.status_code == 200, start_response.text

        sync_response = client.post(
            f"/api/v1/review-runs/{accepted['review_run_id']}/session/sync",
        )
        assert sync_response.status_code == 200, sync_response.text

        collect_response = client.post(
            f"/api/v1/review-runs/{accepted['review_run_id']}/result",
            json={"raw_output": raw_result, "changed_files": changed_files},
        )
        assert collect_response.status_code == 200, collect_response.text

        published = asyncio.run(
            _then_publish_and_read_review_artifacts(
                client.app.state.session_factory,
                accepted["review_run_id"],
                raw_result,
                changed_files,
            )
        )

    assert accepted["internal_event"] == "pr_opened"
    assert accepted["status"] == "queued"
    assert accepted["review_run_id"] is not None
    assert duplicate["duplicate"] is True
    assert duplicate["review_run_id"] == accepted["review_run_id"]
    assert acquired["id"] == accepted["review_run_id"]
    assert acquired["lock_owner"] == "e2e-worker"
    assert workspace["status"] == "ready"
    assert start_response.json()["status"] == "running"
    assert start_response.json()["workspace_path"] == workspace["workspace_path"]
    assert sync_response.json()["status"] == "running"
    assert collect_response.json()["review_run"]["status"] == "completed"
    assert collect_response.json()["parsed"]["findings"][0][
        "publish_as_line_comment"
    ] is True
    assert collect_response.json()["parsed"]["findings"][1][
        "publish_as_line_comment"
    ] is False
    assert collect_response.json()["parsed"]["summary_only_findings"][0][
        "file"
    ] == "src/config.py"

    assert (
        fake_openhands.started_inputs[0].workspace_path
        == workspace["workspace_path"]
    )
    assert fake_openhands.started_inputs[0].base_sha == repo.base_sha
    assert fake_openhands.started_inputs[0].head_sha == repo.head_sha

    assert published["context_latest_review_run_id"] == accepted["review_run_id"]
    assert published["delivery_count"] == 1
    assert published["workspace_status"] == "ready"
    assert published["finding_stats"] == {"existing": 0, "new": 2, "resolved": 0}
    assert published["finding_count"] == 2
    assert published["summary_comment_count"] == 1
    assert published["line_comment_count"] == 1
    assert published["line_comment_deduped"] is True
    assert published["summary_comment_id"] == "summary-pr-42"


def test_p0_given_pr_synchronize_when_new_head_arrives_then_old_run_is_superseded(
    tmp_path,
) -> None:
    repo = create_temp_pull_request_repo(tmp_path)
    opened_payload = given_github_pr_webhook("github_pr_opened.json", repo)
    sync_payload = given_github_pr_webhook(
        "github_pr_synchronize.json",
        repo,
        head_sha=repo.second_head_sha,
    )

    with make_e2e_client(tmp_path) as client:
        opened = when_github_delivers(
            client,
            opened_payload,
            delivery_id="delivery-p0-opened-before-sync",
        )
        synchronized = when_github_delivers(
            client,
            sync_payload,
            delivery_id="delivery-p0-sync",
        )
        old_run = client.get(f"/api/v1/review-runs/{opened['review_run_id']}").json()
        new_run = client.get(
            f"/api/v1/review-runs/{synchronized['review_run_id']}"
        ).json()

    assert synchronized["internal_event"] == "pr_updated"
    assert synchronized["review_run_id"] != opened["review_run_id"]
    assert old_run["status"] == "superseded"
    assert old_run["failure_code"] == "superseded_by_new_head"
    assert old_run["superseded_by_review_run_id"] == synchronized["review_run_id"]
    assert new_run["status"] == "queued"
    assert new_run["head_sha"] == repo.second_head_sha


async def _when_worker_acquires_next_run(session_factory) -> dict[str, Any]:
    async with session_factory() as session:
        review_run = await acquire_next_review_run(session, worker_id="e2e-worker")
        assert review_run is not None
        return {
            "id": review_run.id,
            "status": review_run.status,
            "stage": review_run.stage,
            "lock_owner": review_run.lock_owner,
        }


async def _then_publish_and_read_review_artifacts(
    session_factory,
    review_run_id: str,
    raw_result: dict[str, Any],
    changed_files_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    async with session_factory() as session:
        review_run = await session.get(ReviewRun, review_run_id)
        assert review_run is not None

        reviewing_body = build_summary_comment_body(
            review_run,
            status_text="reviewing",
        )
        await upsert_summary_comment_ref(
            session,
            review_run,
            provider_comment_id="summary-pr-42",
            body=reviewing_body,
        )

        parsed = parse_review_result(
            raw_result,
            changed_files=[
                ChangedFile.model_validate(item) for item in changed_files_payload
            ],
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
            pr_number=review_run.pull_request_number,
            base_sha=review_run.base_sha or "",
            head_sha=review_run.head_sha,
        )
        stats = await persist_and_reconcile_findings(session, review_run, parsed)

        completed_body = build_summary_comment_body(
            review_run,
            status_text="completed",
            finding_stats=stats.__dict__,
        )
        summary_ref = await upsert_summary_comment_ref(
            session,
            review_run,
            provider_comment_id="summary-pr-42",
            body=completed_body,
        )

        publishable_finding = (
            await session.execute(
                select(Finding).where(
                    Finding.review_run_id == review_run.id,
                    Finding.file_path == "src/auth.py",
                    Finding.line_start == 2,
                )
            )
        ).scalar_one()
        first_line_ref, first_created = await ensure_line_comment_ref(
            session,
            review_run,
            publishable_finding,
            provider_comment_id="line-src-auth-py-2",
            body=publishable_finding.message,
        )
        second_line_ref, second_created = await ensure_line_comment_ref(
            session,
            review_run,
            publishable_finding,
            provider_comment_id="line-src-auth-py-2-duplicate",
            body=publishable_finding.message,
        )

        context = (
            await session.execute(
                select(PullRequestContext).where(
                    PullRequestContext.repo_full_name == "example/repo",
                    PullRequestContext.pull_request_number == 42,
                )
            )
        ).scalar_one()
        delivery_count = (
            await session.execute(
                select(func.count(ProviderEventInbox.id)).where(
                    ProviderEventInbox.delivery_id == "delivery-p0-opened"
                )
            )
        ).scalar_one()
        workspace_status = (
            await session.execute(
                select(Workspace.status).where(
                    Workspace.repository == "example/repo",
                    Workspace.pull_request_number == 42,
                    Workspace.head_sha == review_run.head_sha,
                )
            )
        ).scalar_one()
        finding_count = (
            await session.execute(
                select(func.count(Finding.id)).where(
                    Finding.review_run_id == review_run.id
                )
            )
        ).scalar_one()
        summary_comment_count = (
            await session.execute(
                select(func.count(ReviewCommentRef.id)).where(
                    ReviewCommentRef.comment_type == "summary",
                    ReviewCommentRef.pull_request_number == 42,
                )
            )
        ).scalar_one()
        line_comment_count = (
            await session.execute(
                select(func.count(ReviewCommentRef.id)).where(
                    ReviewCommentRef.comment_type == "line",
                    ReviewCommentRef.pull_request_number == 42,
                )
            )
        ).scalar_one()

        return {
            "context_latest_review_run_id": context.latest_review_run_id,
            "delivery_count": delivery_count,
            "workspace_status": workspace_status,
            "finding_stats": {
                "existing": stats.existing,
                "new": stats.new,
                "resolved": stats.resolved,
            },
            "finding_count": finding_count,
            "summary_comment_count": summary_comment_count,
            "line_comment_count": line_comment_count,
            "line_comment_deduped": (
                first_created
                and not second_created
                and first_line_ref.id == second_line_ref.id
            ),
            "summary_comment_id": summary_ref.provider_comment_id,
        }
