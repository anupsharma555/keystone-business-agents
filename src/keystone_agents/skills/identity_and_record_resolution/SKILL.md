---
skill_id: identity_and_record_resolution
skill_version: 2026-05-31.2
skill_purpose: Normalize and compare business entities and workflow records without silently merging them.
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
  - evals/static/business_research_analyst_cases.json
  - evals/static/outreach_composer_cases.json
validation_paths:
  - tests/test_agent_registry.py
  - tests/test_business_research_analyst.py
  - tests/test_outreach_composer.py
safety_notes:
  - This skill never authorizes record merges, writes, sends, or approval changes.
---

# Identity And Record Resolution

## Purpose

Use this skill when an agent must compare or normalize companies, domains,
people, Gmail threads, WorkItems, drafts, artifacts, source IDs, or approval
records before using them in a structured output.

## Applicable Agents

All registered Keystone agents may use this shared contract when comparing
entities, records, source IDs, artifacts, or workflow state.

## Typical Inputs

- User request and raw target names.
- Source bundles, CRM/contact records, Gmail/thread metadata, WorkItem state,
  approval items, draft IDs, artifact IDs, and prior run summaries.

## Required Behavior

- Preserve the distinction between exact matches, likely matches, possible
  matches, and no match.
- State the identifiers used for comparison, such as domain, name, thread ID,
  source ID, artifact ID, approval ID, or WorkItem ID.
- Surface ambiguous or conflicting identity evidence instead of choosing one
  silently.
- Keep unresolved identity questions in missing evidence, limitations, blockers,
  or next-step fields when the active schema supports them.

## Flexible Behavior

- May use deterministic helper results, approved local records, source bundles,
  Gmail metadata, CRM context, or user-provided identifiers when available and
  permitted.
- May proceed with a reversible assumption when the output clearly marks the
  assumption and avoids external side effects.

## Boundaries

- Must not auto-merge records or treat a possible match as an exact match.
- Must not create or update CRM, Gmail, Workspace, Slack, or approval state.
- Must not infer identity from vague similarity when stronger identifiers are
  missing.

## Reasoning Questions

- What object type is being resolved: company, person, thread, WorkItem,
  artifact, source, draft, approval, or external destination?
- Which identifiers are strong, such as domain, URL, email, thread ID,
  artifact ID, source ID, approval ID, or canonical key?
- Which identifiers are weak, such as similar names, overlapping topics,
  inferred geography, role title, or stale memory?
- Would a wrong merge cause an unsafe write, wrong attribution, duplicate
  outreach, or misleading handoff?

## Decision Rubric

- Exact match: stable identifiers match and no material conflict is present.
- Likely match: several strong signals align, but one stable identifier is
  absent or stale.
- Possible match: name/topic similarity exists without enough stable
  identifiers.
- No match: stable identifiers conflict or the evidence points to different
  entities.

## Tie-Breakers

- Prefer stable identifiers over name similarity.
- Prefer source/current WorkItem IDs over model memory.
- When ambiguity affects external use or record mutation, ask for clarification
  or return a validation step.
- When ambiguity affects only internal analysis, proceed only with a labeled
  assumption.

## Output Contract

Record the resolved identity, match confidence, evidence used, unresolved
ambiguity, and safe next action in the current agent's schema fields.

## Failure Modes

- Ambiguous identity, conflicting aliases, stale records, missing IDs, or
  possible duplicate artifacts should produce explicit uncertainty rather than a
  silent merge.

## Eval Criteria

- Exact matches preserve canonical identifiers.
- Possible matches remain marked as possible.
- Ambiguous records produce missing evidence or clarification needs.
- No test case may show a skill-driven write, merge, or approval change.
