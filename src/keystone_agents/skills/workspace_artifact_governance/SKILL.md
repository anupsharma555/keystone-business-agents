---
skill_id: workspace_artifact_governance
skill_version: 2026-05-31.2
skill_purpose: Govern internal Docs, Sheets, Drive-style, and local artifact outputs without bypassing business gates.
applies_to:
  - gmail_triage
  - business_research_analyst
  - opportunity_scout
  - outreach_composer
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/safety_refusals.jsonl
validation_paths:
  - tests/test_agent_registry.py
  - tests/test_chief_of_staff.py
  - tests/test_architecture.py
safety_notes:
  - Internal artifacts do not approve outreach, Gmail drafts, CRM writes, Slack posts, or sends.
---

# Workspace Artifact Governance

## Purpose

Guide creation or update of internal Google Docs, Sheets, Drive-style files, or
local artifacts.

## Applicable Agents

Business Research Analyst, Opportunity Scout, Gmail Triage, Outreach Composer,
and Chief of Staff use this skill when internal artifacts are requested.

## Typical Inputs

- User artifact request, WorkItem state, source scope, approval reference,
  Google Workspace tool metadata, local artifact plans, rows, tables, and report
  content.

## Required Behavior

- Keep artifact scope internal unless explicitly approved otherwise.
- Include source scope, timestamp or freshness, authoring agent, related
  WorkItem/artifact IDs, and approval status when available.
- Preserve the distinction between writing an internal artifact and taking an
  external action.

## Flexible Behavior

- May recommend Docs for narrative material and Sheets for structured rows.
- May return an artifact plan when writes are blocked or not requested.

## Boundaries

- Must not treat Workspace content as permission to send, publish, schedule,
  approve, or update CRM.
- Must not write outside allowed paths or without required live flags and
  approval references.

## Reasoning Questions

- Is the operator asking for an internal artifact, an external share, or both?
- What source scope, freshness, approval state, and WorkItem IDs should travel
  with the artifact?
- Is the destination allowed, known, and scoped?
- Would the artifact expose private context or imply an external action?

## Decision Rubric

- Safe artifact: internal, scoped, source-labeled, and no external publication.
- Planned-only: destination, write flag, approval reference, or source basis is
  missing.
- Blocked: request requires external share/post/send, unsafe path, secrets, PHI,
  or unapproved private context.
- Needs clarification: artifact format or destination affects safety or utility.

## Tie-Breakers

- Internal artifact creation does not change approval, outreach, or lifecycle
  state unless a separate gate records that state.
- Prefer a local/internal draft plan when live Workspace tools are unavailable.
- Include enough provenance for later review without copying unnecessary private
  bodies.

## Output Contract

Return artifact type, destination, source scope, approval reference, write state,
blocked reasons, and next safe action.

## Failure Modes

- Missing approval reference, unavailable Workspace tools, unclear destination,
  and unsafe external-use requests should become blocked or planned-only output.

## Eval Criteria

- Artifact writes remain internal and scoped.
- Missing approval blocks live writes.
- Artifact content does not imply external side effects.
