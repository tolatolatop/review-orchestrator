# Deployment Guide

This guide deploys Review Orchestrator with its embedded pi-agent SDK runtime,
PostgreSQL, the worker, and the optional Nginx edge.

## Architecture

`docker-compose.self_host.yaml` starts five long-running services:

- `postgres`: orchestrator state;
- `pi-agent-runtime`: `@earendil-works/pi-coding-agent` 0.80.7 over a small HTTP API;
- `review-orchestrator`: webhook, review, workspace, human-input, and observability APIs;
- `review-orchestrator-worker`: workspace preparation, agent polling, result validation, and provider publishing;
- `nginx`: the remote operator boundary.

The orchestrator and worker share `review_orchestrator_data`. The pi-agent
container mounts that volume read-only and can see only prepared workspaces. Its
own JSONL sessions and safe runtime snapshots live in `pi_agent_state`.

## Quick Start

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD, REVIEW_PROXY_TOKEN, PI_AGENT_RUNTIME_TOKEN,
# GITHUB_WEBHOOK_SECRET, and at least one LLM provider credential.
docker compose -f docker-compose.self_host.yaml up -d --build
docker compose -f docker-compose.self_host.yaml ps
```

Trusted local endpoints:

```bash
curl http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/health
curl http://127.0.0.1:${PI_AGENT_RUNTIME_PORT:-3210}/health
open http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/reviews/
```

Remote access goes through `${REVIEW_PROXY_PORT:-18080}`. With token validation
enabled, send `X-Review-Token: $REVIEW_PROXY_TOKEN` or use the `token` query
parameter for browser pages.

## Configuration

### Orchestrator and provider

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | local SQLite | SQLAlchemy async database URL. Self-host Compose supplies PostgreSQL. |
| `PROVIDERS_ENABLED` | `github,gitlab` | Comma-separated provider plugin keys. Disabled providers are neither constructed nor validated. |
| `PROVIDER_CORE_API_TOKEN` | empty | Bearer token for the four `/v1` Provider Core endpoints. The endpoints fail closed while unset. |
| `WORKSPACE_ROOT` | `./.workspaces` | Configurable root used to create provider/repository/PR/head-isolated checkouts. |
| `GIT_CACHE_ROOT` | `./.git-cache` | Bare mirror cache root. |
| `GITHUB_WEBHOOK_SECRET` | empty | Verifies GitHub webhook signatures. Required in production. |
| `GITHUB_APP_ID` | empty | GitHub App ID; configure with `GITHUB_PRIVATE_KEY_PATH`. |
| `GITHUB_PRIVATE_KEY_PATH` | empty | Mounted App private key path, normally `/run/secrets/github-app.pem`. |
| `GITHUB_INSTALLATION_ID` | auto | Optional fixed Installation ID. |
| `GITHUB_INSTALLATION_TOKEN` | empty | Legacy static/fine-grained token fallback. |
| `GITLAB_WEBHOOK_SECRET` | empty | GitLab webhook token. |
| `GITLAB_API_TOKEN` | empty | GitLab MR read, private checkout, and comment token. |
| `PROVIDER_API_TIMEOUT_SECONDS` | `30` | GitHub/GitLab request timeout. |
| `REVIEW_RUN_SOFT_TIMEOUT_SECONDS` | `900` | Emit delayed status after this duration. |
| `REVIEW_RUN_TIMEOUT_SECONDS` | `1800` | Cancel the runtime session and fail the run after this duration. |

### pi-agent and LLM

| Variable | Default | Purpose |
| --- | --- | --- |
| `PI_AGENT_BASE_URL` | `http://localhost:3210` | Runtime API used outside Compose. Compose uses `http://pi-agent-runtime:3210`. |
| `PI_AGENT_RUNTIME_PORT` | `3210` | Loopback-only runtime diagnostics port. |
| `PI_AGENT_RUNTIME_TOKEN` | empty | Shared Bearer token between orchestrator/worker and runtime. Use a strong value in production. |
| `PI_AGENT_PROVIDER` | `openai` | pi model provider. |
| `PI_AGENT_MODEL` | `gpt-5.4` | pi model ID. |
| `PI_AGENT_THINKING_LEVEL` | `high` | `minimal`, `low`, `medium`, `high`, or `xhigh`. |
| `PI_AGENT_MODEL_BASE_URL` | empty | Deployment-level provider/gateway URL; Task requests cannot override it. |
| `PI_AGENT_LLM_API_KEY` | empty | Generic runtime-only key for the selected provider. |
| `OPENAI_API_KEY` | empty | Standard key discovered by pi's OpenAI provider. |
| `ANTHROPIC_API_KEY` | empty | Standard key discovered by pi's Anthropic provider. |
| `PI_AGENT_REVIEW_AGENT` | `code-review` | Installed Agent selected for automatic reviews. |
| `PI_AGENT_REVIEW_SKILL` | `code-review` | Default Repository Skill ref for reviews. Prefix with `builtin:`, `npm:`, or `prebuilt:` when needed. |
| `AGENT_COMMAND_ENABLED` | `true` | Enable trusted `@bot` PR message commands. |
| `AGENT_COMMAND_AGENT` | `pr-assistant` | Installed Agent selected for PR message commands. |
| `AGENT_COMMAND_SKILL` | `pr-assistant` | Default Repository Skill ref for message commands. |
| `AGENT_TASK_SOFT_TIMEOUT_SECONDS` | `120` | Refresh a delayed command placeholder once. |
| `AGENT_TASK_TIMEOUT_SECONDS` | `600` | Cancel a command session and publish timeout. |
| `AGENT_TASK_MAX_HISTORY_TURNS` | `6` | Prior successful PR command exchanges supplied to the agent. |
| `AGENT_TASK_MAX_HISTORY_CHARS` | `24000` | Total prior command/answer prompt characters. |
| `AGENT_TASK_ALLOWED_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | GitHub author associations allowed to consume command capacity. |
| `AGENT_TASK_MAX_COMMAND_CHARS` | `8000` | Maximum command length after removing the bot mention. |
| `PI_AGENT_SKILLS_PATH` | `./pi-agent-runtime/skills` | Host directory mounted read-only at `/opt/pi-agent/skills`. |
| `PI_AGENT_CONFIG_PATH` | `./pi-agent-runtime/config` | Host directory containing optional `models.json`. |
| `PI_AGENT_ENVIRONMENT_TEMPLATE_PATH` | `./pi-agent-runtime/environment-template` | Read-only prebuilt directory cloned into every Task overlay. |
| `PI_AGENT_TIMEOUT_SECONDS` | `30` | Runtime HTTP request timeout. |

Built-in providers use the environment variable names supported by `pi-ai`.
For an OpenAI-compatible or company provider, copy
`pi-agent-runtime/config/models.example.json` to `models.json`, edit its
provider/model data, then select it with `PI_AGENT_PROVIDER` and
`PI_AGENT_MODEL`. The runtime never returns API keys in session state or events.

## Agents and Skills

The Runtime has one installed definition for each production Agent. The control
plane starts it with `agent_id`, `repository_skills`, `task_type`, and
schema-validated input. The Task Type maps to an installed named preset; Runtime
requests cannot select semantic versions, profiles, models, Base URLs, or Tools.

Skills use the Agent Skills `SKILL.md` convention:

```text
pi-agent-runtime/skills/
└── code-review/
    └── SKILL.md
