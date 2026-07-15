"""Permanent, redacted Agent Session and Task metadata archive."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.domain.models import (
    AgentTask,
    ReviewRun,
    SessionArchive,
    Task,
    TaskAttempt,
    utc_now,
)
from review_orchestrator.infrastructure.observability import (
    is_sensitive_key,
    redact_text,
    redact_value,
)
from review_orchestrator.integrations.pi_agent import PiAgentSession

MAX_WORKSPACE_DIFF_BYTES = 500_000


async def archive_agent_session(
    session: AsyncSession,
    task: Task,
    runtime_session: PiAgentSession,
) -> SessionArchive:
    """Upsert the permanent explanation record for one Runtime Session."""

    attempt = await _get_or_create_attempt(session, task, runtime_session)
    runtime_status = _runtime_status(runtime_session)
    raw_archive = runtime_session.session_archive or {
        "session_id": runtime_session.id,
        "status": runtime_status,
        "stage": runtime_session.stage,
        "events": [event.model_dump(mode="json") for event in runtime_session.events],
        "result": runtime_session.result,
        "session_file": runtime_session.session_file,
    }
    workspace_diff, diff_truncated = await _workspace_diff(
        getattr(task, "workspace_path", None)
    )
    archived = (
        await session.execute(
            select(SessionArchive).where(
                SessionArchive.task_id == task.id,
                SessionArchive.agent_run_id == runtime_session.id,
            )
        )
    ).scalar_one_or_none()
    if archived is None:
        archived = SessionArchive(
            task_id=task.id,
            task_attempt_id=attempt.id,
            agent_run_id=runtime_session.id,
            session_json={},
            task_metadata_json={},
        )
    archived.task_attempt_id = attempt.id
    archived.session_json = _redact_archive_value(raw_archive)
    archived.task_metadata_json = _redact_archive_value(
        _task_metadata(task, runtime_session)
    )
    archived.workspace_diff = (
        None if workspace_diff is None else redact_text(workspace_diff)
    )
    archived.workspace_diff_truncated = diff_truncated
    archived.redaction_version = "1"
    archived.updated_at = utc_now()
    session.add(archived)
    await session.flush()
    return archived


async def _get_or_create_attempt(
    session: AsyncSession,
    task: Task,
    runtime_session: PiAgentSession,
) -> TaskAttempt:
    attempt = (
        await session.execute(
            select(TaskAttempt).where(
                TaskAttempt.task_id == task.id,
                TaskAttempt.agent_run_id == runtime_session.id,
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        attempt_no = int(
            (
                await session.execute(
                    select(func.coalesce(func.max(TaskAttempt.attempt_no), 0)).where(
                        TaskAttempt.task_id == task.id
                    )
                )
            ).scalar_one()
        ) + 1
        attempt = TaskAttempt(
            task_id=task.id,
            attempt_no=attempt_no,
            agent_run_id=runtime_session.id,
            workspace_path=getattr(task, "workspace_path", None),
            resolved_preset_json=task.resolved_preset_json,
        )
    runtime_status = _runtime_status(runtime_session)
    attempt.status = runtime_status
    attempt.stage = runtime_session.stage
    attempt.agent_run_id = runtime_session.id
    attempt.workspace_path = getattr(task, "workspace_path", None)
    attempt.resolved_preset_json = task.resolved_preset_json
    archive_stats = (runtime_session.session_archive or {}).get("stats")
    if isinstance(archive_stats, dict):
        attempt.usage_json = _redact_archive_value(archive_stats)
    if runtime_status in {"completed", "failed", "cancelled"}:
        attempt.completed_at = utc_now()
        attempt.error_message = runtime_session.error
        attempt.failure_category = (
            None if runtime_status == "completed" else "agent_failure"
        )
    session.add(attempt)
    await session.flush()
    return attempt


def _runtime_status(runtime_session: PiAgentSession) -> str:
    value = runtime_session.status
    return value.value if hasattr(value, "value") else str(value)


def _redact_archive_value(value: Any) -> Any:
    """Keep numeric token counters while still redacting credential-shaped data."""

    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text) and not isinstance(
                item, (bool, int, float)
            ):
                redacted[key_text] = redact_value({key_text: item})[key_text]
            else:
                redacted[key_text] = _redact_archive_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_archive_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _task_metadata(task: Task, runtime_session: PiAgentSession) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "task": {
            "id": task.id,
            "kind": task.kind,
            "capability_id": task.capability_id,
            "status": task.status,
            "stage": task.stage,
            "execution_status": task.execution_status,
            "delivery_status": task.delivery_status,
            "queue": task.queue,
            "priority": task.priority,
            "resource_class": task.resource_class,
            "resource_context": task.resource_context_json,
            "resolved_preset": task.resolved_preset_json,
        },
        "runtime": {
            "agent_run_id": runtime_session.id,
            "agent_id": runtime_session.agent_id,
            "agent_version": runtime_session.agent_version,
            "provider": runtime_session.provider,
            "model": runtime_session.model,
            "thinking_level": runtime_session.thinking_level,
            "skills": runtime_session.skills,
            "tools": runtime_session.tools,
            "execution_limits": runtime_session.execution_limits,
            "execution_counters": runtime_session.execution_counters,
            "execution_environment": runtime_session.execution_environment,
            "resolved_preset": runtime_session.resolved_preset,
        },
    }
    if isinstance(task, ReviewRun):
        metadata["subject"] = {
            "provider": task.provider,
            "repository": task.repo_full_name,
            "pull_request_number": task.pull_request_number,
            "base_sha": task.base_sha,
            "head_sha": task.head_sha,
        }
    elif isinstance(task, AgentTask):
        metadata["subject"] = {
            "provider": task.provider,
            "repository": task.repo_full_name,
            "pull_request_number": task.pull_request_number,
            "head_sha": task.head_sha,
            "source_comment_id": task.source_comment_id,
            "source_author_login": task.source_author_login,
            "command_text": task.command_text,
        }
    return metadata


async def _workspace_diff(workspace_path: str | None) -> tuple[str | None, bool]:
    if not workspace_path:
        return None, False
    return await asyncio.to_thread(_capture_workspace_diff, workspace_path)


def _capture_workspace_diff(workspace_path: str) -> tuple[str | None, bool]:
    workspace = Path(workspace_path)
    if not workspace.is_dir() or not (workspace / ".git").exists():
        return None, False
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "HEAD",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    content = result.stdout
    remaining = MAX_WORKSPACE_DIFF_BYTES - min(
        len(content), MAX_WORKSPACE_DIFF_BYTES
    )
    untracked_truncated = False
    if remaining > 0:
        untracked, untracked_truncated = _capture_untracked_diffs(
            workspace, remaining
        )
        content += untracked
    truncated = len(content) > MAX_WORKSPACE_DIFF_BYTES or untracked_truncated
    if truncated:
        content = content[:MAX_WORKSPACE_DIFF_BYTES]
    return content.decode("utf-8", errors="replace"), truncated


def _capture_untracked_diffs(workspace: Path, limit: int) -> tuple[bytes, bool]:
    try:
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return b"", False
    content = bytearray()
    truncated = False
    for raw_path in listed.stdout.split(b"\0"):
        if not raw_path:
            continue
        if len(content) >= limit:
            truncated = True
            break
        path = raw_path.decode("utf-8", errors="surrogateescape")
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "diff",
                    "--no-index",
                    "--binary",
                    "--",
                    "/dev/null",
                    path,
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        remaining = limit - len(content)
        if len(result.stdout) > remaining:
            truncated = True
        content.extend(result.stdout[:remaining])
    return bytes(content), truncated
