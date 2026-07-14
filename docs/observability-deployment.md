# Observability Dashboard: Secure Deployment and Verification

The observability endpoints expose repository names, pull request metadata,
execution failures, task inputs/results, and OpenHands identifiers. Treat them
as an operator console. **Never expose them to an untrusted network without
authentication and TLS.**

## Current Availability

The service mounts its bundled operator dashboard at `/dashboard/` and a
focused review-run ledger at `/reviews/`. Both consume the canonical
observability APIs below from the same origin:

| Purpose | Endpoint |
| --- | --- |
| Provider events | `GET /api/v1/observability/provider-events[/{event_id}]` |
| Agent tasks | `GET /api/v1/observability/agent-tasks[/{task_id}]` |
| Review runs | `GET /api/v1/observability/review-runs[/{review_run_id}]` |
| OpenHands by run | `GET /api/v1/observability/review-runs/{review_run_id}/openhands-session` |
| OpenHands by conversation | `GET /api/v1/observability/openhands-sessions/{conversation_id}` |

The legacy paths without `/observability` remain compatible. Remote requests
use the token-protected Nginx boundary; trusted requests from the deployment
host may use the separate loopback-only FastAPI port without a token. See
[observability-api.md](observability-api.md) for the API and redaction contract.

## Recommended Self-host Topology

Use `docker-compose.self_host.yaml`. It exposes Nginx for remote access and
publishes FastAPI separately as `127.0.0.1:${REVIEW_LOCAL_PORT:-18000}` for
trusted local inspection. Before startup, set at least:

```dotenv
APP_ENV=production
REVIEW_PROXY_TOKEN=<strong-random-operator-token>
GITHUB_WEBHOOK_SECRET=<github-webhook-secret>
POSTGRES_PASSWORD=<strong-random-database-password>
```

Generate independent secrets with a password manager or cryptographically
secure generator. Store them in a deployment secret manager and never commit
`.env`.

```bash
docker compose -f docker-compose.self_host.yaml up --build -d
```

The supplied Nginx template applies these boundaries:

- `/health` is public for health checks.
- `/api/v1/webhooks/github` bypasses the operator token because GitHub cannot
  send it; the application verifies `X-Hub-Signature-256` when
  `GITHUB_WEBHOOK_SECRET` is configured.
- Every other path requires `X-Review-Token` or the `token` query parameter.

Local operators bypass Nginx entirely and therefore do not need the token:

```bash
open http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/reviews/
curl -fsS http://127.0.0.1:${REVIEW_LOCAL_PORT:-18000}/api/v1/observability/review-runs
```

The Compose binding is explicitly loopback-only. Do not publish this port on
`0.0.0.0` or forward it from an untrusted network.

Prefer the header:

```bash
curl -fsS "https://review.example.com/api/v1/review-runs?limit=20" \
  -H "X-Review-Token: ${REVIEW_PROXY_TOKEN}"
```

The query parameter exists for browser-only clients, but it can leak through
browser history, logs, referrer headers, screenshots, and analytics. Do not use
it for shared links. Terminate TLS, prefer a VPN/private network or
identity-aware proxy, rate-limit authentication failures, and avoid logging
credentials or response bodies.

The application currently relies on the Nginx edge for remote authentication;
FastAPI does not enforce an in-process operator identity or roles. The direct
FastAPI mapping is safe only while bound to host loopback. Do not make it
remotely reachable or permit untrusted services to bypass the proxy. Per-user
authorization and audit records require an identity-aware proxy or application
auth layer.

## Raw Payloads and Sensitive Data

Provider event detail hides the stored payload by default. An operator must
explicitly add `include_payload=true`, and the response is still passed through
the shared `redact_value` rules:

```text
GET /api/v1/provider-events/{event_id}?include_payload=true
```

The redactor removes sensitive key values and common credential shapes such as
authorization headers, cookies, signatures, tokens, private keys, JWTs, and
stack traces. Agent task input/result data is also redacted. OpenHands
diagnostics expose identifiers and status only, not the OpenHands API token,
raw events, logs, or container internals.

Redaction is risk reduction, not access control. New payload shapes may carry
sensitive values under unfamiliar keys, and payloads may contain user data or
private repository metadata. Keep raw inspection disabled in UI defaults, make
the action conspicuous, avoid client-side persistence, and review responses
before copying them to tickets or chat. Protect database backups and service
logs with the same operator boundary.

Set `OPENHANDS_UI_BASE_URL` only to an operator-protected URL. Generated links
do not carry `OPENHANDS_API_TOKEN`; OpenHands must enforce its own auth. Leave
the variable empty if no safe URL exists, and passthrough remains disabled.

## Verification Checklist

Run these checks from outside the private service network against Nginx/ingress.

### Authentication and network boundary

- `/health` succeeds without a token.
- Observability requests through Nginx with no token or an invalid token return
  `401`; the same requests with `X-Review-Token` succeed.
- Observability requests through `127.0.0.1:${REVIEW_LOCAL_PORT:-18000}` succeed
  without a token.
- FastAPI, PostgreSQL, and OpenHands are blocked from untrusted networks; TLS is
  valid on remote entrypoints; logs contain no token or response body.

### API and redaction

- Lists honor pagination and relevant filters.
- Event detail omits `payload` unless `include_payload=true` is supplied.
- A test event containing authorization, token, signature, JWT, private-key,
  and stack-trace values returns redaction markers, not originals.
- Agent task input/result values are redacted.
- OpenHands diagnostics contain no API credential or raw log/event data.
- Error responses expose no full stack trace or infrastructure secret.

```bash
uv run pytest tests/test_observability.py tests/test_api.py
```

### UI

- Through Nginx the route is `401` without auth; through the loopback FastAPI
  port it loads without a token.
- Overview, event, run, task, and session views handle loading, empty, API
  error, `401`/`403`, and not-found states.
- Raw payload is hidden initially and requires a deliberate action.
- Drill-down URLs contain no token; browser storage, console output, telemetry,
  and error reporting contain no payload or credentials.

### Webhook regression

Dashboard deployment must not add operator auth to the provider webhook handler
or alter its response contract:

- A correctly signed GitHub request to `/api/v1/webhooks/github` is accepted
  without `X-Review-Token`.
- A missing or invalid signature returns `401` when
  `GITHUB_WEBHOOK_SECRET` is configured.
- Required headers, duplicate-delivery idempotency, normalization, and
  review/task enqueue behavior remain unchanged.
- Observability endpoints still require the operator token.

Use the signed smoke test in [deployment.md](deployment.md), then run:

```bash
uv run pytest tests/test_api.py tests/test_providers.py
```

The Nginx exception currently covers GitHub only. If another provider must
reach the stack, add an equally narrow provider webhook location and rely on
that provider's configured signature/token validation; never make the full
`/api/v1/` namespace public.
