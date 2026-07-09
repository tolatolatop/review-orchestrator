# Platform Provider Extension Guide

Review Orchestrator is currently a GitHub-first MVP. The data model and several
API schemas already carry a `provider` value, but webhook ingestion, event
normalization, API headers, and provider-side publishing behavior are still
implemented with GitHub semantics. This guide describes how to add GitLab, Azure
DevOps, Bitbucket, GitCode, or another code-hosting platform without weakening
the existing GitHub path.

The target shape is a small provider adapter boundary around external platform
behavior. Review run lifecycle, OpenHands session orchestration, workspace
preparation, result parsing, finding reconciliation, and retry policy should stay
provider-agnostic.

## Current GitHub MVP

GitHub support currently covers:

- `POST /api/v1/webhooks/github` with `X-GitHub-Delivery`, `X-GitHub-Event`,
  and optional `X-Hub-Signature-256` verification.
- Normalization of `pull_request`, `issue_comment`, `pull_request_review`, and
  `pull_request_review_comment` events.
- Review-run creation for PR `opened`, `synchronize`, `reopened`, and
  `ready_for_review`.
- PR context updates for supported pull request state and metadata changes.
- Mention-trigger agent tasks when PR comments or reviews mention the configured
  review bot login.
- Provider event idempotency through `(provider, delivery_id)`.
- Provider-scoped storage for PR contexts, review runs, comment refs, workspaces,
  and review config.
- Summary and line comment reference tracking in `ReviewCommentRef`.
- Summary-only fallback when a finding cannot be mapped to a changed,
  commentable line.

GitHub-specific assumptions still visible in the code:

- The generic `/webhooks/{provider}` route rejects every provider except
  `github`.
- Header names, signature format, event names, and payload field paths are
  handled directly in the route and `review_orchestrator.github`.
- `accept_github_webhook` is named and typed around
  `NormalizedGitHubEvent`.
- Runtime settings contain GitHub App and GitHub API fields only.
- Provider diff fetching, comment publishing, review thread resolve, and rate
  limit handling are not yet represented as a shared adapter interface.

Do not rename or generalize all GitHub code before another provider proves the
need. Add the smallest adapter surface that lets the next provider use the same
internal contracts.

## Provider Boundary

A provider adapter owns external platform concerns:

- Webhook header validation and signature verification.
- Raw payload parsing and delivery ID extraction.
- Event normalization into the internal event names listed below.
- Pull request or merge request metadata extraction.
- Changed-file and diff metadata retrieval.
- Commentability mapping for line-level findings.
- Summary comment create/update behavior.
- Line comment or review thread create/update behavior.
- Review thread resolution or stale-comment handling when supported.
- Provider rate limit, retry-after, permission, and not-found error mapping.
- Token lookup and API client construction from provider-specific secrets.

The orchestrator core owns shared behavior:

- Provider event inbox idempotency and coalescing.
- `PullRequestContext`, `ReviewRun`, `Finding`, `ReviewCommentRef`,
  `ReviewConfig`, and `Workspace` persistence.
- Review run retry, cancellation, superseding, timeout, and lifecycle state.
- OpenHands session start/sync/cancel.
- Review result schema validation and fingerprint generation.
- Summary-only fallback for findings that cannot be line-commented.

## Adapter Contract

The minimum adapter contract for a new review-triggering provider is:

```python
class ProviderAdapter(Protocol):
    name: str

    async def parse_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ProviderWebhookEvent: ...

    async def get_pull_request_context(
        self,
        event: ProviderWebhookEvent,
    ) -> ProviderPullRequestContext: ...

    async def list_changed_files(
        self,
        context: ProviderPullRequestContext,
    ) -> list[ProviderChangedFile]: ...

    async def upsert_summary_comment(
        self,
        context: ProviderPullRequestContext,
        body: str,
        existing_provider_comment_id: str | None,
    ) -> ProviderCommentRef: ...

    async def create_line_comment(
        self,
        context: ProviderPullRequestContext,
        finding: PublishableFinding,
        body: str,
    ) -> ProviderCommentRef: ...
```

Only `parse_webhook` and `get_pull_request_context` are required for a provider
that can queue review runs. `list_changed_files` is required before line-comment
publishing is enabled. Summary and line comment publishing can be added
incrementally behind `ReviewConfig.line_comments_enabled` and provider capability
checks.

Use these internal data shapes regardless of external provider vocabulary:

