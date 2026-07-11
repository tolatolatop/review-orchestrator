# Review Observability API Contract

This document defines the backend contract for operator-facing review
observability APIs used by the bundled `/dashboard/` console.

## Goals

- Let operators trace one review from provider webhook ingress through review
  run execution, agent task handling, PR context, OpenHands session state, and
  provider publishing state.
- Keep all observability routes private to operators.
- Make payload exposure opt-in and redacted by default.
- Use one pagination, sorting, filter, and response envelope shape across list
  endpoints so tests can reuse the same helpers.

## Route Layout

All new operator observability endpoints live under:

```text
/api/v1/observability
```

Legacy MVP endpoints remain supported as compatibility aliases:

```text
GET /api/v1/provider-events
GET /api/v1/provider-events/{event_id}?include_payload=false
```

Operator clients should use these paths:

| Route | Purpose |
| --- | --- |
| `GET /api/v1/observability/provider-events` | List provider event inbox records. |
| `GET /api/v1/observability/provider-events/{event_id}` | Event detail with linked run/task references. |
| `GET /api/v1/observability/review-runs` | List review runs and current stage. |
| `GET /api/v1/observability/review-runs/{review_run_id}` | Run detail with session, findings, retry, and publishing summaries. |
| `GET /api/v1/observability/agent-tasks` | List queued/running/completed agent tasks. |
| `GET /api/v1/observability/agent-tasks/{agent_task_id}` | Agent task detail with redacted input and result. |
| `GET /api/v1/observability/pull-requests/{provider}/{repo}/{number}` | PR context drill-down entry point. |
| `GET /api/v1/observability/openhands-sessions/{conversation_id}` | OpenHands session status known to the orchestrator. |
| `GET /api/v1/observability/review-runs/{review_run_id}/openhands-session` | OpenHands session diagnostics for one review run, including missing-session and disabled passthrough states. |
| `GET /api/v1/observability/publishing` | Provider comment publishing and reconciliation state. |

Path parameters use stored IDs, not provider display labels, except for the PR
context route. Repository names containing `/` must be path-encoded by clients.

## Authentication And Authorization

No observability route may be public. The initial guard is operator access at the
deployment edge:

- `GET /health` remains public for load balancers.
- `POST /api/v1/webhooks/{provider}` remains provider-authenticated by webhook
  signature or provider token validation.
- Every other route, including all `/api/v1/observability/*` routes, requires an
  operator credential. The current nginx deployment enforces this with
  `X-Review-Token` or a `token` query parameter.

When in-process auth is added, it must be implemented as a FastAPI dependency
that is attached to the observability router. Do not add unauthenticated
observability endpoints for local convenience; use test settings or dependency
overrides instead.

## List Conventions

All list endpoints accept these shared pagination query parameters:

| Parameter | Default | Limit | Notes |
| --- | --- | --- | --- |
| `limit` | `50` | `1..200` | Maximum number of records returned. |
| `offset` | `0` | `>=0` | Offset for page navigation. |
| `sort` | `-created_at` | endpoint-specific allowlist | `-` means descending. |

List endpoints return the shared envelope represented by
`ObservabilityListEnvelope`:

```json
{
  "items": [],
  "total": 0,
  "limit": 50,
  "offset": 0,
  "sort": "-created_at"
}
```

The default sort is newest first. Ties must be broken by stable ID order. Endpoint
implementations should reject unsupported `sort` values with `422` instead of
silently ignoring them.

## Common Filters

Use exact-match filters unless documented otherwise:

- `provider`
- `repo_full_name`
- `pull_request_number`
- `head_sha`
- `status`
- `stage`
- `delivery_id`
- `internal_event`
- `created_from`
- `created_to`

Time filters use RFC 3339 datetimes and are inclusive. API responses use ISO
8601 datetimes from Pydantic's JSON serialization.

## Entity References

Drill-down pages should expose linked references without embedding every linked
entity by default:

```json
{
  "provider_event_id": "event-id",
  "review_run_id": "run-id",
  "agent_task_id": "task-id",
  "pull_request_context_id": "context-id",
  "openhands_conversation_id": "conversation-id",
  "summary_comment_id": "provider-comment-id"
}
```

The canonical review chain is:

```text
ProviderEventInbox
  -> ReviewRun or AgentTask
  -> PullRequestContext
  -> OpenHands conversation/session fields on ReviewRun or ReviewSession
  -> Finding / ReviewCommentRef provider publishing state
```

For each link, return `null` when the entity has not been created or the stored
provider payload did not include enough identity to derive it.

## Redaction Policy

The shared implementation is `review_orchestrator.observability.redact_value`.
Use it for any JSON payload, provider header map, agent task input/result,
OpenHands response snapshot, or provider publishing error exposed to operators.

Default rules:

- Redact any value whose key name contains sensitive terms such as
  `authorization`, `cookie`, `secret`, `signature`, `token`, `private_key`,
  `client_secret`, `api_key`, `installation`, `x-hub-signature-256`, or
  `x-gitlab-token`.
- Redact common credential shapes inside strings, including bearer/basic auth
  header values, GitHub tokens, GitLab personal access tokens, OpenAI-style
  `sk-` tokens, Slack `xox*` tokens, JWTs, and PEM private keys.
- Replace stack traces with `[redacted stack trace]`.
- Preserve safe scalar values, object shape, list order, IDs, repository names,
  PR numbers, commit SHAs, statuses, timestamps, and payload digests.

Provider event detail keeps payloads hidden unless `include_payload=true`.
Included payloads are always returned after redaction.

## Response Models

Current shared models:

- `ObservabilityPage`
- `ObservabilityListEnvelope`
- `ProviderEventInboxSummary`
- `ProviderEventInboxListResponse`
- `ProviderEventInboxDetail`

OpenHands session diagnostics expose only safe metadata: review run and linked
agent task identifiers, conversation/start-task/sandbox IDs, agent server URL,
run status/stage, live execution and sandbox status when OpenHands is reachable,
and a `passthrough` object. `passthrough.enabled=false` is returned with a
human-readable reason when the review run has no conversation ID or
`OPENHANDS_UI_BASE_URL` is not configured. The response never includes
OpenHands API credentials, raw events, container internals, or logs.

Future endpoint response models should add endpoint-specific item fields while
reusing `ObservabilityListEnvelope` for list metadata. Do not return raw ORM
objects directly.

## Test Contract

Backend tests should reuse:

- `ObservabilityPage` defaults for list pagination.
- `ObservabilityListEnvelope` for list response assertions.
- `redact_value` fixtures for payload, header, token, private key, and stack
  trace redaction.

Every endpoint that can expose provider payloads, headers, task inputs/results,
OpenHands snapshots, or publishing errors must include at least one redaction
test.
