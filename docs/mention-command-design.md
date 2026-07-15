# Mention Command Agent Design

> Historical note: trigger parsing, placeholder semantics, and result schemas
> remain useful, but the read-only Runtime, per-request Profile, and interaction
> assumptions below are superseded by `agent-task-architecture-redesign_zh.md`.
> The current Agent has full Task Workspace read/write/shell capability and is
> configured only through Agent + Repository Skills + Task Type preset composition.

## Status

Implemented on `feat/pi-agent`; this document is the behavioral and acceptance
contract for the message-command path.

## Decision

A bot mention is an **agent command**, not an implicit pull-request review.
`AgentTask` becomes the durable owner of the command lifecycle. It reuses the
existing webhook inbox, provider adapters, isolated workspace preparation,
pi-agent model configuration, polling worker, and timeout conventions, but it
does not create a `ReviewRun`.

The first implementation targets GitHub, which is the provider that currently
normalizes mention events. Provider interfaces remain neutral so GitLab note
support can be added without changing the task state machine.

## Goals

- Accept a natural-language command after `@<review-bot-login>` on a pull
  request.
- Create or recover exactly one placeholder comment before starting pi-agent.
- Run a read-only repository task against a head-SHA-isolated workspace.
- Replace the same placeholder with the final answer.
- Replace the placeholder promptly on validation failure, runtime failure,
  cancellation, or hard timeout, and refresh it once on soft timeout.
- Preserve webhook, session-start, provider-comment, and worker-restart
  idempotency.
- Give follow-up commands bounded context from earlier successful bot exchanges
  on the same pull request.
- Keep the existing automatic review path and structured findings contract
  unchanged.

## Non-goals for the first version

- Editing repository files, pushing commits, or opening pull requests. The
  runtime remains read-only.
- Reusing a terminal pi-agent session. Each valid mention owns a new session.
- Streaming tokens into the provider comment or updating it for every tool call.
- Asking a live human-input question from the runtime. If information is
  missing, the final result uses `needs_clarification`; a later mention starts a
  new task with prior exchanges included as context.
- Treating edited comments as new commands. Only newly created/submitted events
  trigger the first version.

## End-to-end flow

```text
GitHub mention webhook
  -> provider_event_inbox (delivery idempotency)
  -> AgentTask(command, source metadata, queued)
  -> worker upserts placeholder comment
  -> worker hydrates PR context and pins head SHA
  -> worker prepares/reuses isolated read-only workspace
  -> pi-agent instruction session
  -> submit_task_result
  -> validate and persist result
  -> PATCH the same placeholder comment
  -> AgentTask completed
```

The webhook handler does not call GitHub or the LLM. It commits the inbox event
and task, then returns `200`. The worker's first external side effect is the
placeholder. Workspace preparation and runtime start are forbidden until the
placeholder comment ID has been persisted or recovered.

## Event acceptance and command extraction

### GitHub events

The following actions are accepted when their body contains the configured bot
login:

| Event | Accepted action | Source ID |
| --- | --- | --- |
| `issue_comment` | `created` | `comment.id` |
| `pull_request_review` | `submitted` | `review.id` |
| `pull_request_review_comment` | `created` | `comment.id` |

The normalizer extracts a provider-neutral message envelope:

```json
{
  "source_kind": "issue_comment",
  "source_comment_id": "123456",
  "source_url": "https://github.com/owner/repo/pull/25#issuecomment-123456",
  "author_login": "alice",
  "author_association": "MEMBER",
  "command_text": "Explain why this retry is safe."
}
```

Rules:

1. Match the configured login case-insensitively with token boundaries, then
   remove only the bot mention from the body.
2. Normalize line endings and surrounding whitespace; preserve Markdown inside
   the command.
3. Reject commands larger than 8,000 characters.
4. Ignore events authored by the configured bot or by a provider `Bot` actor.
   This prevents the placeholder and final answer from recursively triggering
   another task.
5. By default, accept `OWNER`, `MEMBER`, and `COLLABORATOR` associations. The
   allowlist is configurable. Rejected actors do not consume LLM capacity.
6. A mention with no command creates a terminal validation response without
   starting pi-agent: "Please include a request after the bot mention."
7. Use a unique partial index on non-null `provider_event_id` so a provider
   delivery cannot create multiple tasks.

Valid mentions use `internal_event=agent_command` and
`task_type=message_command`. Historical `agent_mention` tasks remain readable
and are not migrated into the new execution path.

## AgentTask as the execution aggregate

