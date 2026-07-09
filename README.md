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
