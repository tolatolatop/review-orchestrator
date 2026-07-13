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
cp .env.example .env
uv run uvicorn review_orchestrator.main:app --reload
uv run ruff check .
uv run pytest
```

The BDD/E2E scenario matrix and local-only fixture strategy are documented in
[`docs/bdd-e2e.md`](docs/bdd-e2e.md). The default E2E tests use SQLite, a
temporary git repository, fixture GitHub payloads, and a fake OpenHands client;
optional real PostgreSQL/OpenHands/GitHub integration tests are intentionally not
part of the default command.

Deployment guidance for local, test, and production environments is documented
in [`docs/deployment.md`](docs/deployment.md).

Guidance for extending the GitHub MVP to GitLab, Azure DevOps, Bitbucket,
GitCode, and other providers is documented in
[`docs/platform-extension.md`](docs/platform-extension.md). A Chinese version is
also available at
[`docs/platform-extension_zh.md`](docs/platform-extension_zh.md).

Default database:

```text
sqlite+aiosqlite:///./review_orchestrator.db
```

PostgreSQL example:

```bash
export DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/review
```

## API Reference

The service exposes FastAPI's generated OpenAPI documentation when running
locally:

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- OpenAPI JSON: `GET /openapi.json`

### MVP Endpoints

- `GET /health`
- `POST /api/v1/diagnostics/platform-permissions`
- `POST /api/v1/webhooks/{provider}`
- `POST /api/v1/review-runs`
- `GET /api/v1/review-runs/{review_run_id}`
- `POST /api/v1/review-runs/{review_run_id}/session/start`
- `POST /api/v1/review-runs/{review_run_id}/session/sync`
- `POST /api/v1/review-runs/{review_run_id}/session/cancel`
- `POST /api/v1/review-runs/{review_run_id}/result`
- `POST /api/v1/review-runs/{review_run_id}/retry`
- `POST /api/v1/review-runs/{review_run_id}/cancel`

The operator observability API contract and shared redaction rules are defined
in [`docs/observability-api.md`](docs/observability-api.md).
Secure self-host exposure, authentication boundaries, raw-payload risks, and
the deployment verification checklist are documented in
[`docs/observability-deployment.md`](docs/observability-deployment.md).

`POST /api/v1/review-runs` is idempotent for
`provider + repo_full_name + pull_request_number + head_sha`. A repeated request
returns the latest existing run unless `force=true` is supplied. Failed runs can
be retried through the retry endpoint without `force=true`.

### Platform permission diagnostics

`POST /api/v1/diagnostics/platform-permissions` performs read-only checks with
the configured GitHub or GitLab API token. It verifies repository access and,
when `pull_request_number` is supplied, PR/MR read access. It also reports safe
scope, repository-role, and rate-limit metadata when the provider returns it.
Credentials and upstream response bodies are never included in the response.

```bash
curl -sS http://localhost:8000/api/v1/diagnostics/platform-permissions \
  -H 'Content-Type: application/json' \
  -d '{
    "provider": "github",
    "repo_full_name": "owner/repo",
    "pull_request_number": 123
  }'
```

The overall `status` is `healthy`, `degraded`, or `failed`. A write check can be
`unknown` when a fine-grained provider token does not advertise its grants;
the diagnostic intentionally does not create a probe comment to test writes.

### GitHub Webhooks

`POST /api/v1/webhooks/github` verifies `X-Hub-Signature-256` when
`GITHUB_WEBHOOK_SECRET` is configured, normalizes GitHub pull request and PR
comment events, stores the delivery in the provider inbox, and creates review
runs for review-triggering PR events.

Required headers:

- `X-GitHub-Delivery`
- `X-GitHub-Event`
- `X-Hub-Signature-256` when a webhook secret is configured

Duplicate delivery IDs are idempotent and return the original event status.

### Review Run Status Values

`ReviewRunRead.status` is one of:

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`
- `superseded`

## Runtime Configuration

Infrastructure and secret values are read from environment variables. Use
`.env.example` as the local starting point. Repository-level review behavior is
stored in the database with conservative defaults:

- `review_enabled = true`
- `line_comments_enabled = false`
- `min_severity_for_summary = info`
- `max_findings_per_run = 50`
- `large_pr_file_limit = 100`
- `large_pr_patch_bytes_limit = 500000`
- `auto_retry_invalid_agent_result = false`
- `auto_retry_infra_failure = true`
- `default_review_skill = code-review`
- `default_review_profile = default`

Workspace storage defaults:

- `WORKSPACE_ROOT=./.workspaces`
- `GIT_CACHE_ROOT=./.git-cache`

OpenHands App Server integration:

- `OPENHANDS_BASE_URL=http://localhost:3000`
- self-host OpenHands UI: `http://127.0.0.1:${OPENHANDS_FRONTEND_PORT:-3000}`
- `OPENHANDS_API_TOKEN=optional-service-token`
- `OPENHANDS_TIMEOUT_SECONDS=30`

### OpenHands Integration

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

### Workspace MVP Contract

The Workspace module only prepares local Git working directories and manages
their lifecycle. It does not generate diff schemas, publish PR checks, cache
review results, or manage dependency/build caches.

Workspace endpoints:

- `POST /api/v1/workspaces/prepare`
- `GET /api/v1/workspaces/{workspace_id}`
- `POST /api/v1/workspaces/{workspace_id}/lease`
- `POST /api/v1/workspace-leases/{lease_id}/release`
- `POST /api/v1/workspaces/{workspace_id}/cleanup`
- `POST /api/v1/workspaces/cleanup/pr`
- `POST /api/v1/workspaces/cleanup/expired`

Prepare a workspace:

```json
{
  "provider": "github",
  "repository": {
    "full_name": "owner/repo",
    "clone_url": "https://github.com/owner/repo.git"
  },
  "pull_request": {
    "number": 123,
    "base_sha": "abc1234",
    "head_sha": "def5678",
    "is_fork": false
  },
  "auth": {
    "token_ref": "GITHUB_INSTALLATION_TOKEN"
  },
  "options": {
    "use_git_cache": true,
    "force_refresh": false,
    "enable_submodules": false,
    "enable_lfs": false
  }
}
```

The response returns `workspace_path`, `base_sha`, and `head_sha`. Callers can run
their own diff commands from that path:

```bash
git diff {base_sha}...{head_sha}
git diff --name-status {base_sha}...{head_sha}
```

Workspace paths are isolated by provider, repository hash, PR number, and head
SHA. If `use_git_cache` is enabled, Workspace maintains a repo-level bare mirror
under `GIT_CACHE_ROOT` to speed up repeated prepare calls.

`workspace_id` is path-safe and uses the repository hash:

```text
github:{repo_hash}:pr:{pr_number}:head:{head_sha}
```

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