`AgentTask` receives first-class columns for fields needed for claiming,
recovery, timeout processing, and observability. Large provider payloads remain
in `ProviderEventInbox`; they are not copied into the agent prompt.

### Identity and input

- `source_kind`
- `source_comment_id`
- `source_url`
- `source_author_login`
- `command_text`
- `head_sha`
- existing provider/repository/pull-request/context identifiers

### Execution

- `stage`
- `workspace_path`
- `agent_session_id`
- `agent_status`
- `agent_provider`, `agent_model`, `agent_thinking_level`
- `attempt`
- `lock_owner`, `locked_until`

### Provider response

- `response_comment_id`
- `response_body_hash`
- `response_published_at`
- `publish_attempts`
- `last_publish_error`

### Result and lifecycle

- `result_text`
- existing `result_json` for the validated structured response
- `failure_code`, existing `error_message`
- `started_at`, `completed_at`, `deadline_at`
- `soft_timeout_emitted_at`, `hard_timeout_emitted_at`

Existing rows receive nullable/default values through the current additive
database-upgrade mechanism. Existing review-linked mention tasks are left
untouched.

## State machine

`status` remains a small lifecycle value; `stage` supplies operator detail.

| Status | Stage | Required action |
| --- | --- | --- |
| `queued` | `placeholder_pending` | Create or recover placeholder. |
| `queued` | `waiting_for_turn` | Placeholder exists; serialize behind an older command on the same PR. |
| `running` | `preparing_workspace` | Hydrate PR context, pin head SHA, prepare workspace. |
| `running` | `starting_agent` | Start or recover the idempotent runtime session. |
| `running` | `waiting_for_agent` | Poll the runtime without holding the DB lock. |
| `running` | `collecting_result` | Validate and persist `submit_task_result`. |
| `running` | `publishing_result` | Update the placeholder with the stored result. |
| `completed` | `completed` | Final comment is confirmed published. |
| `failed` | `failed` | Failure is stored and its placeholder update is confirmed or exhausted. |
| `cancelled` | `cancelled` | Cancellation is stored and reflected in the placeholder. |

Commands for the same provider/repository/PR are executed FIFO, one active
runtime session at a time. Every queued command may already have its own
placeholder. Serialization makes bounded conversation history deterministic and
prevents competing answers from appearing out of order.

Workers claim both queued and running tasks with an expiring lease. PostgreSQL
uses `FOR UPDATE SKIP LOCKED`; SQLite tests use an optimistic compare/update.
The worker releases the lease while the runtime is working, just as review runs
are polled rather than synchronously awaited.

## Placeholder and answer contract

Every response contains a task-specific hidden marker:

```html
<!-- review-orchestrator:agent-task task_id=<uuid> source_id=<provider-id> -->
```

Initial body:

```markdown
🤖 Working on @alice's request…

Status: preparing repository context
Task: `<short-task-id>`
```

Final body:

```markdown
🤖 Answer for @alice

<validated answer Markdown>

References:
- `src/example.py:42`

Task: `<short-task-id>` · completed
```

The source comment is never edited. GitHub v1 always creates an issue comment
on the PR timeline, even if the command came from a review or line comment; the
body may link back to the source URL. This uses the existing stable create/PATCH
APIs and avoids separate reply semantics per event type.

The provider adapter gains one operation:

```python
async def upsert_agent_task_comment(
    session: AsyncSession,
    task: AgentTask,
    *,
    presentation: AgentTaskPresentation,
) -> str: ...
```

The operation updates `response_comment_id` when known. If a worker crashes
after GitHub creates the comment but before the database commit, recovery lists
PR comments and finds the exact task marker before creating anything new.
`response_body_hash` skips redundant PATCH requests.

Comments are updated only at meaningful milestones:

- initial placeholder;
- queued-behind-another-task, when applicable;
- soft timeout;
- final answer, validation failure, runtime failure, cancellation, or hard
  timeout.

Tool-level progress is visible through observability APIs, not provider comment
churn.

## pi-agent instruction mode

`POST /v1/sessions` becomes a backward-compatible discriminated request. An
omitted `kind` continues to mean `review`; the command path sends
`kind=instruction`.

```json
{
  "kind": "instruction",
  "idempotency_key": "agent-task:<task-id>:attempt:1",
  "title": "PR #25 command from alice",
  "workspace_path": "/var/lib/review-orchestrator/workspaces/.../repo",
  "repository_context": {
    "provider": "github",
    "repo_full_name": "owner/repo",
    "pr_number": 25,
    "base_sha": "...",
    "head_sha": "..."
  },
  "instruction": {
    "text": "Explain why this retry is safe.",
    "author_login": "alice",
    "source_url": "https://github.com/owner/repo/pull/25#issuecomment-123456",
    "history": []
  },
  "model": {
    "provider": "deepseek",
    "id": "deepseek-v4-pro",
    "thinking_level": "high"
  },
  "skills": ["pr-assistant"],
  "profile": "default"
}
```