| Internal shape | Required fields | Notes |
| --- | --- | --- |
| `ProviderWebhookEvent` | `provider`, `delivery_id`, `provider_event`, `provider_action`, `internal_event`, `repository`, `pull_request_number`, `head_sha`, `status`, `raw_payload` | `pull_request_number` is also used for GitLab merge requests and Azure pull requests. It is the stable human-facing MR/PR number when available. |
| `ProviderPullRequestContext` | `provider`, `repo_full_name`, `pull_request_number`, `base_sha`, `head_sha`, `base_ref`, `head_ref`, `author_login`, `html_url`, `status`, `is_fork` | Keep `provider_pr_id` for opaque platform IDs that differ from PR/MR number. |
| `ProviderChangedFile` | `path`, `status`, `patch`, `commentable_lines`, `provider_position` | `commentable_lines` is the orchestrator-facing gate. `provider_position` may store GitLab diff positions or Azure thread context. |
| `ProviderCommentRef` | `provider_comment_id`, `provider_thread_id`, `comment_type`, `status` | Store external IDs without assuming numeric GitHub IDs. |

## Event Mapping

Normalize provider events into a small internal vocabulary. Unknown actions
should be stored as ignored inbox events rather than failing the webhook unless
the payload is invalid or unauthenticated.

| Internal event | GitHub | GitLab | Azure DevOps | Bitbucket / GitCode guidance |
| --- | --- | --- | --- | --- |
| `pr_opened` | `pull_request.opened` | `Merge Request Hook` with `open` or `opened` | `git.pullrequest.created` | PR created/opened events. |
| `pr_updated` | `pull_request.synchronize` | MR update with changed source SHA | `git.pullrequest.updated` with new source commit | Source branch commit changed. |
| `pr_reopened` | `pull_request.reopened` | MR reopened | Pull request reactivated | Reopened from declined/closed. |
| `pr_closed` | `pull_request.closed` when not merged | MR closed | Pull request abandoned/closed | Closed without merge. |
| `pr_merged` | `pull_request.closed` with `merged=true` | MR merged | Pull request completed | Use merged/completed timestamp if present. |
| `pr_ready_for_review` | `pull_request.ready_for_review` | Draft flag changed to false | Draft support varies | If unsupported, leave unmapped. |
| `pr_converted_to_draft` | `pull_request.converted_to_draft` | Draft flag changed to true | Draft support varies | If unsupported, leave unmapped. |
| `pr_metadata_changed` | edited, labeled, assigned, unlabeled, unassigned | title/label/assignee/target branch update | title/reviewer/status metadata updates | Do not create review runs by default. |
| `pr_comment_context` | issue comment, review, review comment on PR | MR note/discussion | PR thread/comment | Context only unless bot is mentioned. |
| `agent_mention` | PR comment/review mentions bot | MR note mentions bot | PR thread/comment mentions bot | Requires provider-specific bot identity matching. |

Review runs should be created for `pr_opened`, `pr_updated`, `pr_reopened`, and
`pr_ready_for_review` by default. Metadata-only and comment-context events should
update context or create agent tasks without starting a full automated review
unless product policy explicitly changes.

## Comment Capabilities

Provider comment APIs differ more than webhook APIs. Model capabilities
explicitly and degrade to summary-only publishing when line placement is unsafe.

| Capability | GitHub MVP | GitLab target | Azure DevOps target | Required fallback |
| --- | --- | --- | --- | --- |
| Summary comment create | PR issue comment | MR note | PR thread with file path omitted or general comment | Store `ReviewCommentRef` when created. |
| Summary comment update | Edit prior bot comment by ID | Update MR note by ID when token permits | Update thread/comment when API permits | Create a new summary with a stable marker if update is unavailable. |
| Line comment | Review comment on diff position | Discussion with `position` fields | Thread with `threadContext` and file/line | Put finding in summary when position cannot be built. |
| Multi-line comment | GitHub supports ranges with constraints | GitLab supports line ranges in newer APIs | Azure support varies by API shape | Collapse to start line or summary-only. |
| Thread resolve | Review thread APIs | Discussion resolve API | Thread status update | Mark local ref stale when unsupported. |
| Bot mention detection | `@review-agent` in comment/review body | MR note body | Thread/comment content | Provider-specific bot login and identity config. |

Line comments are publishable only when all of these are true:

- Repository review config enables line comments.
- Provider adapter declares line comments supported.
- The finding path exists in the provider changed-file map.
- The finding line maps to a provider-commentable line or diff position.
- The provider token has permission to create the comment.

Otherwise keep the finding as summary-only with a reason such as
`file_not_changed`, `line_not_commentable`, `provider_line_comments_disabled`,
or `provider_permission_denied`.

## Fingerprints

Finding fingerprints must stay stable across provider adapters and independent
of provider-generated comment IDs. The orchestrator should continue generating
fingerprints from normalized review context:

