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

## API Reference

The service exposes FastAPI's generated OpenAPI documentation when running
locally:

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- OpenAPI JSON: `GET /openapi.json`

### Health Check

```http
GET /health
```

Response:

```json
{
  "status": "ok"
}
```

### Accept Provider Webhook

```http
POST /api/v1/webhooks/{provider}
Content-Type: application/json
```

Path parameters:

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `provider` | string | yes | Source provider name, for example `github`. |

Request body:

Any JSON object. In the MVP this endpoint acknowledges the payload and leaves
provider authentication, event normalization, and enqueueing for the next
implementation step.

Example request:

```json
{
  "action": "opened",
  "pull_request": {
    "number": 42
  }
}
```

Response `200 OK`:

```json
{
  "accepted": true,
  "provider": "github"
}
```

### Create Review Run

```http
POST /api/v1/review-runs
Content-Type: application/json
```

Creates a queued review run for a pull request commit range. The create operation
is idempotent for the same `provider`, `repository`, `pull_request_number`, and
`head_sha`; repeating the same request returns the existing run.

Request body:

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `provider` | string | yes | 1-64 chars | Provider name, for example `github`. |
| `repository` | string | yes | 1-512 chars | Repository full name, for example `owner/repo`. |
| `pull_request_number` | integer | yes | `> 0` | Pull request number. |
| `base_sha` | string or null | no | max 80 chars | Base commit SHA. |
| `head_sha` | string | yes | 7-80 chars | Head commit SHA to review. |

Example request:

```json
{
  "provider": "github",
  "repository": "owner/repo",
  "pull_request_number": 42,
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

Response `201 Created`:

```json
{
  "id": "6d41f5d2-0b65-4dc7-b02e-20ac8a68818e",
  "provider": "github",
  "repository": "owner/repo",
  "pull_request_number": 42,
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "status": "queued",
  "summary_comment_id": null,
  "workspace_path": null,
  "error": null,
  "created_at": "2026-07-09T10:00:00Z",
  "updated_at": "2026-07-09T10:00:00Z"
}
```

Validation failure response `422 Unprocessable Entity` uses FastAPI's standard
validation error format.

### Get Review Run

```http
GET /api/v1/review-runs/{review_run_id}
```

Path parameters:

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `review_run_id` | string | yes | Review run UUID returned by create. |

Response `200 OK` uses the same `ReviewRunRead` shape returned by create.

Response `404 Not Found` is returned when the run ID does not exist.

### Review Run Status Values

`ReviewRunRead.status` is one of:

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`
- `superseded`

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

### Review Result Parser

`parse_review_result` is an internal contract used by the orchestrator after an
OpenHands session finishes. It accepts either a JSON string or decoded object and
returns:

```json
{
  "result": {
    "summary": "Review result for the summary comment.",
    "findings": []
  },
  "findings": [
    {
      "finding": {
        "file": "src/app.py",
        "line": 42,
        "severity": "high",
        "message": "Publishable line-comment body.",
        "suggestion": "Optional fix direction.",
        "confidence": 0.86
      },
      "fingerprint": "sha256:...",
      "publish_as_line_comment": true,
      "reason": null
    }
  ],
  "summary_only_findings": []
}
```

Parser inputs:

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `raw_output` | string or object | yes | Agent final JSON output. |
| `changed_files` | list | no | Commentable file/line map from the provider diff. |
| `provider` | string | yes | Provider name used for fingerprinting. |
| `repo_full_name` | string | yes | Repository full name used for fingerprinting. |
| `pr_number` | integer | yes | Pull request number used for fingerprinting. |
| `base_sha` | string | yes | Base commit SHA used for fingerprinting. |
| `head_sha` | string | yes | Head commit SHA used for fingerprinting. |

`changed_files` items use this shape:

```json
{
  "path": "src/app.py",
  "commentable_lines": [42, 43]
}
```

If `changed_files` is provided, a finding is publishable as a line comment only
when `file` exists in the changed file map and `line` is included in
`commentable_lines`. Otherwise the finding is kept in `summary_only_findings`
with a reason such as `file_not_changed` or `line_not_commentable`.

Parser errors are raised as `ReviewResultError` and can be serialized with
`to_dict()`:

```json
{
  "error_code": "schema_error",
  "message": "Input should be 'critical', 'high', 'medium' or 'low'",
  "finding_index": null,
  "retryable": true
}
```

Error codes:

- `json_parse_error`: the agent output is not a JSON object.
- `schema_error`: required fields, enum values, lengths, or confidence bounds are invalid.
- `location_error`: reserved for provider diff-location validation failures.

Fingerprint generation is deterministic and performed by the orchestrator from:

- provider
- repository full name
- pull request number
- base/head commit SHA
- normalized file path
- severity
- normalized finding message
