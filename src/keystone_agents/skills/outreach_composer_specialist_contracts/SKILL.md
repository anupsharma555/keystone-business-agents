---
skill_id: outreach_composer_specialist_contracts
skill_version: 2026-05-31.2
skill_purpose: Specialist reasoning contracts for approved-context synthesis, safe personalization, channel drafting, and review packets.
applies_to:
  - outreach_composer
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/static/outreach_composer_cases.json
  - evals/local/outreach_copy_constraints.jsonl
validation_paths:
  - tests/test_outreach_composer.py
  - tests/test_sdk_execution.py
safety_notes:
  - Outreach skills are draft-only and require approved source-backed context.
---

# Outreach Composer Specialist Contracts

## Purpose

Guide draft-only outreach generation from approved context while preserving
source support, style flexibility, and approval boundaries.

## Applicable Agents

Outreach Composer.

## Typical Inputs

- Approved company research, opportunity records, CRM/contact context, email
  style profiles, templates, outreach examples, source IDs, revision notes, and
  Orchestrator memo.

## Required Behavior

- `approved_context_synthesis`: use only context approved for the current
  drafting purpose.
- `personalization_claim_screening`: check each personalized sentence for
  support and soften or remove weak claims.
- `channel_specific_draft_composition`: draft email, LinkedIn, call prep,
  follow-up, or intro copy within channel constraints.
- `channel_specific_draft_composition`: for inbound Gmail reply handoffs,
  default to Slack-thread-only draft text; provider-side Gmail drafts require a
  separate backend setting and approval gate.
- `channel_specific_draft_composition`: for Slack-thread-only replies, keep the
  operator voice concise but not empty. When selected thread context provides
  safe details, include one concrete source-backed detail about the email's
  topic. Do not echo marketing copy, broad platform claims, or unsupported
  personalization.
- `style_and_tone_adaptation`: adapt approved style without adding facts.
- `template_application`: use templates as structure, not evidence.
- `cta_and_next_step_design`: match ask strength to confidence and relationship
  stage.
- `call_prep_and_talk_track_generation`: distinguish verified facts from
  hypotheses.
- `follow_up_plan_design`: recommend timing/content without scheduling or
  sending.
- `draft_review_packet_preparation`: include draft, source notes, unsupported
  claims, approval status, rationale, and suggested edits.

## Flexible Behavior

- May deviate from templates when context supports a clearer structure.
- May produce a sparse, low-pressure draft or refuse to draft when context is too
  thin.

## Boundaries

- Must not send, publish, schedule, or mark follow-up complete.
- Must not create Gmail drafts by default; Slack-thread-only reply drafts are
  the safe default for inbound email reply handoffs.
- Must not use pending, rejected, private, stale, or unsupported context in
  outbound copy.

## Reasoning Questions

- What approved facts can be safely used in external copy, and what must remain
  internal or unsupported?
- What is the relationship stage, channel, recipient role, and appropriate ask
  strength?
- Which personalization claims are source-backed, merely stylistic, or risky?
- Does the draft need email, LinkedIn, call prep, follow-up plan, or a review
  packet rather than a message?

## Decision Rubric

- Draft-ready: approved context, source-backed personalization, known channel,
  clear low-risk CTA, and no-send preserved.
- Thread-local reply-ready: selected Gmail thread summary, no unsupported
  claims, Slack-thread-only output, one safe concrete topic detail when
  available, and no Gmail draft creation.
- Sparse draft: approved facts exist but personalization is thin; use neutral,
  low-pressure copy.
- Review-only: context is useful internally but not approved or sufficient for
  external copy.
- Blocked: no approved context, unsafe claims, sensitive content, or action
  request beyond draft-only boundaries.

## Tie-Breakers

- Approved source-backed facts beat style examples.
- Remove unsupported personalization rather than hiding it in softer language
  when it would still imply a fact.
- Match CTA strength to evidence confidence and relationship warmth.
- Follow-up plans are recommendations until a separate approval and scheduler
  gate exists.
- Gmail draft creation is not implied by reply drafting; it requires a separate
  deterministic setting and approval gate.

## Skill Gate Contract

- Reasoning question: which facts are approved for this recipient and channel,
  and which would become risky if copied externally?
- Hard gate: outbound copy must remain draft-only, no-send, approval-gated, and
  source/context backed; Python enforces `outreach_approval_claim_gate`.
- Fallback behavior: if approval or source basis is missing, return review-only
  notes, blocked claims, and the next approval/source step instead of a draft.
- Output field: `approval_required`, `approval_status`, `source_ids_used`,
  `unsupported_claims_flagged`, `send_enabled`.
- Eval labels: `outreach.approval_claim_gate`, `context_permission_gating`,
  `action_boundary_enforcement`, `evidence_attribution_and_claim_mapping`.

## Output Contract

Return email subject/body, LinkedIn note, rationale, source IDs, facts used,
unsupported claims, approval state, no-send fields, review packet, and next safe
action.

## Failure Modes

- Missing approved context, unsupported personalization, stale contact data,
  style-only evidence, and ambiguous channel should block or soften copy.

## Eval Criteria

- Drafts are plain text and draft-only; inbound Gmail reply drafts are
  Slack-thread-only unless Gmail draft creation is explicitly enabled and
  approved.
- Unsupported claims are flagged or removed.
- Source IDs match approved context.
- CTA remains clear and low pressure.
