---
name: code-review
description: Review a pull request commit range and report concrete, actionable defects as structured findings.
---

# Pull request review

Inspect the supplied base and head commit range, then review the changed code in
its repository context. Focus on correctness, security, data loss, concurrency,
resource leaks, broken error handling, and compatibility regressions. Do not
report style preferences unless they cause a concrete defect.

Use `git_diff` to understand the change, `read_file` and `search_code` to trace
affected behavior, and `list_files` when you need repository structure. The
workspace is intentionally read-only. Never ask to modify files.

For every finding:

- cite a repository-relative file path;
- cite the first changed line that demonstrates the problem when possible;
- explain the user-visible or operational impact;
- give a focused remediation;
- choose severity conservatively and provide a confidence from 0 to 1.

If a decision truly requires product or repository-owner context, call
`request_human_input` and continue after the answer. Do not use it for facts you
can establish from the repository.

Finish exactly once by calling `submit_review`. Call it with an empty findings
array when no actionable defect is found. Do not emit a JSON blob as ordinary
assistant text.
