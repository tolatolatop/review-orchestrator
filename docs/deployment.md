# Deployment Guide

This guide covers deploying Review Orchestrator with OpenHands App Server,
GitHub webhooks, persistent database storage, and local workspace storage.

## Topology

The smallest useful deployment has these components:

- Review Orchestrator: the FastAPI service in this repository.
- OpenHands App Server: the execution backend used by review sessions.
- Database: SQLite for local development, PostgreSQL for test and production.
- Workspace storage: local or mounted disk for checked-out pull request repos and
  optional bare git mirrors.
- GitHub webhook: sends pull request and PR comment events to Review
  Orchestrator.

In local development, all components can run on one machine with SQLite and
local directories. In production, run Review Orchestrator and OpenHands as
separate services, use PostgreSQL, keep workspace storage on durable mounted
disk, terminate TLS at a proxy or load balancer, and inject secrets through the
runtime environment or secret manager.

## Prerequisites

- Python 3.12.
- `uv`.
- `git` available on the Review Orchestrator host.
- Network access from Review Orchestrator to GitHub, PostgreSQL, and OpenHands.
- Network access from GitHub to the public webhook URL.
- An OpenHands App Server reachable from Review Orchestrator.
- A GitHub App or webhook configuration with a shared webhook secret.

## Configuration

Copy the template and fill in local values:

```bash
cp .env.example .env
```

Review Orchestrator reads environment variables directly and also loads `.env`
from the current working directory. Do not commit `.env`, GitHub App private
keys, webhook secrets, installation tokens, OpenHands API tokens, or database
passwords.

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `APP_ENV` | `local` | no | Deployment label used by operators. |
| `LOG_LEVEL` | `INFO` | no | Logging verbosity for the process manager or ASGI server. |
| `HOST` | `0.0.0.0` | no | Bind host for local `uvicorn` examples. |
| `PORT` | `8000` | no | Bind port for local `uvicorn` examples. |
| `REVIEW_LOCAL_PORT` | `18000` | no | Self-host FastAPI port bound only to `127.0.0.1`; trusted local requests bypass Nginx token checks. |
| `REVIEW_PROXY_PORT` | `18080` | no | Self-host Nginx port for remote or proxied operator access. |
| `REVIEW_PROXY_TOKEN_ENABLED` | `true` | no | Enables Nginx token validation. Set to `false` only when the proxy is intentionally open: every route and feature then accepts requests without a token. |
| `REVIEW_PROXY_TOKEN` | empty | when token validation is enabled | Token enforced by Nginx on protected routes; FastAPI does not validate it. An empty token fails closed while validation is enabled. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./review_orchestrator.db` | yes | SQLAlchemy async URL. `sqlite:///`, `postgres://`, and `postgresql://` are normalized to async drivers. |
| `GITHUB_WEBHOOK_SECRET` | empty | production yes | Shared webhook secret. If unset, signature verification is skipped for local development. |
| `GITHUB_APP_ID` | empty | with App auth | GitHub App ID. Configure it together with `GITHUB_PRIVATE_KEY_PATH`. |
| `GITHUB_PRIVATE_KEY_PATH` | empty | with App auth | Read-only filesystem path to the App private key PEM. Self-host Compose uses `/run/secrets/github-app.pem`. |
| `GITHUB_INSTALLATION_ID` | empty | no | Optional fixed Installation ID for a single Installation. When empty, the service resolves and caches the Installation independently for each repository. |
| `GITHUB_API_BASE_URL` | `https://api.github.com` | no | GitHub API base URL. Override for GitHub Enterprise. |
| `GITHUB_INSTALLATION_TOKEN` | empty | legacy auth only | Static token compatibility mode. Leave it empty when App ID and private key are configured; App mode signs and refreshes Installation Tokens automatically. |
| `REVIEW_BOT_LOGIN` | `review-agent` | no | Bot login recognized in PR comments such as `@review-agent`. |
| `GITLAB_WEBHOOK_SECRET` | empty | production yes for GitLab | Shared token checked against `X-Gitlab-Token`. |
| `GITLAB_API_BASE_URL` | `https://gitlab.com/api/v4` | no | GitLab API base URL. Override for self-managed GitLab. |
| `GITLAB_API_TOKEN` | empty | for GitLab MR lookup and notes | Token used by the worker for MR details, changes, and summary note publishing. |
| `PLATFORM_DIAGNOSTICS_TIMEOUT_SECONDS` | `10` | no | Timeout for read-only GitHub/GitLab permission diagnostic requests. |
| `OPENHANDS_BASE_URL` | `http://localhost:3000` | yes | Base URL for OpenHands App Server. |
| `OPENHANDS_UI_BASE_URL` | empty | no | Operator-facing OpenHands UI base URL used by observability responses to build conversation links. |
| `OPENHANDS_FRONTEND_PORT` | `3000` | no | Local-only host port for the OpenHands UI/API in `docker-compose.self_host.yaml`; bound to `127.0.0.1`. |
| `OPENHANDS_API_TOKEN` | empty | if OpenHands requires auth | Bearer token sent to OpenHands. |
| `OPENHANDS_REVIEW_SKILL` | `code-review` | no | Review skill name stored with repository review defaults. |
| `OPENHANDS_REVIEW_PROFILE` | `default` | no | Review profile stored with repository review defaults. |
| `OPENHANDS_TIMEOUT_SECONDS` | `30` | no | HTTP timeout for OpenHands API calls. |
| `WORKSPACE_ROOT` | `./.workspaces` | yes | Root directory for prepared pull request workspaces. |
| `GIT_CACHE_ROOT` | `./.git-cache` | no | Root directory for bare mirror caches when `use_git_cache` is enabled. |
| `REVIEW_RUN_TIMEOUT_SECONDS` | `1800` | no | Hard timeout used by worker timeout logic. |
| `REVIEW_RUN_SOFT_TIMEOUT_SECONDS` | `900` | no | Soft timeout used by worker timeout logic. |
| `WORKER_POLL_INTERVAL_SECONDS` | `5` | no | Delay between idle worker polling passes. |
| `WORKER_LOCK_SECONDS` | `300` | no | Per-pass task lock lease. Expired running locks can be reacquired. |
| `RETRY_MAX_ATTEMPTS` | `2` | no | Retry budget for failed review runs. |
| `RETRY_INITIAL_DELAY_SECONDS` | `60` | no | Initial retry delay in seconds. |

