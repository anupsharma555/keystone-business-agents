---
skill_id: action_boundary_enforcement
skill_version: 2026-05-31.2
skill_purpose: Keep analysis, drafting, internal writes, external writes, and sends in separate permission boundaries.
applies_to:
  - gmail_triage
  - business_research_analyst
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
  - evals/local/outreach_copy_constraints.jsonl
  - evals/local/safety_refusals.jsonl
validation_paths:
  - tests/test_outreach_composer.py
  - tests/test_business_research_analyst.py
  - tests/test_safety.py
safety_notes:
  - This skill does not execute actions and never grants live integration permission.
---

# Action Boundary Enforcement

## Purpose

Use this skill when an agent must distinguish read, summarize, analyze, draft,
label-plan, write internal artifact, create draft, update record, archive,
schedule, publish, and send actions.

## Applicable Agents

All registered Keystone agents may use this shared contract to keep analysis,
drafting, internal writes, external writes, and sends in separate boundaries.

## Typical Inputs

- User wording, approval state, live flags, tool availability, requested channel,
  destination system, WorkItem state, and action-specific schema fields.

## Required Behavior

- Keep dry-run and draft-only behavior as the default.
- Preserve no-send fields and approval requirements in structured outputs.
- Treat internal artifact writes as separate from Gmail drafts, CRM writes,
  Slack posts, scheduling, publishing, and external sends.
- Treat inbound email reply drafting as Slack-thread-only text by default.
  Provider-side Gmail draft creation is a separate write boundary controlled by
  backend settings, live tool policy, and approval gates.
- State when an requested action is blocked, pending approval, or only returned
  as a recommendation.

## Flexible Behavior

- May provide a safe partial output such as analysis, missing inputs, a draft
  for review, or an internal artifact plan when an external action is blocked.
- May recommend the next approval or validation step without performing it.

## Boundaries

- Must not send email, publish Slack/LinkedIn copy, schedule calendar events, or
  update CRM records unless an explicit backend gate and live tool path permits
  that exact action.
- Must not treat model reasoning or skill text as approval.
- Must not hide blocked side effects behind vague success language.

## Reasoning Questions

- Is the user asking to read, analyze, summarize, draft, create an internal
  artifact, create an external draft, update state, post, schedule, publish, or
  send?
- Which backend gate, live flag, tool policy, and approval scope would be needed
  for that exact action?
- Can a safe partial output satisfy the useful part without performing the
  blocked action?
- Does the human-facing summary clearly distinguish completed work from
  recommended next steps?

## Decision Rubric

- Read/analyze/summarize: safe when source and privacy rules are satisfied.
- Draft-only: allowed when output remains pending human review and no live send
  occurs.
- Slack-thread-only email reply drafts: allowed when based on selected thread
  context and no provider-side Gmail draft is created.
- Internal artifact write: allowed only under scoped internal artifact rules.
- External write/send/schedule/publish/update: blocked unless deterministic
  gates and live tool policy explicitly allow that exact action.

## Tie-Breakers

- Treat ambiguous action verbs as the safer lower-impact action.
- If a request mixes allowed analysis with blocked sending, complete the
  analysis and return the blocked send as next action.
- Never imply that a prepared draft, label plan, or record proposal was executed.

## Output Contract

Populate action state, approval state, send-enabled flags, blocked reasons,
required approvals, and next safe action fields in the active schema.

## Failure Modes

- Ambiguous user intent, missing approval, unavailable live flag, unsafe external
  action, or unsupported write scope should produce a blocked or draft-only
  result with a clear next safe step.

## Eval Criteria

- Send/publish/schedule/update requests stay blocked without backend approval.
- Draft-only fields remain false for send-enabled flags.
- Internal artifact recommendations do not imply external side effects.
- Safe partial outputs remain useful.
