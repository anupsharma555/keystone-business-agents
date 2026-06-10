---
skill_id: writing_style_adaptation
skill_version: 2026-05-31.1
skill_purpose: Adapt draft-only user-facing copy to the operator's approved writing style without adding unsupported facts.
applies_to:
  - outreach_composer
  - gmail_triage
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/outreach_copy_constraints.jsonl
  - evals/static/outreach_composer_cases.json
validation_paths:
  - tests/test_outreach_composer.py
  - tests/test_sdk_execution.py
safety_notes:
  - Writing style is tone guidance only; it is never evidence, approval, or permission to send.
---

# Writing Style Adaptation

## Purpose

Guide agents that draft email, LinkedIn, Slack-thread, or Gmail reply text so
the copy sounds like the operator while preserving source, approval, and
draft-only boundaries.

## Applicable Agents

Outreach Composer and Gmail Triage.

## Typical Inputs

- Approved writing style policy, approved email style profiles, approved
  examples, channel, recipient context, source-backed facts, blocked claims,
  and operator revision notes.

## Required Behavior

- Treat style as expression, pacing, tone, and structure only.
- Preserve approved factual claims exactly enough that source attribution still
  holds.
- Use a concise, professional, natural, low-pressure tone.
- Prefer short paragraphs, clear asks, restrained warmth, and no hype.
- For Anup's default voice, prefer short, passive or low-friction wording:
  "would be useful", "could", "if helpful", "if useful", "would welcome", and
  similar phrasing over assertive sales language.
- Keep replies succinct but not under-informative: answer the ask, include only
  necessary context, and avoid over-explaining.
- For Slack-thread-only email replies, include one concrete thread-grounded detail
  when available so the draft does not read as a generic placeholder.
  If the source is marketing-heavy, translate only the safe topic into neutral
  wording rather than copying promotional phrases.
- Use `Sincerely,\nAnup` as the default email signoff when no approved profile
  provides a more specific signoff.
- Keep draft-only and no-send language intact when the output is routed through
  an approval workflow.
- Flag when a requested style change would imply unsupported familiarity,
  outcomes, relationship depth, urgency, or authority.

## Flexible Behavior

- May choose a warmer, more direct, or more concise variant when the channel and
  relationship context support it.
- May use placeholders or questions when the recipient, ask, or approved context
  is incomplete.
- May preserve the operator's exact phrase when it is stylistic and does not
  add a factual claim.

## Boundaries

- Must not copy facts, names, outcomes, or relationship claims from examples
  unless they are independently approved for this recipient.
- Must not use style examples as source evidence.
- Must not turn pending, private, rejected, internal-only, or unsupported
  context into outbound copy.
- Must not send, schedule, publish, create live Gmail drafts, or weaken approval
  gates.
- For inbound email replies, apply style to Slack-thread-only draft text by
  default. Do not convert styled reply text into a provider-side Gmail draft
  unless a separate backend setting and approval gate allow that action.

## Reasoning Questions

- Which parts of the requested draft are factual claims, and which parts are
  style choices?
- Does the tone match the relationship stage, channel, and confidence level?
- Would any wording imply familiarity, validation, urgency, or outcomes not
  present in approved context?
- Is the CTA appropriately light for the available evidence?

## Decision Rubric

- Ready: facts are approved, tone is natural, CTA is low-pressure, and no-send
  is preserved.
- Needs softening: wording is too sales-like, too familiar, too broad, or too
  confident for the evidence.
- Needs placeholders: target, ask, or approved context is missing but a
  thread-local draft can still be useful.
- Blocked: the only available copy would depend on unsupported, private,
  sensitive, or unapproved context.

## Tie-Breakers

- Approved source-backed facts beat style preferences.
- A shorter draft beats a warmer draft when both would satisfy the request, but
  a reply that omits all available thread-specific context is too sparse unless
  no safe context exists.
- Low-pressure phrasing beats assertive phrasing unless the operator explicitly
  asks for a more direct approved variant.
- Placeholder questions beat invented recipient details.

## Output Contract

Return draft text plus style rationale, unsupported or softened wording, source
IDs used for factual claims, approval state, and next safe step.

## Failure Modes

- Over-polishing into generic sales copy.
- Copying example facts as if they apply to the current recipient.
- Hiding unsupported claims behind softer language.
- Blocking a thread-local placeholder draft when the user explicitly asked for
  draft-only internal text and no external action.

## Eval Criteria

- Drafts preserve source-backed claims and approval state.
- Style changes improve clarity and naturalness without adding facts.
- Unsupported personalization is removed, softened, or placed in questions.
- Output remains draft-only unless a separate deterministic approval gate
  permits a live action.