## Database

SQLite is the default and is intended for local development:

```bash
export DATABASE_URL=sqlite+aiosqlite:///./review_orchestrator.db
```

Use PostgreSQL for shared test and production deployments:

```bash
export DATABASE_URL=postgresql+asyncpg://review:change-me@postgres.example.com:5432/review_orchestrator
```

The service currently initializes tables during FastAPI startup with SQLAlchemy
metadata. There is no separate migration command in this repository yet. For
production upgrades, back up the database before deploying new code and verify
schema changes in a staging environment first.

## Local Deployment

Install dependencies, create the local environment file, and start the service:

```bash
uv sync
cp .env.example .env
uv run uvicorn review_orchestrator.main:app --host 0.0.0.0 --port 8000 --reload
```

Or run the local Docker Compose profile, which builds Review Orchestrator, keeps
SQLite/workspace data in a named volume, and connects to an OpenHands App Server
on the host by default:

```bash
cp .env.example .env
docker compose -f docker-compose.yaml up --build
```

Health check:

```bash
curl -fsS http://localhost:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Open API docs:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

For local GitHub webhook testing, expose the service with a tunnel and use the
tunnel URL as the webhook callback:

```text
https://<tunnel-host>/api/v1/webhooks/github
```

## Production Deployment

Run the app with an ASGI server under a process manager or container platform.
One direct API command is:

```bash
uv run uvicorn review_orchestrator.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
```

Run at least one worker process alongside the API; webhooks only enqueue work,
while the worker prepares workspaces, starts OpenHands, polls for results, and
publishes provider comments:

```bash
uv run review-orchestrator-worker
```

For a single-host deployment with PostgreSQL and OpenHands, use:

```bash
cp .env.example .env
docker compose -f docker-compose.self_host.yaml up --build -d
```

Before starting the self-host stack, edit `.env` and set production secrets such
as `REVIEW_PROXY_TOKEN`, `GITHUB_WEBHOOK_SECRET`, the GitHub App settings below,
and `OPENHANDS_API_TOKEN` when your OpenHands deployment requires one. Override
`POSTGRES_PASSWORD` in `.env` or the shell; the compose default is only suitable
for local testing.

The same PostgreSQL container also hosts OpenHands, but OpenHands uses a
separate logical database and login. Set `OPENHANDS_DB_PASSWORD` to a different
strong password in production. `openhands-db-init` provisions or updates the
role and database on every deployment, including deployments with an existing
`postgres_data` volume; it does not rely on first-boot
`/docker-entrypoint-initdb.d` behavior.

Before OpenHands starts, `openhands-db-migrate` applies the OpenHands Alembic
migrations and checks the persistent `openhands_state` volume for the legacy
`/.openhands/openhands.db`. When it exists, the service:

1. creates a content-addressed
   `/.openhands/openhands.db.pre-postgres-<sha256>.bak` with the SQLite backup
   API without overwriting an earlier cutover snapshot;
2. copies the six OpenHands application tables to PostgreSQL in one
   transaction;
3. validates row counts before allowing OpenHands to start.

The migration is restart-safe. It validates rows with matching primary keys,
copies only missing legacy rows, and preserves PostgreSQL-only rows from an
earlier trial or cutover. If a matching primary key contains different values,
it fails closed and prevents OpenHands from starting instead of overwriting
ambiguous state. Keep the backup through at least one successful review and the
deployment observation period. `settings.json`, `secrets.json`, `.keys`, and
other file-backed state remain on `openhands_state`; do not remove that volume
after switching databases.

Before the first cutover, stop OpenHands and the orchestrator worker so SQLite
cannot receive new writes while the final backup and migration run:

```bash
docker compose -f docker-compose.self_host.yaml stop \
  review-orchestrator-worker review-orchestrator openhands
