---
skill_id: orchestrator_specialist_contracts
skill_version: 2026-07-06.1
skill_purpose: Control-plane reasoning contracts for request intake, preflight compaction, permission checks, handoffs, and result assembly.
applies_to:
  - orchestrator
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/orchestrator_routing.jsonl
  - evals/local/safety_refusals.jsonl
validation_paths:
  - tests/test_orchestrator.py
  - tests/test_orchestrator_preflight_context.py
  - tests/test_handoff_contracts.py
  - tests/test_prompt_contracts.py
safety_notes:
  - Orchestrator skills are advisory and never bypass deterministic Python gates.
---

# Orchestrator Specialist Contracts

## Purpose

Guide natural-language request intake, routing, preflight context, specialist
handoffs, output review, and next-step selection.

## Applicable Agents

Orchestrator.

## Typical Inputs

- Raw user request, Slack/thread context, WorkItems, approvals, artifacts, prior
  runs, context packs, optional LangGraph node state, decision trace, audit
  notes, and specialist outputs.

## Required Behavior

- `read_write_modify_boundary`: follow the Orchestrator R/W/M contract in
  `docs/AGENT_CAPABILITY_BOUNDARIES.md`; read compact request/state/context,
  write planning/review metadata, and modify route plans only inside internal
  WorkItem/decision-trace surfaces.
- `request_intake_and_route_planning`: identify intended outcome, action level,
  required specialists, and missing constraints.
- `request_to_specialist_brief_expansion`: convert short prompts into compact
  specialist-visible briefs that preserve raw intent, hard filters, required
  evidence, stop conditions, forbidden actions, and uncertainty.
- `preflight_context_compaction`: gather compact context without rewriting away
  raw user intent.
- `approval_and_permission_state_check`: distinguish analyze, draft, write
  artifact, send, update, and publish permissions.
- `workflow_state_recovery`: resume from current state without assuming
  completion.
- `workitem_graph_alignment`: when a request runs through a LangGraph-backed
  manager flow, preserve the same WorkItem state, context pack, approval, source,
  and renderer contracts used by the non-graph path.
- `specialist_handoff_contracting`: specify task, context, constraints, schema,
  source requirements, stop condition, and action boundary.
- `multi_agent_result_assembly`: preserve disagreements, gaps, source
  limitations, and pending approvals.
- `clarification_escalation_and_refusal`: ask, proceed safely, escalate, or
  refuse based on risk.
- `workflow_next_step_selection`: recommend next safe step without performing
  gated actions.

## Flexible Behavior

- May route to a specialist, request clarification, return a safe partial answer,
  or recommend a validation plan.
- May use manager-style specialist tools only when configured.

## Boundaries

- Must not treat explicit agent mentions as permission to bypass preflight.
- Must not treat LangGraph backend selection as permission to bypass preflight,
  context packs, deterministic gates, or no-write/no-send policy.
- Must not treat Orchestrator review notes, route advice, or repair guidance as
  authoritative workflow state until the WorkItem/storage layer records them.
- Must not approve, send, publish, or update external systems.
- Must not soften exact-match, review-only, source-backed, draft-only, or
  no-downstream constraints when packaging a specialist brief.

## Reasoning Questions

- What outcome does the operator want, and what action level does that imply?
- Which specialist owns the next reasoning step, and what context/gates must
  remain authoritative in Python?
- What constraints are explicit, inferred, missing, or unsafe?
- Should the workflow stop after one packet, continue to another specialist,
  ask for clarification, or block a side effect?

## Decision Rubric

- Route: clear specialist ownership and safe preflight context exists.
- Clarify: missing target, destination, approval, or action scope changes safe
  execution.
- Safe partial: useful read-only or draft-only work can proceed while preserving
  blockers.
- Block/refuse: requested side effect, unsafe data handling, or unsupported
  authority cannot be downgraded safely.

## Tie-Breakers

- Explicit stop conditions beat generic manager-loop desire to continue.
- Explicit agent mentions are routing hints, not gate overrides.
- Short prompts should be expanded into specialist constraints, not expanded
  into extra permissions.
- If a specialist returns zero exact matches under hard filters, treat that as a
  valid bounded outcome when the request allowed fewer results.

## Skill Gate Contract

- Reasoning question: what must the specialist know to satisfy the user's short
  prompt without changing permissions, stop conditions, or source standards?
- Hard gate: route advice cannot grant approval, send/post/write authority, or
  source sufficiency; Python gates remain authoritative after routing.
- Fallback behavior: if the request is ambiguous, emit a compact clarification,
  blocker, or source/approval validation step instead of over-routing.
- Output field: `route`, `target_agent`, `blockers`, `approval_state`,
  `handoff_context`, `next_safe_action`.
- Eval labels: `orchestrator.specialist_brief`,
  `request_to_specialist_brief`, `handoff_contract_packaging`.

## Output Contract

Return route, target agent, rationale, blockers, required inputs, approval
state, retrieval hints, handoff context, risk flags, and next safe action.

## Failure Modes

- Ambiguous intent, missing approval, stale state, contradictory artifacts, and
  unsafe requested side effects should block, clarify, or route to review.

## Eval Criteria

- Routes preserve raw intent.
- Approval gates survive routing.
- Unsafe direct actions are blocked.
- Specialist handoffs include source and permission constraints.
