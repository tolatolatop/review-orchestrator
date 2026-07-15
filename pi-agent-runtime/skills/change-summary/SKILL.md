---
name: change-summary
description: Explain a pull request commit range as a structured change summary for a selected audience.
---

# Change summary

Inspect the complete base-to-head commit range and explain the meaningful behavior,
API, data-flow, configuration, and operational changes. Group related edits by area
instead of listing files mechanically.

Use repository evidence and distinguish confirmed behavior from inference. This is
not a defect review: mention risks that a reviewer or release manager should know,
but do not manufacture findings or style criticism.

Finish exactly once by calling `submit_change_summary`. Do not emit the structured
result as ordinary assistant text.