The runtime persists `idempotency_key -> session_id` atomically. Repeating the
start request returns the existing session, preventing orphan duplicate agents
if the orchestrator crashes between runtime start and database persistence.

Instruction sessions receive only:

- `list_files`
- `read_file`
- `search_code`
- `git_diff`
- `submit_task_result`

They do not receive `submit_review`, shell, write, or edit tools. They also do
not receive `request_human_input` in v1; clarification is a terminal structured
outcome so every command can update its placeholder.

The terminating tool contract is:

```json
{
  "outcome": "answered",
  "answer": "The retry is safe because…",
  "references": [
    {
      "path": "src/example.py",
      "line_start": 42,
      "line_end": 51
    }
  ]
}
```

- `outcome`: `answered`, `needs_clarification`, or `refused`.
- `answer`: required Markdown, 1 to 30,000 characters.
- `references`: at most 50 repository-relative path/line references. Paths are
  canonicalized against the workspace before acceptance.
- The runtime terminates only after one valid `submit_task_result` call. A model
  turn ending without it fails with `missing_result`.

A separate `pr-assistant` skill directs the agent to answer the requested
question rather than perform a general review, cite repository evidence, state
uncertainty, respect the read-only boundary, and always call
`submit_task_result`.

## Conversation context

Each command starts a new session, but it receives up to the six most recent
successfully completed message-command exchanges for the same PR, ordered
oldest to newest. Each history item includes author, command, answer, outcome,
and pinned head SHA.

Limits:

- at most 6 turns;
- at most 24,000 total characters after deterministic oldest-first truncation;
- only orchestrator-owned command/answer pairs, never the full untrusted PR
  comment feed;
- failed tasks contribute no answer text;
- system/runtime safety instructions remain higher priority than message text
  and repository contents.

This provides useful follow-up dialogue while retaining per-message durability,
independent placeholders, and deterministic timeout handling.

## Timeout, retry, and failure behavior

New configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_COMMAND_ENABLED` | `true` | Global command switch. |
| `AGENT_COMMAND_SKILL` | `pr-assistant` | Runtime skill. |
| `AGENT_TASK_SOFT_TIMEOUT_SECONDS` | `120` | Refresh placeholder with delayed status. |
| `AGENT_TASK_TIMEOUT_SECONDS` | `600` | Cancel runtime and publish terminal timeout. |
| `AGENT_TASK_MAX_HISTORY_TURNS` | `6` | Conversation history bound. |
| `AGENT_TASK_MAX_HISTORY_CHARS` | `24000` | Prompt history size bound. |
| `AGENT_TASK_ALLOWED_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | Cost/security allowlist. |

Timeouts start when the runtime session is first started, not while the command
is waiting in the FIFO queue. The hard deadline is never extended by retries.

The worker runs an `AgentTask` timeout scan every poll:

- At soft timeout, set `soft_timeout_emitted_at` once and PATCH the placeholder
  to say the task is still running and will be stopped at the hard deadline.
- At hard timeout, best-effort cancel the runtime, persist
  `failure_code=hard_timeout`, and immediately PATCH the placeholder with the
  terminal failure.

Failure handling:

| Failure | Behavior |
| --- | --- |
| Placeholder create/update | Retry with existing backoff. Never start the agent before initial placeholder success. |
| Context/workspace | Persist safe category and update placeholder in the same worker pass. |
| Runtime start transport/5xx | Retry with the same idempotency key; deadline is unchanged. |
| Runtime/model failure | Persist safe category and update placeholder immediately. |
| Invalid structured result | Fail closed; never publish unvalidated model output. |
| Final PATCH failure | Keep the validated result, remain in `publishing_result`, and retry. |
| Provider retry exhaustion | Mark `result_publish_failed`; retain result for operators. |

Provider-facing errors contain only a stable category and safe message. Raw
upstream bodies, credentials, headers, stack traces, and workspace host paths
remain in restricted logs and are redacted by existing observability helpers.

## Idempotency and crash recovery

Four independent keys cover the side effects:

1. Webhook: existing `(provider, delivery_id)` inbox uniqueness.
2. Task: unique non-null `provider_event_id`.
3. Placeholder: hidden task marker plus persisted `response_comment_id`.
4. Runtime: persisted `idempotency_key` in the pi-agent service.

