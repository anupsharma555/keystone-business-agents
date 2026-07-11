# ANU-120 Target-Action Matrix

ANU-120 uses `src/keystone_agents/target_action_matrix.py` as the current
repo-local scorecard for natural asks that must resolve to the right owner,
target system, allowed tool tier, approval gate, and expected proof.

The matrix is deliberately data-first. It documents representative read,
research, compare, summarize, draft, label, save-plan, Airtable/CRM,
Workspace, Gmail, post, schedule, workflow, and artifact-backed asks without
adding phrase-specific planner branches for each prompt.

Focused coverage lives in `tests/test_target_action_matrix.py`. The tests
assert that the manual planner preserves the expected target action, keeps
approval-required actions draft/read-only or typed internal-write-plan only,
and blocks unsupported or underspecified post/schedule/write requests before
any live side effect. An authenticated exact internal write ask may proceed only
through the owning typed tool with provider flags, unique target identity,
approval reference, and read-back evidence; the matrix does not itself execute
provider mutations.
