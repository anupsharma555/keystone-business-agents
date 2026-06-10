---
skill_id: unsupported_claim_and_gap_handling
skill_version: 2026-05-31.2
skill_purpose: Preserve unsupported claims and missing evidence instead of filling gaps with plausible guesses.
applies_to:
  - gmail_triage
  - business_research_analyst
  - opportunity_scout
  - outreach_composer
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/source_attribution.jsonl
  - evals/local/outreach_copy_constraints.jsonl
  - evals/static/business_research_analyst_cases.json
validation_paths:
  - tests/test_business_research_analyst.py
  - tests/test_opportunity_scout.py
  - tests/test_outreach_composer.py
safety_notes:
  - Missing evidence should lower confidence or block external claims, not invite invention.
---

# Unsupported Claim And Gap Handling

## Purpose

Identify unsupported facts, weak personalization hooks, thin fit rationales,
unverified recommendations, stale evidence, and missing inputs.

## Applicable Agents

Business Research Analyst, Opportunity Scout, Outreach Composer, and
Orchestrator use this skill for source-aware reasoning and safe handoffs.

## Typical Inputs

- Draft claims, source bundles, approved context, CRM/contact notes, user
  requests, opportunity signals, and previous specialist outputs.

## Required Behavior

- Mark unsupported claims explicitly.
- Keep missing evidence visible in structured fields where possible.
- Prefer lower confidence, neutral wording, a validation step, or a blocked
  result over confident unsupported language.

## Flexible Behavior

- May preserve a weak claim as a hypothesis when the schema supports that
  distinction.
- May ask for clarification or recommend research when gaps materially affect
  the requested output.

## Boundaries

- Must not fill gaps with invented facts, customer names, outcomes, contacts,
  funding, clinical claims, or Keystone experience.
- Must not hide missing evidence to make an answer look complete.

## Reasoning Questions

- Which requested fields lack source support, approval, freshness, or identity
  confidence?
- Is a weak statement useful as a hypothesis, or would it mislead the operator
  or an external recipient?
- Does the missing evidence block the whole task, only lower confidence, or
  require a narrower answer?
- What validation step would resolve the gap?

## Decision Rubric

- Blocking gap: needed for safety, identity, approval, source sufficiency, or a
  requested retained result.
- Material limitation: output can proceed, but confidence or readiness is lower.
- Minor gap: note it only if relevant to the operator decision.
- Unsupported claim: remove from external copy and preserve in unsupported
  claims or missing evidence.

## Tie-Breakers

- For outbound copy, unsupported personalization is blocked, not softened into a
  confident claim.
- For internal analysis, hypotheses are allowed only when labeled.
- If evidence is absent, return fewer results or a thinner brief instead of
  padding.

## Output Contract

Return unsupported claims, missing evidence, confidence, limitations, risks, and
recommended validation or next action.

## Failure Modes

- Thin evidence, conflicting evidence, unapproved context, and absent sources
  should remain visible and should not become facts.

## Eval Criteria

- Unsupported personalization is removed or softened.
- Research gaps are preserved.
- Weak evidence lowers confidence.
- External-facing drafts do not contain unsupported facts.
