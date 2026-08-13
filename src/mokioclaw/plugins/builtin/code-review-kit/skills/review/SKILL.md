---
name: plugin-review
description: Structured code review focusing on correctness, security, and maintainability
invoke: manual
---

# Code Review (plugin)

Review the recent changes with this checklist:

1. Correctness — logic bugs, edge cases, off-by-one
2. Security — injection, path traversal, secrets
3. Maintainability — naming, duplication, dead code
4. Tests — missing coverage for new branches

Output a short report with findings ordered by severity (P0/P1/P2).
