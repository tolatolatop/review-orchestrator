# Observability Dashboard: Secure Deployment and Verification

The observability endpoints expose repository names, pull request metadata,
execution failures, task inputs/results, and OpenHands identifiers. Treat them
as an operator console. **Never expose them to an untrusted network without
authentication and TLS.**

## Current Availability

The current service exposes operator APIs, but does not mount a bundled HTML
dashboard at `/dashboard/`. A separately deployed operator UI can consume:

| Purpose | Endpoint |
| --- | --- |
| Provider events | `GET /api/v1/provider-events[/{event_id}]` |
| Agent tasks | `GET /api/v1/agent-tasks[/{task_id}]` |
| Review runs | `GET /api/v1/review-runs[/{review_run_id}]` |
| OpenHands by run | `GET /api/v1/observability/review-runs/{review_run_id}/openhands-session` |
| OpenHands by conversation | `GET /api/v1/observability/openhands-sessions/{conversation_id}` |

Do not publish a `/dashboard/` link until its UI artifact is mounted and the
route is verified in the deployed release. See
[observability-api.md](observability-api.md) for the API and redaction contract.

## Recommended Self-host Topology

Use `docker-compose.self_host.yaml`. It keeps FastAPI on the private Compose
network and exposes Nginx as the only Review Orchestrator entrypoint. Before
startup, set at least:

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

The application currently relies on edge authentication; FastAPI does not
enforce an in-process operator identity or roles. Do not publish port `8000`,
route around Nginx, or permit untrusted services to bypass the proxy. Per-user
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
- Observability requests with no token or an invalid token return `401`.
- The same requests with `X-Review-Token` succeed.
- FastAPI port `8000`, PostgreSQL, and OpenHands are blocked from untrusted
  networks; TLS is valid; logs contain no token or response body.

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

### UI (when an artifact is added)

- The route is `401` without auth and loads only after authentication.
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

