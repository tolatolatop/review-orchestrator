# pi-agent Chain Verification

This temporary document exists only to exercise the deployed pull-request
review chain on 2026-07-15.

Expected result:

- GitHub delivers the pull-request webhook to Review Orchestrator.
- The worker prepares a read-only, head-SHA-isolated workspace.
- pi-agent reviews the commit range and submits a structured result.
- Review Orchestrator publishes the result back to this pull request.

No production behavior or configuration is changed by this verification.
