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
