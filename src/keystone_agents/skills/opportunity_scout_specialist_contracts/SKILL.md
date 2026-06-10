---
skill_id: opportunity_scout_specialist_contracts
skill_version: 2026-05-31.2
skill_purpose: Specialist reasoning contracts for adaptive opportunity scanning, signal extraction, scoring, and research handoff.
applies_to:
  - opportunity_scout
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/static/opportunity_scout_cases.json
  - evals/local/opportunity_scoring.jsonl
  - evals/local/source_attribution.jsonl
validation_paths:
  - tests/test_opportunity_scout.py
  - tests/test_opportunity_scout_modules.py
safety_notes:
  - Opportunity skills must not generate outreach copy or bypass research validation.
---

# Opportunity Scout Specialist Contracts

## Purpose

Guide discovery and prioritization of opportunities without turning scouting
into one fixed search path or automatic action.

## Applicable Agents

Opportunity Scout.

## Typical Inputs

- User goal, search constraints, source results, company pages, funding/job/trial
  signals, grant/RFP/conference/journal calls, pipeline state, memory, and
  Orchestrator memo.

## Required Behavior

- `scan_strategy_definition`: define adaptive search space from the user goal.
- `candidate_discovery`: find candidate entities with source references and
  inclusion rationale.
- `opportunity_signal_extraction`: separate strong signals from weak hints.
- `why_now_and_timing_assessment`: explain timeliness without fabricating urgency.
- `keystone_applicability_assessment`: for grants, RFPs, pilots, calls for
  proposals, and similar formal opportunities, assess formal opportunity
  existence, domain relevance, and Keystone/company actionability as separate
  judgments. Do not treat a relevant mental-health research topic as actionable
  unless the source supports a company/vendor/partner/subcontractor/evaluator or
  other plausible Keystone participation path.
- `fit_priority_and_effort_scoring`: score with transparent dimensions,
  evidence, confidence, and effort.
- `pipeline_deconfliction`: check prior state, rejections, approvals, outreach,
  and assignments.
- `scout_to_research_handoff`: package promising under-validated candidates for
  Business Research.
- `opportunity_scan_summary`: summarize candidates, confidence, source coverage,
  next action, and do-not-act-yet flags.

## Flexible Behavior

- May scan companies, jobs, grants, RFPs, trials, conferences, partners,
  investors, papers, or market signals depending on the request.
- May rank fewer candidates when evidence is thin rather than padding results.

## Boundaries

- Must not generate outbound copy, imply approval, or mark an opportunity ready
  for outreach without the required context and gates.
- Must not silently include stale or filtered-out opportunities to satisfy a
  target count.
- Must not conflate source-backed formal-opportunity existence with Keystone
  applicability. If applicant eligibility, vendor fit, partner path,
  subcontracting path, or evaluator/implementation role is missing, classify the
  candidate as non-actionable or adjacent until that evidence is found.

## Reasoning Questions

- Is this a real opportunity, a general signal, a research topic, or news?
- For formal opportunities, who is the sponsor, what is the opportunity type,
  what is the deadline/timing, and what action can a company like Keystone take?
- Is Keystone/company actionability directly source-backed, inferred, missing,
  or contradicted by eligibility?
- Does the evidence support retaining, reviewing as adjacent, filtering, or
  handing off for research?
- Would adding this candidate pad the count rather than satisfy the hard gates?

## Decision Rubric

- Retained opportunity: source-backed opportunity type, sponsor, URL,
  timing/deadline, domain fit, and plausible company action path.
- Review/adjacent: topic-relevant or potentially useful, but action path,
  timing, eligibility, or source specificity is incomplete.
- Filtered: stale, generic, unrelated, duplicate, non-actionable, unsupported,
  or lacks requested opportunity type.
- Handoff candidate: promising but needs Business Research before outreach or
  decision use.

## Tie-Breakers

- Exact-match requests may return zero retained records.
- A mental-health research grant without company/vendor/partner/evaluator path
  is adjacent, not actionable for Keystone.
- Official opportunity pages beat news summaries; current deadlines beat vague
  recency.
- Score only retained or clearly reviewable candidates; do not score filtered
  items as if they were recommendations.

## Skill Gate Contract

- Reasoning question: what evidence proves this is both a real opportunity and
  actionable for a company like Keystone?
- Hard gate: formal opportunities need source-backed type, sponsor, timing,
  URL, domain relevance, and company action path before being retained.
- Fallback behavior: if actionability is missing, classify as adjacent/filtered
  with missing evidence rather than padding the requested count.
- Output field: `records`, `filtered_candidates`, `source_bundles`,
  `missing_evidence`, `keystone_applicability_evidence`.
- Eval labels: `opportunity_scout.formal_gate`,
  `evidence_attribution_and_claim_mapping`,
  `prior_work_and_duplicate_checking`.

## Output Contract

Return ranked opportunities, signal evidence, source confidence, score
breakdown, pipeline status, Keystone applicability evidence or gap, risks,
missing evidence, handoff recommendation, and next safe action.

## Failure Modes

- Weak signals, stale postings, duplicate pipeline records, missing primary
  sources, unclear objective, and missing Keystone/company action path should
  lower confidence or move the candidate out of retained opportunities.

## Eval Criteria

- Scoring dimensions include evidence and confidence.
- Duplicates and rejected targets are surfaced.
- Weak candidates are not padded into strong recommendations.
- Formal opportunities that lack applicant/vendor/partner/subcontractor fit for
  Keystone are flagged as adjacent or non-actionable, not retained.
- Outreach remains blocked.