docker compose -f docker-compose.self_host.yaml up --build -d
```

The migration service also contains narrow compatibility handling for the
OpenHands 1.8 PostgreSQL migrations: it creates the enum omitted by migration
002 and applies migration 010's concurrent index outside a transaction. These
steps are idempotent and can be removed after the pinned OpenHands image ships
equivalent fixed migrations.

Inspect the one-shot services and verify the selected backend with:

```bash
docker compose -f docker-compose.self_host.yaml logs openhands-db-init openhands-db-migrate
docker compose -f docker-compose.self_host.yaml exec openhands \
  /app/.venv/bin/python -c \
  "from openhands.app_server.config import get_global_config; print(get_global_config().db_session.host)"
```

The second command must print `postgres`. To roll back before new PostgreSQL
state is accepted, stop OpenHands, remove its `DB_*` settings from the compose
service, and restore the retained SQLite backup. Do not run SQLite and
PostgreSQL-backed OpenHands concurrently against the same deployment.

The self-host compose file runs separate `review-orchestrator` API and
`review-orchestrator-worker` services. It publishes Nginx as the remote Review
Orchestrator entrypoint and also binds FastAPI to a separate host-loopback port
for trusted local access without a proxy token. Neither FastAPI nor OpenHands
is bound to a non-loopback host interface:

```bash
open http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/reviews/
open http://127.0.0.1:${OPENHANDS_FRONTEND_PORT:-3000}
```

Use `REVIEW_LOCAL_PORT` to move the tokenless, loopback-only FastAPI port. Do
not change its Compose binding from `127.0.0.1` to `0.0.0.0`; local access
intentionally relies on the host boundary instead of application auth.
Use `OPENHANDS_FRONTEND_PORT` to move this local-only OpenHands UI/API port.
The legacy `OPENHANDS_PORT` variable is still accepted as a fallback when
`OPENHANDS_FRONTEND_PORT` is not set.

Requests sent through the Nginx port outside `/health` and
`/api/v1/webhooks/github` must include the fixed token:

```bash
curl -fsS http://localhost:${REVIEW_PROXY_PORT:-18080}/api/v1/review-runs/<review_run_id> \
  -H "X-Review-Token: ${REVIEW_PROXY_TOKEN}"