```

Builtin Skills use `${PI_AGENT_SKILLS_PATH}/<name>/SKILL.md`. Repository policy
may instead select `npm:<package>`, which runs the package's normal `npm install`
inside the disposable Task overlay, or `prebuilt:<name>` from the cloned
template. Skill content and installation have Task-level execution capability,
but never register or grant a Tool implicitly. Runtime records content digests.

## Isolation Model

The self-host Runtime gives the Agent complete coding capability inside one
Task boundary:

- only the dedicated Workspace volume is mounted read-write; database, Git
  credential, and Provider secret storage are not mounted;
- the container root filesystem is read-only;
- the Controller retains only the capabilities needed to change Task UID and
  Workspace ownership; `no-new-privileges` remains set;
- no Docker socket or provider private-key directory is mounted;
- explicit Tools provide list/read/search/diff, file writes, and shell execution;
- every tool canonicalizes paths and rejects traversal/symlink escape;
- concurrent shell/npm children receive distinct UIDs from 20000–60000 while
  the credentialed Controller runs as root, so they cannot read Controller
  environment/state or another active Task Workspace;
- child environments contain only Task paths and non-secret process settings;
- each checkout path is isolated by provider, repository hash, PR number, and
  head SHA.

The runtime still needs outbound network access to the selected LLM. Apply an
egress allowlist at the container/network layer when your platform supports it.

## Pull Request Message Commands

Set `REVIEW_BOT_LOGIN` to the GitHub App bot login, for example
`bitbakedev[bot]`. A trusted repository participant can then write:

```text
@bitbakedev[bot] explain why the retry loop cannot run forever
```

The webhook stores a `message_command` AgentTask and returns immediately. The
worker must create or recover its placeholder before starting pi-agent. It then
updates the same comment with the validated answer. The Agent may edit, build,
and test in its Task Workspace; Provider delivery and credentialed Git side
effects remain deterministic Orchestrator operations.

Inspect or control a command:

```bash
curl http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/api/v1/agent-tasks/<task-id>
curl http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/api/v1/agent-tasks/<task-id>/agent-session
curl -X POST http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/api/v1/agent-tasks/<task-id>/cancel
curl -X POST http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/api/v1/agent-tasks/<task-id>/retry
```

The runtime uses `submit_task_result`, not `submit_review`. An empty mention is
completed without an LLM call and asks the user to include a request. Comments
from the bot itself, untrusted author associations, edited comments, and
oversized commands do not start an agent.

## Manual Session API

After a workspace exists, a session can be started with the deployment defaults:

```bash
curl -X POST http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/api/v1/review-runs/\
<review-run-id>/session/start \
  -H 'Content-Type: application/json' \
  -d '{"workspace_path":"/var/lib/review-orchestrator/workspaces/.../repo"}'