The database result is committed before final comment publication. On restart,
a task in `waiting_for_agent` polls its persisted runtime session; a task in
`publishing_result` republishes the already validated result and never calls the
model again.

## Cancellation and PR lifecycle

- Closing or merging a PR cancels queued/running message tasks for that PR,
  best-effort cancels active runtime sessions, and updates every existing
  placeholder to `cancelled`.
- A command is pinned to the head SHA resolved before workspace preparation.
  A later PR synchronize event does not silently move a running task to a new
  commit.
- New commands after a head update resolve and pin the new head.
- Manual cancellation is exposed as `POST /api/v1/agent-tasks/{id}/cancel`.

## Observability and API surface

The existing agent-task endpoints remain and add:

- stage and pinned head SHA;
- response comment ID/URL and publish state;
- runtime session/model/status metadata;
- result outcome and safe answer preview;
- failure code, timeout state, attempt, and lifecycle timestamps.

New endpoints:

- `GET /api/v1/agent-tasks/{id}/agent-session`
- `POST /api/v1/agent-tasks/{id}/cancel`
- `POST /api/v1/agent-tasks/{id}/retry` for terminal retryable failures

Dashboard trace:

```text
provider event -> message command -> placeholder -> workspace -> pi-agent
-> result validation -> provider update
```

Logs and metrics include task ID, provider/repository/PR, stage duration,
placeholder latency, runtime latency, total completion latency, outcome, retry
count, and failure category. Command and answer bodies are not emitted to normal
application logs.

## Repository-level policy

`ReviewConfig` gains:

- `agent_commands_enabled` (default `true`);
- `default_agent_command_skill` (default `pr-assistant`);

Automatic review enablement and command enablement are independent. Disabling
automatic review must not disable explicitly requested bot commands.

## Implementation sequence

1. Add message extraction, author filtering, new task fields, additive database
   migration, schemas, and tests. Stop creating `ReviewRun` for new commands.
2. Add task-specific provider placeholder upsert and exact-marker recovery.
3. Add pi-agent instruction request, idempotency storage,
   `submit_task_result`, and `pr-assistant` skill.
4. Replace the one-shot mention worker with the leased, pollable AgentTask state
   machine and per-PR FIFO serialization.
5. Add soft/hard timeout scans, cancellation, retries, and terminal placeholder
   updates.
6. Extend observability APIs/dashboard and deployment configuration/docs.
7. Deploy and validate with a real PR mention after automated tests pass.

## Required test matrix

### Normalization and policy

- All three accepted GitHub event types extract the same command envelope.
- Case-insensitive mention matching does not match substrings.
- Bot-authored, disallowed-association, edited, oversized, and non-PR comments
  do not start the runtime.
- Empty commands produce a terminal guidance response without LLM use.
- Duplicate webhook deliveries return the original task.

### State machine and comments

- Placeholder is confirmed before workspace/runtime calls.
- A crash between comment creation and DB persistence recovers by exact marker.
- Final answer, validation failure, workspace failure, runtime failure,
  cancellation, soft timeout, and hard timeout all update the same comment.
- Provider PATCH failure retries without rerunning the agent.
- Multiple commands on one PR execute FIFO and keep distinct placeholders.

### Runtime and security

- Repeating the idempotent start returns one session.
- Instruction mode exposes only read/search/diff/result tools.
- `submit_review` is unavailable and `submit_task_result` terminates the task.
- Workspace traversal/symlink escape checks remain enforced.
- Invalid result paths, oversized answers, and missing terminal tool calls fail
  closed.

### End to end

- Faux provider: webhook -> placeholder -> real `createAgentSession` ->
  `submit_task_result` -> same-comment PATCH -> completed task.
- Real GitHub validation: mention `@bot explain the verification document`,
  observe a placeholder, then a cited answer in that exact comment.
- Real failure validation: use a short test timeout or injected model failure and
  verify the placeholder becomes terminal without a second comment.

## Acceptance criteria

The feature is complete only when all of the following are true:

1. A valid mention produces an AgentTask and no ReviewRun.
2. The provider receives exactly one bot comment per command.
3. The runtime cannot start before that comment is recoverably identified.
4. The final validated answer replaces the placeholder in the same comment.
5. Soft timeout, hard timeout, cancellation, and every terminal failure are
   reflected in that comment within one worker poll plus provider latency.
6. Duplicate deliveries, worker restarts, and provider retries do not duplicate
   comments or runtime sessions.
7. Follow-up mentions receive bounded earlier command/answer context.
8. Automatic PR reviews and their findings/publication behavior are unchanged.
