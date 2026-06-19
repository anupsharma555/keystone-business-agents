---
skill_id: structured_output_quality_review
skill_version: 2026-05-31.2
skill_purpose: Review final structured output for schema, source, approval, and action-boundary completeness.
applies_to:
  - gmail_triage
  - business_research_analyst
  - opportunity_scout
  - outreach_composer
  - airtable_context_agent
  - google_workspace_context_agent
  - zotero_context_agent
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/source_attribution.jsonl
  - evals/local/outreach_copy_constraints.jsonl
  - evals/static/business_research_analyst_cases.json
  - evals/static/outreach_composer_cases.json
validation_paths:
  - tests/test_business_research_analyst.py
  - tests/test_outreach_composer.py
  - tests/test_sdk_execution.py
safety_notes:
  - Review catches concrete issues but does not override deterministic Python validation.
---

# Structured Output Quality Review

## Purpose

Use this skill before returning a structured agent output that will be validated,
rendered, saved, handed off, or reviewed by a human operator.

## Applicable Agents

All registered Keystone agents may use this shared contract before returning
Pydantic structured output.

## Typical Inputs

- Draft structured output, source bundle, approval state, unsupported claims,
  blocked context, action flags, limitations, and expected output schema.

## Required Behavior

- Check that required schema fields are populated or that missing fields are
  explained as blockers, gaps, or limitations.
- Check source coverage for factual claims and preserve visible citations when
  required by the workflow.
- Check approval state, no-send flags, unsupported claims, blocked context,
  action boundaries, and next safe steps.
- Keep human-facing summaries concise and focused on operator decisions.

## Flexible Behavior

- May revise wording inside bounded narrative fields to improve clarity,
  concision, or tone.
- May lower confidence, add a limitation, or request validation when the output
  is structurally valid but evidence is thin.

## Boundaries

- Must not rewrite domain judgments without a concrete schema, source, safety, or
  clarity reason.
- Must not remove risk flags, unsupported claims, blocked context, or missing
  evidence to make output look cleaner.
- Must not convert recommendations into completed actions.

## Reasoning Questions

- Does the output answer the actual request and preserve hard constraints?
- Are required schema fields present, or are missing fields explained as gaps or
  blockers?
- Are factual claims source-backed, permission-eligible, and visible where
  required?
- Do action, approval, no-send, lifecycle, and next-step fields agree?

## Decision Rubric

- Pass: schema-valid, source/permission/action boundaries intact, and useful to
  the operator.
- Warn: structurally valid but thin, partial, stale, or confidence-limited.
- Repair: missing required fields, confusing next action, hidden limitations, or
  unsupported claims that can be corrected.
- Block: unsafe side effect, approval bypass, PHI/private exposure, or no source
  basis for required factual output.

## Tie-Breakers

- Keep limitations visible even when they make the answer less polished.
- Prefer fewer retained records or a thinner brief over schema-shaped padding.
- If final copy conflicts with structured fields, structured safety fields win.

## Output Contract

The final output should validate against the active Pydantic schema and preserve
source IDs, unsupported claims, limitations, approval state, no-send state,
blocked reasons, and recommended next actions.

## Failure Modes

- Missing required fields, orphan claims without sources, hidden blocked
  context, accidental send-enabled flags, verbose debug output, and weak next
  steps should be corrected or surfaced as limitations.

## Eval Criteria

- Outputs validate against the schema.
- Approval and no-send fields remain intact.
- Missing evidence and unsupported claims are preserved.
- Human-facing fields are concise and decision-useful.