```

The only request field is `workspace_path`. Agent, Repository Skill, and Task
Type are resolved from domain configuration; arbitrary per-session overrides
return HTTP 422.

Use `/session/sync` to refresh database state and `/session/cancel` to abort.
Normal operation uses the worker and does not require manual calls.

## State and Upgrade from OpenHands

On startup, `init_models` adds the generic `agent_session_id`, `agent_status`,
`agent_provider`, `agent_model`, and `agent_thinking_level` columns when missing.
If a pre-cutover database has `openhands_conversation_id`, its value is copied to
`agent_session_id` for traceability. The old OpenHands services, privileged
Docker socket, database provisioning, and migration containers are no longer
part of Compose.

Before upgrading:

```bash
docker compose -f docker-compose.self_host.yaml down
# Back up PostgreSQL and the review_orchestrator_data volume.
docker compose -f docker-compose.self_host.yaml up -d --build
```

Old active OpenHands conversations cannot resume in pi-agent; retry those review
runs after cutover. Completed findings and provider comments remain in the
orchestrator database.

## GitHub App Authentication

Configure a GitHub App with these repository permissions:

- Pull requests: read and write;
- Issues: read and write (PR summary comments use issue comments);
- Contents: read;
- Metadata: read.

Subscribe to pull request and issue-comment events. Copy the PEM file to
`./secrets/github-app.pem`, set:

```dotenv
GITHUB_APP_ID=<app-id>
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
GITHUB_WEBHOOK_SECRET=<webhook-secret>
```

Only orchestrator containers receive `./secrets`; pi-agent does not.

## Nginx Authentication

The default is fail-closed token validation:

```dotenv
REVIEW_PROXY_TOKEN_ENABLED=true
REVIEW_PROXY_TOKEN=<strong-random-token>
```

`/health` and signed provider webhook routes bypass the operator token. Every
other Nginx route requires it. Setting `REVIEW_PROXY_TOKEN_ENABLED=false`
deliberately makes every proxy route tokenless; use that only behind an
equivalent trusted-network or upstream-authentication boundary. The direct
FastAPI port stays bound to loopback for trusted local administration.

## Troubleshooting

Runtime health and logs:

```bash
docker compose -f docker-compose.self_host.yaml ps pi-agent-runtime
docker compose -f docker-compose.self_host.yaml logs pi-agent-runtime review-orchestrator-worker
curl http://127.0.0.1:${PI_AGENT_RUNTIME_PORT:-3210}/health
```

Common failure categories:

- `pi_agent_infrastructure_error`: runtime connection/5xx failure; check service
  health, token agreement, and networking;
- `pi_agent_error`: invalid model, missing LLM authentication, failed agent, or
  invalid runtime request;
- `invalid_result`: `submit_review` data failed the orchestrator schema;
- `workspace_failed`: clone/fetch/checkout failed;
- `hard_timeout`: session exceeded `REVIEW_RUN_TIMEOUT_SECONDS` and was aborted.

If a model is unknown, verify `PI_AGENT_PROVIDER`, `PI_AGENT_MODEL`, and
`models.json`. If authentication is missing, use the provider's standard key or
`PI_AGENT_LLM_API_KEY`. If a skill is missing, verify its directory name,
frontmatter name and selected `builtin:`, `npm:`, or `prebuilt:` source.

## Production Checklist

- Use PostgreSQL with backups and TLS where appropriate.
- Configure signed webhooks and GitHub App/GitLab credentials.
- Set strong, distinct `POSTGRES_PASSWORD`, `REVIEW_PROXY_TOKEN`, and
  `PI_AGENT_RUNTIME_TOKEN` values.
- Store LLM keys in the platform secret manager, never in the repository.
- Keep the direct FastAPI and pi-agent ports loopback-only.
- Preserve runtime isolation settings and do not mount a Docker socket.
- Size `WORKSPACE_ROOT`, `GIT_CACHE_ROOT`, and `pi_agent_state` for concurrency.
- Apply outbound network policy for provider and LLM endpoints.
- Verify `/health`, one signed webhook, one completed structured review, one
  writable Workspace build/test run, hard-timeout cancellation, SessionArchive,
  and Provider publishing.
