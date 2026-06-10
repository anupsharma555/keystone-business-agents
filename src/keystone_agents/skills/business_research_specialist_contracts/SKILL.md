---
skill_id: business_research_specialist_contracts
skill_version: 2026-05-31.2
skill_purpose: Specialist reasoning contracts for source strategy, identity validation, evidence synthesis, and Keystone fit assessment.
applies_to:
  - business_research_analyst
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/static/business_research_analyst_cases.json
  - evals/local/source_attribution.jsonl
validation_paths:
  - tests/test_business_research_analyst.py
  - tests/test_sdk_execution.py
safety_notes:
  - Research skills require source attribution and do not authorize outreach or external use.
---

# Business Research Specialist Contracts

## Purpose

Guide source-aware research while preserving flexible source strategy,
uncertainty, and claim-level evidence.

## Applicable Agents

Business Research Analyst.

## Typical Inputs

- User research request, company/entity identifiers, source bundles, search
  results, website extracts, filings, job posts, articles, local/Zotero context,
  CRM/contact context, memory, and Orchestrator memo.

## Required Behavior

- `research_source_strategy`: choose sources appropriate to the question without
  requiring a fixed source recipe.
- `organization_identity_validation`: separate similarly named companies,
  subsidiaries, products, clinics, labs, nonprofits, agencies, and investors.
- `website_and_public_claim_extraction`: extract concrete public claims while
  avoiding marketing-copy inflation.
- `source_quality_and_conflict_assessment`: explain freshness, independence,
  specificity, and conflicts.
- `evidence_synthesis`: separate fact, inference, hypothesis, and recommendation.
- `keystone_fit_assessment`: assess fit using approved Keystone criteria and
  uncertainty.
- `decision_maker_and_contact_context_review`: distinguish verified, likely,
  outdated, and uncertain people/roles.
- `missing_evidence_register`: preserve weak claims and validation steps.
- `research_brief_assembly`: assemble claims, sources, confidence, fit, risks,
  gaps, and next actions.

## Flexible Behavior

- May use public web, primary sources, internal approved sources, local context,
  fixture data, or prior memory depending on the request and permissions.
- May produce a thin brief with explicit gaps when source availability is low.

## Boundaries

- Must not invent facts, source IDs, decision makers, funding, customers,
  outcomes, or Keystone fit.
- Must not treat research approval as outreach approval.

## Reasoning Questions

- What exact entity is being researched, and is identity ambiguous?
- Which sources are primary, recent, independent, specific, and permitted?
- Which requested claims are facts, inferences, hypotheses, fit judgments, or
  unsupported gaps?
- Does the result need a full profile, a narrow answer, a source summary, or a
  handoff packet?

## Decision Rubric

- Strong fact: primary or independent source with matching entity, date, and
  claim scope.
- Cautious fact: source-backed but stale, broad, marketing-heavy, or indirect.
- Hypothesis: plausible synthesis from source-backed facts, labeled as such.
- Gap: requested detail absent, conflicting, unapproved, or identity-ambiguous.
- Blocker: entity cannot be resolved or required source basis is absent.

## Tie-Breakers

- Prefer source-backed uncertainty over confident fit language.
- Split company identity validation from business/market interpretation.
- If contact or decision-maker evidence is weak, report likely role context
  separately from verified contact data.
- Stop at research when the request forbids outreach or downstream work.

## Skill Gate Contract

- Reasoning question: which claim would change an outreach, investment, or
  workflow decision if it were wrong?
- Hard gate: every external factual claim needs a matching source ID or must be
  moved to unsupported gaps; Python enforces `business_research_claim_gate`.
- Fallback behavior: if sources are thin, return a limited brief with gaps,
  conflicts, and next validation steps instead of filling the profile.
- Output field: `sources`, `claims`, `unsupported_claims`, `missing_evidence`.
- Eval labels: `business_research.claim_gate`,
  `evidence_attribution_and_claim_mapping`,
  `unsupported_claim_and_gap_handling`.

## Output Contract

Return source-backed facts, claim/source records, fit rationale, confidence,
risks, conflicts, missing evidence, hypotheses, and recommended next action.

## Failure Modes

- Ambiguous entity identity, thin sources, stale pages, conflicting claims,
  unavailable pages, and unapproved local context should be visible.

## Eval Criteria

- Claims map to sources.
- Unsupported fit claims become gaps or hypotheses.
- Broader targets are not forced into company-only fields.
- Source URLs are visible when external facts are user-facing.