```

For manual browser access, `?token=<REVIEW_PROXY_TOKEN>` is also accepted, but
the header form is preferred because query strings are commonly stored in
browser history, proxy logs, and analytics systems. GitHub webhooks cannot send
this custom header, so `/api/v1/webhooks/github` is allowed through Nginx and
must be protected by setting `GITHUB_WEBHOOK_SECRET`.

To intentionally expose every Review Orchestrator feature without the Nginx
token gate, set `REVIEW_PROXY_TOKEN_ENABLED=false` and restart the Nginx
service. `REVIEW_PROXY_TOKEN` may be empty in this mode, and neither the header
nor query parameter is required on any route. This is a fully open proxy, not a
development-only bypass; restrict it to a trusted network or place another
authentication layer in front of it. Any value other than exactly `false`
keeps validation enabled, and an empty token then rejects protected requests.

The observability endpoints contain private repository and execution metadata.
Read the dedicated [secure observability deployment and verification guide](observability-deployment.md)
before exposing an operator UI or API. It documents the current route set,
edge-authentication boundary, raw-payload behavior, redaction limitations, and
the API/UI/webhook regression checklist.

Recommended production settings:

- Set `APP_ENV=production`.
- Use PostgreSQL with TLS and regular backups.
- Set `GITHUB_WEBHOOK_SECRET` and reject unsigned webhook traffic.
- Keep `REVIEW_PROXY_TOKEN_ENABLED=true`, set `REVIEW_PROXY_TOKEN` to a strong
  random value, and expose only the Nginx port to non-loopback interfaces; keep
  the direct FastAPI mapping loopback-only. Disable validation only behind an
  equivalent trusted-network or upstream-authentication boundary.
- Store `.env` values in the platform secret manager instead of the repository.
- Put `WORKSPACE_ROOT` and `GIT_CACHE_ROOT` on a disk with enough space for the
  largest expected pull requests.
- Run cleanup for expired workspaces on a schedule:

```bash
curl -fsS -X POST http://review-orchestrator.internal:8000/api/v1/workspaces/cleanup/expired
```

If multiple service instances share the same database, they must also share
compatible workspace storage or route workspace/session operations to the same
instance that prepared the workspace.

## GitHub App Authentication

GitHub Installation Tokens normally expire after one hour. Do not generate one
manually for a long-running deployment. Review Orchestrator uses PyGithub's App
authentication implementation to sign App JWTs, resolve the Installation for
each repository, and obtain or refresh Installation Tokens when an API call or
Git checkout needs one. Tokens stay in memory and temporary child-process Git
configuration; they are not written to the database, workspace, clone URL, or
Compose environment file.

Create a GitHub App with these minimum repository permissions:

- Metadata: read (GitHub requires this permission for every App).
- Contents: read, for private repository checkout and commit access.
- Pull requests: read and write, for PR lookup and line review comments.
- Issues: read and write, for PR summary/status comments.

Subscribe the App to these webhook events:

- Pull requests.
- Issue comments.
- Pull request reviews.
- Pull request review comments.

Set its webhook URL to:

```text
https://<public-host>/api/v1/webhooks/github
```

Install the App on every organization/account and repository that the service
must review. Then copy the downloaded PEM into the ignored `secrets` directory:

```bash
mkdir -p secrets
cp /secure/path/github-app-private-key.pem secrets/github-app.pem
chmod 600 secrets/github-app.pem
```

Configure `.env`:

```dotenv
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
# Optional optimization for deployments that will always use one Installation:
GITHUB_INSTALLATION_ID=
GITHUB_WEBHOOK_SECRET=<same-secret-configured-in-the-app>
GITHUB_API_BASE_URL=https://api.github.com
GITHUB_INSTALLATION_TOKEN=
REVIEW_BOT_LOGIN=<app-slug>[bot]
```

`GITHUB_APP_ID` and `GITHUB_PRIVATE_KEY_PATH` are an all-or-nothing pair. App
mode takes precedence over `GITHUB_INSTALLATION_TOKEN`. Leave
`GITHUB_INSTALLATION_ID` empty when one service reviews repositories across
multiple App Installations; the repository-to-Installation mapping is resolved
automatically. The self-host Compose file mounts `./secrets` read-only into only
the API and worker containers.

Without App ID and private key, the service retains static-token compatibility
through `GITHUB_INSTALLATION_TOKEN`. Static mode cannot refresh an expiring
Installation Token and is therefore not recommended for unattended operation.

## GitHub Webhook

Configure a GitHub App webhook or repository webhook with this callback URL:

```text
https://<public-host>/api/v1/webhooks/github
```

Subscribe to these events:

- Pull requests.
- Issue comments.
- Pull request reviews.
- Pull request review comments.

Set the webhook secret in GitHub and inject the same value as
`GITHUB_WEBHOOK_SECRET`. Review Orchestrator verifies `X-Hub-Signature-256` when
the secret is configured. Required headers are:

- `X-GitHub-Delivery`
- `X-GitHub-Event`
- `X-Hub-Signature-256` when `GITHUB_WEBHOOK_SECRET` is set

Pull request actions `opened`, `synchronize`, `reopened`, and
`ready_for_review` create review runs. Duplicate `X-GitHub-Delivery` values are
idempotent.

## Smoke Tests

Health:

```bash
curl -fsS http://localhost:8000/health
```

Manual review run creation:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/review-runs \
  -H 'Content-Type: application/json' \
  -d '{
    "provider": "github",
    "repo_full_name": "owner/repo",
    "pull_request_number": 123,
    "base_sha": "0000000000000000000000000000000000000000",
    "head_sha": "1111111111111111111111111111111111111111"
  }'
```

