---
skill_id: handoff_contract_packaging
skill_version: 2026-05-31.2
skill_purpose: Package compact but loss-aware context for specialist and multi-agent handoffs.
applies_to:
  - gmail_triage
  - business_research_analyst
  - opportunity_scout
  - outreach_composer
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/orchestrator_routing.jsonl
  - evals/local/source_attribution.jsonl
validation_paths:
  - tests/test_handoff_contracts.py
  - tests/test_orchestrator_preflight_context.py
  - tests/test_context_packs.py
safety_notes:
  - Handoffs preserve context and constraints; they never bypass gates or approvals.
---

# Handoff Contract Packaging

## Purpose

Create compact handoff context that preserves raw user intent, constraints,
source evidence, approval state, missing information, risks, and requested
output.

## Applicable Agents

Orchestrator owns most handoff packaging, but all specialists may produce or
consume handoff-ready context.

## Typical Inputs

- Raw user request, Orchestrator memo, WorkItem state, context packs, source
  bundles, approval records, prior specialist outputs, blockers, and audit notes.

## Required Behavior

- Preserve the original request wording or a faithful reference to it.
- Include approval state, source limitations, missing inputs, action boundaries,
  and expected output contract.
- Avoid over-compressing away caveats, uncertainty, or unsupported assumptions.

## Flexible Behavior

- May summarize verbose sources or state when the source IDs and limitations are
  preserved.
- May ask for clarification when the receiving specialist cannot safely proceed.

## Boundaries

- Must not convert routing advice into permission.
- Must not remove blockers or unsupported claims from downstream context.
- Must not include raw secrets, PHI, or unnecessary private bodies.

## Reasoning Questions

- What does the receiving agent need to act safely, and what can be omitted?
- Which constraints are explicit user requirements versus Orchestrator or agent
  inferences?
- Which source IDs, approval states, blockers, and missing evidence must survive
  compression?
- What stop condition or forbidden action must the receiving agent honor?

## Decision Rubric

- Complete handoff: raw intent, target agent, task, evidence, approval state,
  blockers, output contract, and next safe action are present.
- Partial handoff: enough for safe internal analysis, but missing inputs are
  explicit.
- Unsafe handoff: loses approval/source boundaries, hides blockers, or converts
  advice into permission.
- Clarification needed: target, recipient, source basis, or action boundary is
  ambiguous.

## Tie-Breakers

- Preserve fewer high-value constraints over many low-value details.
- Prefer source IDs and summaries over full private bodies.
- If compressed context could mislead the next agent, pass a limitation rather
  than a confident summary.

## Output Contract

Return target agent, task, compact context, source refs, approval state,
blockers, missing inputs, requested output, and next safe action.

## Failure Modes

- Missing source refs, ambiguous route, stale approval state, and oversummarized
  context should produce a clarification, blocker, or repair request.

## Eval Criteria

- Handoffs preserve raw intent.
- Approval gates survive specialist routing.
- Source and missing-evidence context remains visible.
- Private bodies are minimized or omitted.
