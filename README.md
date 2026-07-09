# Review Orchestrator

MVP backend for PR review orchestration. The service owns webhook ingestion,
review-run lifecycle state, and integration points for OpenHands-backed review
sessions.

## Stack

- Python 3.12
- uv
- FastAPI
- SQLAlchemy async
- SQLite for local development
- PostgreSQL via asyncpg for production
- ruff
- pytest

## Development

```bash
uv sync
uv run uvicorn review_orchestrator.main:app --reload
uv run ruff check .
uv run pytest
```

Default database:

```text
sqlite+aiosqlite:///./review_orchestrator.db
```

PostgreSQL example:

```bash
export REVIEW_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/review
```

## MVP Endpoints

- `GET /health`
- `POST /api/v1/webhooks/{provider}`
- `POST /api/v1/review-runs`
- `GET /api/v1/review-runs/{review_run_id}`
- `POST /api/v1/review-runs/{review_run_id}/session/start`
- `POST /api/v1/review-runs/{review_run_id}/session/sync`
- `POST /api/v1/review-runs/{review_run_id}/session/cancel`
- `POST /api/v1/review-runs/{review_run_id}/result`

## OpenHands Integration

Configure the OpenHands App Server integration with:

```bash
export REVIEW_OPENHANDS_BASE_URL=http://localhost:3000
export REVIEW_OPENHANDS_API_KEY=optional-service-token
export REVIEW_OPENHANDS_TIMEOUT_SECONDS=30
```

The Review Orchestrator owns `review_run` state. OpenHands is treated as the
execution backend for a single review session:

1. `session/start` converts an existing `review_run` plus a workspace path into a
   small `ReviewSkillInput` commit-range reference and creates an OpenHands app
   conversation.
2. `session/sync` polls OpenHands start-task/conversation state and maps hard
   failures back to the review run.
3. `session/cancel` marks the run cancelled and best-effort deletes the
   OpenHands conversation.
4. `result` accepts the final OpenHands JSON output, validates it with
   `review_orchestrator.review_results`, stores the summary, and marks the run
   completed.

## Review Skill Contract

The OpenHands review skill receives only a small commit-range reference:

```json
{
  "provider": "github",
  "repo_full_name": "owner/repo",
  "pr_number": 123,
  "base_sha": "abc1234",
  "head_sha": "def5678",
  "workspace_path": "/workspaces/owner-repo/pr-123/def5678",
  "review_mode": "pull_request_review"
}
```

The agent is expected to inspect the local workspace and `base_sha...head_sha`
range with tools instead of receiving large diffs in the prompt. Its final output
uses the minimal publishing schema:

```json
{
  "summary": "Review result for the summary comment.",
  "findings": [
    {
      "file": "src/app.py",
      "line": 42,
      "severity": "high",
      "message": "Publishable line-comment body.",
      "suggestion": "Optional fix direction.",
      "confidence": 0.86
    }
  ]
}
```

`review_orchestrator.review_results` validates this output, marks whether each
finding can be published as a line comment, downgrades unpublishable findings to
summary-only handling, and generates deterministic fingerprints in the
orchestrator instead of trusting model-provided IDs.
