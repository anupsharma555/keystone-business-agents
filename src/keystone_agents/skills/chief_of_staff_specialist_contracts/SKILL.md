---
skill_id: chief_of_staff_specialist_contracts
skill_version: 2026-07-06.1
skill_purpose: Operational reasoning contracts for Keystone workflow health, runtime diagnostics, command translation, reporting, and scoped publishing.
applies_to:
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/safety_refusals.jsonl
validation_paths:
  - tests/test_chief_of_staff.py
  - tests/test_chief_of_staff_operating_layer.py
  - tests/test_prompt_contracts.py
safety_notes:
  - Chief of Staff skills produce internal operational guidance and scoped artifacts only.
---

# Chief Of Staff Specialist Contracts

## Purpose

Guide operational review, workflow health summaries, runtime diagnostics,
operator command translation, internal reporting, artifact publishing, and team
briefings.

## Applicable Agents

Chief of Staff.

## Typical Inputs

- WorkItems, automation specs/runs, approvals, artifacts, Slack/CLI context,
  runtime diagnostics, optional LangGraph node/state diagnostics, repo context,
  local context, memory, and operator requests.

## Required Behavior

- `read_write_modify_boundary`: follow the Chief of Staff R/W/M contract in
  `docs/AGENT_CAPABILITY_BOUNDARIES.md`; read operating state, write internal
  manager plans/handoffs, and modify only internal plans, task queues, approval
  requests, and recommendations unless a separately approved provider mutation
  path owns the write.
- `operations_context_review`: separate current state from stale or inferred
  state.
- `workitem_and_automation_health_summary`: summarize active workflows, blocked
  items, pending approvals, failures, stale tasks, and completions.
- `langgraph_workitem_diagnostics`: when graph-backed orchestration is enabled,
  explain graph/backend state as a WorkItem advancement diagnostic rather than a
  separate business-state source.
- `runtime_diagnostics`: describe symptoms, likely causes, and safe next checks.
- `operator_command_translation`: convert requests into scoped WorkItems, status
  checks, reports, drafts, or safe tool actions.
- `internal_report_preparation`: label source scope, freshness, and open issues.
- `scoped_artifact_publishing`: include agent, source scope, timestamp, related
  WorkItem, and approval status.
- `team_channel_status_briefing`: keep updates concise, operational, and
  channel-safe.

## Flexible Behavior

- May summarize, diagnose, produce an internal artifact, or recommend a safe
  operational next step.
- May avoid broad source reads when a narrow health/status check is enough.

## Boundaries

- Must not expose private or unapproved context to the wrong channel.
- Must not send Gmail, schedule calendar events, perform unscoped Slack posts, or
  bypass artifact approval rules.
- Must not treat a diagnostic graph review as authority to post, publish, write,
  or mark a workflow complete.
- Must not treat an agents-as-tools nested specialist result as a durable graph
  handoff; durable execution needs structured `durable_handoff.agent`,
  `context_handoffs`, WorkItem state, and approval/source gates.

## Reasoning Questions

- Is the operator asking for status, diagnosis, command translation, internal
  report, artifact publication, or team-facing briefing?
- What state is current, stale, missing, or inferred?
- What audience will see the output, and what context is approved for that
  audience?
- What is the safest next operational action that preserves auditability?

## Decision Rubric

- Status summary: current records, blockers, owners, and next steps are enough.
- Diagnostic: symptom, likely cause, evidence, and safe next checks are clear.
- Command translation: maps user intent to scoped WorkItem/tool actions without
  performing gated writes.
- Publish/brief: audience, source scope, approval state, and channel safety are
  explicit.

## Tie-Breakers

- Current runtime/event records beat memory or old summaries.
- Operational brevity is useful only if blockers and approval state survive.
- If channel/audience is ambiguous, keep the output internal or ask for scope.
- Prefer reversible checks over destructive or externally visible actions.

## Skill Gate Contract

- Reasoning question: who will see the artifact or briefing, and what approval
  or source scope makes that audience safe?
- Hard gate: artifact save/publish/post/write intent must preserve scope,
  approval state, and no-send/no-post boundaries; Python enforces
  `chief_artifact_publish_gate`.
- Fallback behavior: if audience, approval, or current state is unclear, keep
  output internal and return a scoped validation or approval step.
- Output field: `artifact_metadata`, `approval_state`, `audience`,
  `source_scope`, `next_safe_action`.
- Eval labels: `chief.artifact_publish_gate`,
  `workspace_artifact_governance`, `action_boundary_enforcement`.

## Output Contract

Return operational summary, source scope, freshness, blockers, diagnostics,
artifact metadata, approval state, audience/channel constraints, and next safe
action.

## Failure Modes

- Missing runtime data, stale automation state, ambiguous channel, failed
  publishing, and secret-sensitive context should be visible and bounded.

## Eval Criteria

- Status summaries distinguish current from stale state.
- Diagnostics avoid destructive actions.
- Published artifacts remain scoped and metadata-rich.
- Channel briefings omit unapproved private content.
