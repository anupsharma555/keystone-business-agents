---
skill_id: context_permission_gating
skill_version: 2026-05-31.2
skill_purpose: Determine whether available context is eligible for the current internal or external use case.
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
  - evals/local/outreach_copy_constraints.jsonl
  - evals/local/safety_refusals.jsonl
  - evals/static/outreach_composer_cases.json
validation_paths:
  - tests/test_outreach_composer.py
  - tests/test_business_research_analyst.py
safety_notes:
  - Context eligibility is separate from source quality and never bypasses approval gates.
---

# Context Permission Gating

## Purpose

Use this skill when an agent decides whether available company, contact, CRM,
Gmail, memory, Workspace, fixture, or user-provided context can be used for the
current purpose.

## Applicable Agents

All registered Keystone agents may use this shared contract where context
visibility affects analysis, routing, drafting, rendering, or artifact work.

## Typical Inputs

- Approval state, source records, CRM/contact context, Gmail excerpts, local
  memory, Workspace artifacts, user instructions, and tool-returned permission
  metadata.

## Required Behavior

- Distinguish known context from context usable internally, approved for
  outreach, pending approval, rejected, private, stale, unsupported, or blocked.
- Use contact, CRM, Gmail, and private example context only within the permission
  scope returned by tools, storage records, or explicit user instructions.
- Preserve blocked or pending context in audit, limitation, missing evidence, or
  unsupported-claim fields when the schema supports it.
- Treat approval state and source support as separate checks.

## Flexible Behavior

- May produce an internal analysis using context that is not approved for
  external copy when the output marks the context as internal-only.
- May ask for clarification or approval when the requested use is broader than
  the available permission scope.

## Boundaries

- Must not use pending, rejected, private, or unsupported context in outbound
  copy.
- Must not treat stored context, Workspace artifacts, or style profiles as
  permission to send, publish, schedule, or update external systems.
- Must not weaken no-PHI, no-send, or human-review rules.

## Reasoning Questions

- What is the requested use: internal analysis, source-backed summary, draft,
  external copy, record update, Slack post, send, schedule, or publish?
- What permission state does each context item carry?
- Is source support separate from approval for use?
- Would revealing this context expose private, internal, Gmail, CRM, PHI, legal,
  financial, security, or contractual content?

## Decision Rubric

- Known internally: may inform internal analysis when marked internal.
- Approved for draft: may be used in draft-only copy with approval state
  preserved.
- Approved for external use: may be used only within the explicit approval
  scope and still cannot trigger sends.
- Pending, rejected, private, unsupported, or unknown: preserve as blocked
  context or missing approval.

## Tie-Breakers

- More restrictive context state wins over source confidence.
- User wording can narrow permission, but cannot bypass backend approval gates.
- If permission and source support conflict, keep the fact internal or ask for
  approval instead of externalizing it.

## Output Contract

Record approved context used, blocked context, approval state, approval scope,
unsupported claims, missing approvals, and next safe action in the active schema.

## Failure Modes

- Missing approval, stale approval, conflicting permission metadata, private
  context requested for external use, and unknown source visibility should
  produce blocked context or clarification rather than silent use.

## Eval Criteria

- Pending or rejected context is excluded from outreach.
- Internal-only context remains marked internal.
- Approved context is used only within its approval scope.
- Permission gaps are visible in structured output.