Webhook ingestion with a fixture payload and no local signature verification:

```bash
unset GITHUB_WEBHOOK_SECRET
uv run uvicorn review_orchestrator.main:app --host 0.0.0.0 --port 8000
curl -fsS -X POST http://localhost:8000/api/v1/webhooks/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Delivery: local-smoke-1' \
  -H 'X-GitHub-Event: pull_request' \
  --data-binary @tests/fixtures/github_pr_opened.json
```

Signed webhook check with a configured secret:

```bash
export GITHUB_WEBHOOK_SECRET=change-me
body="$(cat tests/fixtures/github_pr_opened.json)"
signature="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" -binary | xxd -p -c 256)"
curl -fsS -X POST http://localhost:8000/api/v1/webhooks/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Delivery: local-smoke-signed-1' \
  -H 'X-GitHub-Event: pull_request' \
  -H "X-Hub-Signature-256: sha256=$signature" \
  --data-binary "$body"
```

Workspace checkout:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/workspaces/prepare \
  -H 'Content-Type: application/json' \
  -d '{
    "provider": "github",
    "repository": {
      "full_name": "owner/repo",
      "clone_url": "https://github.com/owner/repo.git"
    },
    "pull_request": {
      "number": 123,
      "base_sha": "BASE_SHA",
      "head_sha": "HEAD_SHA",
      "is_fork": false
    },
    "options": {
      "use_git_cache": true,
      "force_refresh": false,
      "enable_submodules": false,
      "enable_lfs": false
    }
  }'
```

App mode obtains the repository token automatically. In legacy static mode, an
API caller can still add `"auth":{"token_ref":"GITHUB_INSTALLATION_TOKEN"}`.

OpenHands session start after a workspace is ready:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/review-runs/<review_run_id>/session/start \
  -H 'Content-Type: application/json' \
  -d '{"workspace_path":"./.workspaces/github/<repo_hash>/pr-123/HEAD_SHA/repo"}'
```

Result collection and reconciliation check:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/review-runs/<review_run_id>/result \
  -H 'Content-Type: application/json' \
  -d '{
    "raw_output": {
      "summary": "Smoke test review completed.",
      "findings": [
        {
          "file": "src/app.py",
          "line": 42,
          "severity": "high",
          "message": "Example publishable finding.",
          "confidence": 0.9
        }
      ]
    },
    "changed_files": [
      {"path": "src/app.py", "commentable_lines": [42]}
    ]
  }'