- provider name
- repository full name or stable repository key
- pull request or merge request number
- base and head commit SHA
- normalized file path
- severity
- normalized finding message

Adapters must normalize paths to repository-relative POSIX-style paths before
result parsing. Do not include provider diff positions, thread IDs, or comment
IDs in the fingerprint because those can change when a platform recalculates
diffs or a comment is recreated.

## Authentication And Configuration

Keep provider credentials isolated by provider and deployment environment.

| Provider | Typical secrets | Webhook signature | API base URL |
| --- | --- | --- | --- |
| GitHub | `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY_PATH`, installation token, `GITHUB_WEBHOOK_SECRET` | `X-Hub-Signature-256` HMAC SHA-256 | `GITHUB_API_BASE_URL` |
| GitLab | bot/project/group token, app secret, `GITLAB_WEBHOOK_SECRET` | `X-Gitlab-Token` shared secret | `GITLAB_API_BASE_URL` |
| Azure DevOps | PAT or OAuth app credentials, webhook secret if configured | Service hook basic auth or configured secret | `AZURE_DEVOPS_ORG_URL` |
| Bitbucket | app password/OAuth consumer, webhook secret if configured | Workspace/webhook secret support varies | `BITBUCKET_API_BASE_URL` |
| GitCode | platform token/app credentials, webhook secret if configured | Follow GitCode webhook docs for HMAC/shared secret | `GITCODE_API_BASE_URL` |

Provider settings should be grouped clearly in `Settings` and `.env.example`.
Avoid overloading GitHub variables for GitLab or Azure. A multi-provider
deployment should use separate webhook URLs:

```text
/api/v1/webhooks/github
/api/v1/webhooks/gitlab
/api/v1/webhooks/azure-devops
```

Secrets should resolve to short-lived API clients or token refs at the adapter
boundary. Do not persist raw tokens in review-run, workspace, comment-ref, or
event-inbox rows.

## Data Model And Routing

The current tables already include `provider` in the key places. Preserve that
as the top-level partition for all provider-specific records:

- `ProviderEventInbox`: unique by `(provider, delivery_id)`.
- `PullRequestContext`: unique by `(provider, repo_full_name, pull_request_number)`.
- `ReviewRun`: unique by provider, repo, PR number, head SHA, and attempt.
- `ReviewCommentRef`: unique by provider, repo, PR number, and provider comment
  ID.
- `ReviewConfig`: unique by provider and repo.
- `Workspace`: unique by provider, repository, PR number, and head SHA.

When a platform has both a human-facing PR/MR number and an opaque ID, keep the
number in `pull_request_number` and store the opaque value in `provider_pr_id`.
When a platform repository name is not globally unique, set `repo_full_name` to a
stable provider-scoped full path and store opaque repository IDs in
`provider_repo_id`.

## Provider-Specific Notes

### GitHub

Keep GitHub as the reference implementation for the MVP. Preserve existing
behavior for duplicate delivery IDs, PR synchronize superseding, draft/ready
actions, mention-trigger tasks, and optional webhook signature enforcement.

MVP-required GitHub capabilities:

- Webhook ingest and normalization.
- PR context persistence.
- Review-run creation.
- Workspace preparation from clone URL and base/head SHA.
- Result parsing and finding reconciliation.
- Summary comment reference tracking.

Future GitHub enhancements:

- A concrete GitHub API client for changed files and comment publishing.
- Review thread lifecycle management.
- Rate limit backoff and retry classification.

### GitLab

GitLab uses merge request terminology but should normalize into the same internal
PR contract. Start with Merge Request Hook payload fixtures for opened, updated,
merged, closed, and reopened events.

GitLab-specific implementation points:

- Use project path or project ID as the repository key consistently.
- Use MR IID as `pull_request_number`; keep the global MR ID in `provider_pr_id`
  if needed.
- Map source/target branch SHAs to `head_sha` and `base_sha`.
- Build line comments from GitLab discussion `position` fields rather than
  GitHub-style diff positions.
- Treat unresolved discussions as provider threads when thread resolve is added.
- Use `X-Gitlab-Token` or the configured GitLab secret mechanism for webhook
  verification.

MVP-required GitLab capabilities:

- Webhook ingest for MR opened/updated/reopened/merged/closed.
- PR context extraction and review-run creation.
- Changed-file map sufficient to validate summary-only vs line-commentable
  findings.
- Summary comment create/update.

Later enhancements:

- Full discussion resolve support.
- Self-managed GitLab API base URL validation.
- Group-level token and project-level token policy checks.

### Azure DevOps

Azure DevOps service hooks use event names and resource shapes that differ from
GitHub. Normalize pull request created/updated/completed events into the same
internal vocabulary.

