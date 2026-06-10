---
skill_id: request_to_specialist_brief
skill_version: 2026-05-31.2
skill_purpose: Expand short operator requests into compact, loss-aware specialist briefs without changing route authority or safety gates.
applies_to:
  - orchestrator
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/orchestrator_routing.jsonl
validation_paths:
  - tests/test_skill_evals.py
  - tests/test_prompt_contracts.py
safety_notes:
  - Brief expansion is advisory context only; Python gates remain authoritative.
  - Must preserve the raw request and must not add permissions, approvals, sources, or claims.
---

# Request To Specialist Brief

## Purpose

Convert terse or ambiguous operator wording into a compact specialist brief that
helps the selected agent do the right work without making the brief a hidden
router, tool policy, or approval grant.

## Required Behavior

- Preserve the raw operator request as the primary intent record.
- Add a `specialist_brief` with objective, selected specialist, hard filters,
  soft preferences, required evidence, source visibility needs, stop condition,
  forbidden actions, uncertainty to preserve, and next safe action.
- Separate inferred constraints from explicit constraints.
- When the operator asks for exact matches, source-backed claims, approval
  safety, draft-only behavior, or no downstream work, make those constraints
  visible to the specialist.
- For short prompts, infer a safe minimum brief from route, context pack,
  WorkItem state, approval state, and prior trace rather than asking the
  specialist to rediscover obvious control-plane constraints.

## Flexible Behavior

- May leave fields empty when the evidence is absent.
- May propose retrieval hints, but provider selection stays in Python retrieval
  policy.
- May recommend clarification when missing context changes the safe route or
  output contract.

## Boundaries

- Must not rewrite away the original wording.
- Must not invent source-backed facts, dates, deadlines, eligibility, contacts,
  approval status, or prior work.
- Must not convert "draft", "review", "find", or "analyze" into permission to
  send, publish, schedule, write externally, or update CRM.
- Must not pad retained results when hard filters are unmet.

## Reasoning Questions

- What is the smallest faithful objective implied by the raw request?
- Which constraints are explicit, and which are safe inferred defaults from the
  route, context pack, or WorkItem state?
- What evidence, approval, source visibility, and stop condition must the
  specialist see before reasoning?
- What should remain uncertain rather than being hardened into the brief?

## Decision Rubric

- Good brief: preserves raw request, exposes hard filters, separates explicit
  and inferred constraints, states forbidden actions, and names the output
  contract.
- Too weak: omits source, approval, stop, or exact-match requirements.
- Too strong: invents facts, deadlines, eligibility, prior work, or approval.
- Unsafe: expands a read/draft/review request into external action permission.

## Tie-Breakers

- When prompts are short, expand safety and output constraints before expanding
  domain assumptions.
- If the user requests exact matches, the brief must allow zero retained results.
- If the route is advisory but Python gates own safety, say so in the brief
  rather than pretending the model can grant permissions.

## Output Contract

Include compact fields when available:

- `raw_request`
- `objective`
- `selected_specialist`
- `explicit_constraints`
- `inferred_constraints`
- `required_evidence`
- `source_visibility_requirement`
- `approval_or_permission_state`
- `stop_condition`
- `forbidden_actions`
- `uncertainties_to_preserve`
- `next_safe_action`

## Failure Modes

- If the brief cannot distinguish explicit from inferred constraints, flag the
  uncertainty instead of hardening it.
- If the user asks for a gated side effect, keep the brief draft-only or route to
  clarification/manual review.
- If the user asks for exact matches and none are source-backed, return fewer or
  zero retained items and list gaps.

## Eval Criteria

- Short prompts still produce specialist-visible constraints.
- Stop conditions survive orchestration.
- Exact-match requirements are not softened into broad discovery.
- Source and approval requirements remain visible but do not bypass deterministic
  gates.
