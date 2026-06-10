---
skill_id: prior_work_and_duplicate_checking
skill_version: 2026-05-31.2
skill_purpose: Surface prior related work and possible duplicates without turning duplicate checks into brittle blockers.
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
  - evals/local/opportunity_scoring.jsonl
  - evals/static/outreach_composer_cases.json
validation_paths:
  - tests/test_agent_registry.py
  - tests/test_gmail_triage.py
  - tests/test_opportunity_scout.py
  - tests/test_outreach_composer.py
safety_notes:
  - Duplicate findings are decision support and never authorize state mutation.
---

# Prior Work And Duplicate Checking

## Purpose

Detect whether similar work, entities, opportunities, drafts, approvals,
outreach records, or internal artifacts already exist.

## Applicable Agents

Gmail Triage, Opportunity Scout, Outreach Composer, Orchestrator, and Chief of
Staff use this contract when current work may overlap with prior Keystone state.

## Typical Inputs

- WorkItems, approval queue items, memory records, pipeline rows, outreach
  tracking records, Gmail thread IDs, draft IDs, company names, opportunity
  records, and artifact metadata.

## Required Behavior

- Distinguish exact, likely, possible, and no duplicate.
- Preserve enough rationale for an operator or downstream agent to decide
  whether to continue, revise, or stop.
- Treat prior rejection, pending approval, or active assignment as state context,
  not as a hidden instruction to mutate anything.

## Flexible Behavior

- May continue with a new record when the possible duplicate is weak and the
  output explains why.
- May recommend review, merge, handoff, or research validation as the next step.

## Boundaries

- Must not delete, merge, archive, approve, or mark work complete.
- Must not block all new work solely because a fuzzy match exists.

## Reasoning Questions

- What prior state matters: duplicate entity, duplicate opportunity, duplicate
  outreach, pending approval, rejected target, active assignment, or saved
  artifact?
- Is the match exact, likely, possible, or only topical overlap?
- Does the prior state change the current safe next action, or merely add
  context?
- Could continuing create duplicate outreach, conflicting approvals, or stale
  artifacts?

## Decision Rubric

- Exact duplicate: same stable ID, thread, artifact, draft, approval, domain, or
  pipeline key.
- Likely duplicate: same entity plus same objective or opportunity signal.
- Possible duplicate: similar entity or topic but different objective, timing,
  channel, or missing stable IDs.
- Not duplicate: different entity, distinct objective, or stale context that no
  longer maps to the current task.

## Tie-Breakers

- Prior rejection and pending approval are stronger than generic memory.
- A possible duplicate should slow or label the workflow, not erase useful new
  work.
- If duplicate status affects an external action, require review before action.

## Output Contract

Return duplicate status, matched record IDs where available, rationale,
confidence, limitations, and next safe action in the active schema.

## Failure Modes

- Missing state, stale records, weak fuzzy matches, and conflicting lifecycle
  status should be recorded as uncertainty.

## Eval Criteria

- Exact duplicates are flagged.
- Possible duplicates remain possible.
- Rejected or pending records do not become approval.
- No mutation is implied by duplicate detection.