Azure-specific implementation points:

- Use organization, project, repository name or ID, and PR ID to build a stable
  repository key.
- Store the Azure PR ID in both `pull_request_number` when it is the human-facing
  ID and `provider_pr_id` when an additional opaque value is needed.
- Map completed pull requests to `pr_merged` when merge commit or completion
  metadata indicates success; abandoned/closed without completion maps to
  `pr_closed`.
- Model comments as PR threads. File/line comments require Azure thread context
  fields, not GitHub review comment positions.
- Azure permissions are often scoped by organization/project/repository; surface
  permission failures as provider errors that can downgrade publishing without
  failing result collection.

MVP-required Azure capabilities:

- Service hook ingest for PR created, updated, and completed.
- Signature or shared-secret validation based on deployment policy.
- PR context extraction and review-run creation.
- Summary comment create/update or summary append fallback.

Later enhancements:

- Thread status updates for resolved findings.
- Branch policy/check status integration.
- Organization-level rate limit and permission diagnostics.

### Bitbucket And GitCode

Treat these as follow-on adapters after GitLab or Azure proves the adapter
boundary. Both should reuse the same internal contracts:

- Normalize provider pull request events into `pr_*` internal events.
- Store opaque provider IDs separately from user-facing PR numbers.
- Build provider-specific commentability maps before enabling line comments.
- Start with summary comments if line comments require fragile diff-position
  mapping.

GitCode may resemble GitHub or GitLab depending on the target API surface, but it
should still get its own provider adapter, settings, fixtures, and contract
tests. Do not point GitCode traffic at the GitHub adapter unless the API contract
is explicitly verified.

## Testing Strategy

Each provider must have local-only tests before real integration tests are added.
Default `uv run pytest` should not require network access or provider
credentials.

Required provider test layers:

| Layer | Purpose | Fixture location |
| --- | --- | --- |
| Normalizer unit tests | Headers, signatures, event names, action mapping, missing fields, invalid payloads | `tests/fixtures/{provider}/webhooks/*.json` |
| Adapter contract tests | Changed files, commentability maps, summary comment upsert, line comment fallback, rate limit error mapping | `tests/fixtures/{provider}/api/*.json` |
| Service tests | Inbox idempotency, PR context persistence, review-run creation, superseding | Existing service/API test style with fake adapters |
| E2E/BDD tests | End-to-end webhook to review run to result reconciliation path | Extend `tests/e2e` helpers with provider parametrization |

Fixture rules:

- Keep raw provider payloads close to real webhook/API responses.
- Replace secrets, clone URLs, commit SHAs, and IDs with deterministic test
  values.
- Include at least one unsupported or ignored action fixture per provider.
- Include one fixture where a finding line cannot be mapped to a commentable
  diff line.
- Keep provider fixture directories separate; do not mutate GitHub fixtures to
  represent other providers.

Contract tests should assert that every adapter returns the same internal shapes
for equivalent events. They should also assert graceful degradation:

- unsupported provider action returns an ignored event;
- missing signature fails only when a secret is configured;
- duplicate delivery ID remains idempotent;
- unpublishable finding becomes summary-only;
- summary comment update falls back to create when provider update is
  unavailable.

## Rollout Checklist

Use this checklist when adding a provider:

- Add provider settings and `.env.example` entries.
- Add adapter module with webhook parsing, signature validation, event
  normalization, and PR context extraction.
- Register the adapter behind `/api/v1/webhooks/{provider}`.
- Add provider webhook fixtures and normalizer tests.
- Add service tests proving inbox idempotency and review-run creation.
- Add changed-file fixtures and commentability mapping tests.
- Add summary comment contract tests before enabling publishing.
- Keep line comments disabled until provider diff-position mapping is reliable.
- Add provider deployment notes, required webhook events, and token permissions.
- Run `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`.
- Run `UV_CACHE_DIR=/tmp/uv-cache uv run pytest`.

## Risks And Follow-Up Work

The current code lacks a concrete adapter registry and provider-neutral webhook
event type. That is acceptable for the GitHub MVP, but the first non-GitHub
provider should introduce the registry before adding large provider-specific
branches to the API route.

Provider comment publishing is also not yet a full adapter. Avoid coupling
finding reconciliation to any one provider's diff-position model. The stable
internal contract is `commentable_lines` plus provider-specific metadata carried
alongside it.

Do not make all providers support the same feature set on day one. The minimum
safe baseline for a new provider is webhook ingest, PR context, review-run
creation, workspace preparation, result parsing, and summary comment publishing.
Line comments, thread resolve, branch policy status, and advanced rate-limit
handling can follow once the adapter has contract coverage.
