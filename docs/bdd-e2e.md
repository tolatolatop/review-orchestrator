# E2E / BDD Test Matrix

The default E2E suite validates the GitHub PR review MVP with local-only
dependencies: SQLite, a temporary git repository, fixture GitHub payloads, and a
fake pi-agent client. It does not require GitHub credentials, a network call, a
real LLM/runtime service, or PostgreSQL.

Run the full default suite:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
```

Run only the BDD/E2E scenarios:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/e2e
```

## P0: Core Closed Loop

| Scenario | Coverage |
| --- | --- |
| Given a GitHub PR `opened` webhook, when the orchestrator receives a signed event, then it stores one inbox event, creates or updates PR context, queues one review run, and ignores duplicate delivery IDs. | `tests/e2e/test_pr_review_bdd.py::test_p0_pr_opened_runs_review_and_reconciles_published_state` |
| Given a queued review run, when a worker acquires it, then the run is locked, a local workspace is prepared from a temporary git repo, and a pi-agent session receives the commit-range input. | Same P0 test |
| Given a completed structured pi-agent result, when the result is collected, then schema validation marks publishable line findings, keeps invalid diff locations as summary-only, persists findings, upserts one summary comment ref, and dedupes line comment refs. | Same P0 test |
| Given an existing queued run, when a PR `synchronize` webhook arrives for a new head SHA, then the old run is superseded and the new head is queued. | `tests/e2e/test_pr_review_bdd.py::test_p0_given_pr_synchronize_when_new_head_arrives_then_old_run_is_superseded` |

## P1: Boundary And Failure Paths

| Scenario | Current coverage |
| --- | --- |
| Invalid GitHub webhook signature, missing headers, malformed payload, and unsupported provider/action. | API and GitHub unit tests; add E2E only if provider behavior spans multiple components. |
| GitHub API rate limit, permission denied, or diff too large. | Planned provider-client contract tests; default E2E should continue to use fixture diff data. |
| Workspace checkout/fetch failures, missing base/head, unsupported submodule/LFS behavior. | Workspace component tests; promote representative checkout failures to E2E as retry policy lands. |
| pi-agent failed, timeout, cancelled, invalid structured output, missing fields, or unpublishable line locations. | API/component tests cover session failure and parser validation; E2E currently covers one summary-only location. |
| Comment publish failure, retry, and idempotency. | Durable `ReviewCommentRef` idempotency is covered; provider retry needs a publish adapter before E2E can assert it. |

## P2: Archive And Operations Quality

| Scenario | Current coverage |
| --- | --- |
| PR closed/merged cleanup and finding archive. | Component coverage exists for merge cleanup pieces; full E2E should wait for provider cleanup orchestration. |
| Accepted, rejected, stale MVP metrics. | Reconciliation data model exists; metrics boundaries need product decisions before E2E assertions. |
| Summary comment displays new, existing, resolved, and unlocated findings. | Summary body helper is covered with new/resolved stats; richer rendering can be added when the publisher owns the final body format. |
| GitLab/Azure DevOps provider contract entry point. | Not implemented for MVP; future provider tests should reuse the same BDD helper shape. |

## Fixtures

- `tests/fixtures/github_pr_opened.json`
- `tests/fixtures/github_pr_synchronize.json`
- `tests/fixtures/pi_agent_review_result.json`
- `tests/fixtures/changed_files.json`

The GitHub fixtures use replacement tokens for `clone_url`, `base_sha`, and
`head_sha` so each test can generate an isolated temporary repository.
