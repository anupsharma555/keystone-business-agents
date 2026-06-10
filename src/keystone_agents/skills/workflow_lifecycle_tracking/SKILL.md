---
skill_id: workflow_lifecycle_tracking
skill_version: 2026-05-31.2
skill_purpose: Track WorkItem, draft, approval, outreach, reply, outcome, artifact, and next-step state without inventing completion.
applies_to:
  - gmail_triage
  - business_research_analyst
  - opportunity_scout
  - outreach_composer
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/gmail_triage.jsonl
  - evals/local/orchestrator_routing.jsonl
  - evals/static/outreach_composer_cases.json
validation_paths:
  - tests/test_workflow_runner.py
  - tests/test_orchestrator.py
  - tests/test_outreach_composer.py
safety_notes:
  - Lifecycle state must come from records or explicit user updates, not inference.
---

# Workflow Lifecycle Tracking

## Purpose

Track where a business workflow stands across WorkItems, drafts, approvals,
manual sends, replies, outcomes, artifacts, and next steps.

## Applicable Agents

Gmail Triage, Outreach Composer, Opportunity Scout, Orchestrator, and Chief of
Staff use this skill when current work depends on prior state.

## Typical Inputs

- WorkItem state, approval queue items, draft records, outreach tracking rows,
  Gmail replies, artifact metadata, opportunity state, and operator updates.

## Required Behavior

- Distinguish draft created, Gmail draft created, approved, manually sent,
  replied, completed, blocked, and pending review.
- Never infer sent, approved, replied, or completed without state records or
  explicit user updates.
- Preserve stale, missing, or contradictory lifecycle evidence.

## Flexible Behavior

- May recommend next steps such as request approval, continue research, revise
  draft, update artifact, or stop.
- May summarize lifecycle state compactly for handoff.

## Boundaries

- Must not update lifecycle state unless an explicit tool and gate permit it.
- Must not present recommendations as completed actions.

## Reasoning Questions

- Which lifecycle object is in scope: WorkItem, draft, approval, opportunity,
  outreach, reply, artifact, or automation?
- What state is proven by records, and what is only inferred from text?
- Does the user ask to continue, revise, approve, send, mark done, or simply
  inspect status?
- What is the last safe next action that does not invent completion?

## Decision Rubric

- Proven state: backed by WorkItem, approval, draft, artifact, message, or
  explicit user update.
- Pending state: next action exists but no completion record exists.
- Blocked state: required approval, context, source, identity, or live gate is
  absent.
- Unknown state: records are missing or contradictory.

## Tie-Breakers

- Pending approval beats ambiguous user optimism.
- Draft created does not mean sent.
- Reply needed, reply drafted, reply sent, and reply received are distinct.
- If resume context is thin, preserve last safe next action and ask for the
  missing state rather than guessing.

## Output Contract

Return lifecycle status, evidence source, related IDs, blockers, pending
approvals, next step, and limitations.

## Failure Modes

- Missing records, stale state, duplicate drafts, ambiguous reply threads, and
  conflicting user updates should remain visible.

## Eval Criteria

- Pending approvals stay pending.
- Draft-only artifacts are not marked sent.
- Replies require explicit reply evidence.
- Resume flows preserve last safe next action.