```

A successful result stores the review summary, finding rows, and publishability
metadata. Summary comment and line comment references are tracked through
`review_comment_ref`; provider-side publishing adapters should upsert the summary
comment and dedupe line comments against those rows.

## OpenHands Connection

Set:

```bash
export OPENHANDS_BASE_URL=http://openhands:3000
export OPENHANDS_API_TOKEN=<token-if-required>
```

Review Orchestrator sends `Authorization: Bearer <token>` only when
`OPENHANDS_API_TOKEN` is non-empty. Session start calls
`POST /api/v1/app-conversations`, sync polls start-task and conversation
endpoints, and cancel best-effort deletes the OpenHands conversation.

If OpenHands is unavailable, review runs can fail with `failure_code` set to
`openhands_error`. Check `error`, `openhands_start_task_id`,
`openhands_conversation_id`, `openhands_sandbox_id`, and
`openhands_agent_server_url` in the review run response.

## Operations

Health check:

```bash
curl -fsS http://localhost:8000/health
```

Inspect a review run:

```bash
curl -fsS http://localhost:8000/api/v1/review-runs/<review_run_id>
```

Retry a failed run:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/review-runs/<review_run_id>/retry
```

Cancel a queued or running run:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/review-runs/<review_run_id>/cancel
```

Release a workspace lease after a session finishes:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/workspace-leases/<lease_id>/release
```

Clean one pull request's workspaces:

```bash
curl -fsS -X POST http://localhost:8000/api/v1/workspaces/cleanup/pr \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","repository":"owner/repo","pull_request_number":123,"force":false}'
```

## Troubleshooting

Webhook returns `401`:

- Confirm `GITHUB_WEBHOOK_SECRET` matches the GitHub webhook secret.
- Confirm GitHub sends `X-Hub-Signature-256`.
- Check that the request body is not modified by a proxy before it reaches the
  app.

Webhook returns `400`:

- Confirm `X-GitHub-Delivery` and `X-GitHub-Event` are present.
- Confirm the payload is a JSON object.

Webhook is accepted but no review run is created:

- Confirm the event is `pull_request`.
- Confirm the action is one of `opened`, `synchronize`, `reopened`, or
  `ready_for_review`.
- Comment events update context or create agent tasks when the configured bot is
  mentioned; they do not create review runs directly.

OpenHands request fails:

- Check `OPENHANDS_BASE_URL` from the Review Orchestrator host.
- Check `OPENHANDS_API_TOKEN` and OpenHands authentication settings.
- Inspect the review run `failure_code` and `error`.
- Verify OpenHands can access the workspace path passed to `session/start`.

Workspace checkout fails:

- `auth_failed`: the App is not installed for the repository, its private key or
  Installation ID is wrong, a legacy `auth.token_ref` is unavailable, or the
  resulting token cannot clone the repository.
- `repo_not_found`: the clone URL or token permissions are wrong.
- `base_missing` or `head_missing`: GitHub cannot fetch the requested commit.
- `network_error`: DNS, proxy, firewall, or GitHub availability problem.
- `workspace_locked`: cleanup was requested while a lease is active; release the
  lease or retry cleanup with `force=true`.

GitHub API or publishing fails:

- Verify the GitHub App installation has access to the repository.
- Verify the API and worker containers can read `GITHUB_PRIVATE_KEY_PATH`.
- Verify App permissions include Contents read, Pull requests read/write, and
  Issues read/write. Token expiry is handled automatically in App mode.
- Confirm summary comments are tracked by the hidden summary marker generated by
  `build_summary_comment_body`.
- Confirm line comments are only attempted for findings marked
  `publish_as_line_comment=true`; unpublishable findings remain summary-only.

Database startup fails:

- Confirm the async driver in `DATABASE_URL` is available.
- For PostgreSQL, confirm network access, credentials, database existence, and
  TLS requirements.
- For SQLite, confirm the process can write the database file and parent
  directory.

## Upgrade, Rollback, Backup

Before upgrading production:

1. Back up PostgreSQL.
2. Back up or snapshot workspace storage if active sessions may need it.
3. Deploy to staging with a copy of production-like configuration.
4. Run health, webhook, workspace, OpenHands, and result collection smoke tests.

For rollback:

1. Stop traffic to the new Review Orchestrator version.
2. Restore the previous application artifact or image.
3. Restore the database backup if the new version introduced incompatible schema
   changes.
4. Keep workspace storage intact unless the rollback plan explicitly requires a
   cleanup.
5. Re-run `/health` and inspect recent review runs before re-enabling webhooks.
