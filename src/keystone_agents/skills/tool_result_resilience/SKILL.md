---
skill_id: tool_result_resilience
skill_version: 2026-05-31.2
skill_purpose: Handle failed, empty, malformed, stale, or partial tool results without unsafe inference.
applies_to:
  - gmail_triage
  - business_research_analyst
  - rag_retrieval_specialist
  - opportunity_scout
  - outreach_composer
  - airtable_context_agent
  - google_workspace_context_agent
  - zotero_context_agent
  - rss_context_agent
  - preprints_context_agent
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/safety_refusals.jsonl
  - evals/provider/search_coverage_cases.jsonl
validation_paths:
  - tests/test_search_provider.py
  - tests/test_live_retrieval_integration.py
  - tests/test_agent_registry.py
safety_notes:
  - Missing tool data is not negative evidence and never authorizes unsafe fallback actions.
---

# Tool Result Resilience

## Purpose

Guide agents when tools return empty results, malformed outputs, partial reads,
timeouts, rate limits, stale data, credential failures, or provider errors.

## Applicable Agents

All live-tool Keystone agents use this skill.

## Typical Inputs

- Tool outputs, error objects, diagnostics, retrieval metadata, source bundles,
  credential/liveness flags, and partial structured records.

## Required Behavior

- Preserve tool limitations in output fields or audit notes.
- Treat missing data as unknown, not false.
- Choose safe fallbacks such as fixture mode, lower confidence, clarification,
  narrower retrieval, or a validation plan.

## Flexible Behavior

- May continue with partial evidence when the answer marks limitations clearly.
- May recommend retry, alternate provider, or manual review when appropriate.

## Boundaries

- Must not fabricate tool results.
- Must not hide live-provider failures.
- Must not bypass approval, source, or write gates because a tool failed.

## Reasoning Questions

- Did the tool fail, return nothing, return partial data, return stale data, or
  return malformed data?
- Is the missing data needed for safety, source support, identity, permission,
  scoring, or only completeness?
- What fallback is allowed by current live flags and tool policy?
- How should the limitation change confidence, readiness, or next action?

## Decision Rubric

- Hard blocker: missing credentials, unavailable required system, unsafe write
  boundary, or required source/approval gate cannot be checked.
- Soft limitation: partial evidence can support an internal answer with caveats.
- Retry candidate: transient timeout, rate limit, malformed page, or alternate
  provider likely improves evidence.
- Not negative evidence: empty results unless the tool explicitly proves absence.

## Tie-Breakers

- Prefer safe partial answers over invented completeness.
- Prefer deterministic diagnostics over model guesses about provider behavior.
- If live provider selection is a Python policy, report the need rather than
  selecting a hidden provider in prose.

## Output Contract

Return limitations, provider diagnostics, confidence effects, missing evidence,
and next safe action.

## Failure Modes

- Rate limits, timeouts, no results, stale cache, malformed JSON, missing
  credentials, and unreachable services should be visible and bounded.

## Eval Criteria

- Empty results are not treated as proof of absence.
- Tool failures appear in limitations or diagnostics.
- Fallbacks remain dry-run-safe unless live gates permit otherwise.
