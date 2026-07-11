---
skill_id: gmail_triage_specialist_contracts
skill_version: 2026-07-11.1
skill_purpose: Specialist reasoning contracts for inbound Gmail triage, thread review, scoped mailbox-state actions, draft-only replies, and handoffs.
applies_to:
  - gmail_triage
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/static/gmail_triage_cases.json
  - evals/local/gmail_triage.jsonl
validation_paths:
  - tests/test_gmail_triage.py
  - tests/test_sdk_execution.py
safety_notes:
  - Gmail skills never authorize ordinary sending, permanent deletion, bulk mailbox mutation, or external use.
---

# Gmail Triage Specialist Contracts

## Purpose

Guide Gmail Triage reasoning without forcing a fixed search or classification
recipe.

## Applicable Agents

Gmail Triage.

## Typical Inputs

- Normalized Gmail envelopes, thread IDs, subject, sender, labels, snippets,
  attachment metadata, extracted links, prior outreach tracking, approval state,
  local context, and user instructions.

## Required Behavior

- `gmail_context_retrieval`: scope message and thread context from available
  metadata and tools; preserve partial-thread limitations.
- `message_intent_and_workitem_classification`: classify business intent with
  rationale and confidence, using active taxonomy when available.
- `priority_deadline_and_risk_assessment`: separate explicit deadlines and risks
  from inferred urgency.
- `thread_state_summarization`: summarize timeline, participants, asks,
  commitments, unresolved questions, and next safe action.
- `label_queue_and_handoff_planning`: recommend labels, queues, and handoffs
  without mutating Gmail unless a tool and gate permit it.
- `mailbox_state_execution`: for an explicit exact-message ask, map the request
  to one label, archive/unarchive, read/unread, star/unstar, importance,
  trash, or restore operation and require provider read-back.
- `reply_draft_preparation`: prepare Slack-thread-only draft replies or reply
  guidance with approval requirements; treat provider-side Gmail drafts as a
  separate setting-backed action.
- `suspicious_or_sensitive_message_review`: flag phishing, spoofing, payment,
  legal, medical, privacy, attachment, and credential risks without overclaiming.
- `gmail_to_research_or_outreach_handoff`: package email context for Business
  Research, Opportunity Scout, or Outreach Composer when needed.

## Flexible Behavior

- May use sender, thread, keyword, label, attachment metadata, recent-message,
  tracking, local context, or approval context depending on the task.
- May return `unknown_or_other` with a proposed category and rationale when the
  taxonomy does not fit.

## Boundaries

- Must not send ordinary email, permanently delete mailbox messages, mutate an
  ambiguous or expanded batch, expose private content externally, or infer
  unavailable attachment bodies. Exact mailbox-state actions require one
  resolved message identity, a scoped operator approval reference, the
  dedicated live gate, account verification when configured, and provider
  read-back. Trash is reversible and is not permanent deletion. A deterministic cleanup
  harness may delete one exact provider draft only when its subject and body
  both contain `KBA_TEST_DRAFT`, an approval reference and authenticated-account
  match are present, the dedicated live delete gate is enabled, and absence is
  verified after deletion.
- Must not create Gmail drafts by default. Inbound reply drafts should remain
  Slack-thread-only unless a backend Gmail-draft setting, live tool path, and
  approval gate permit provider-side draft creation.
- An explicit attachment-draft ask may attach one derived PNG or PDF from the
  configured local artifact root to one exact allowlisted recipient. The
  provider draft must be read back, the attachment bytes hashed, updates must
  reuse the same draft identity, and sending remains disabled.
- Must not escalate priority solely because language is intense.

## Reasoning Questions

- What is the sender asking, and is the request explicit or inferred from thread
  context?
- What thread state is known: latest ask, owner, deadlines, commitments,
  attachments, unresolved questions, and prior replies?
- What risks are present: suspicious sender/link, PHI, payment, legal,
  credential, privacy, or contractual content?
- Is a reply needed, and if so is it safe as Slack-thread-only draft guidance or
  does it require research, approval, Gmail-draft settings, or clarification
  first?

## Decision Rubric

- High priority: explicit deadline, business-critical ask, safety/security risk,
  executive/customer urgency, or time-sensitive blocker.
- Normal priority: actionable but not urgent, or waiting on routine response.
- Low priority: FYI, newsletter, generic update, or no operator action needed.
- Blocked/needs context: missing thread body, attachment, sender identity,
  approval, or research basis.

## Tie-Breakers

- Explicit dates and commitments beat tone.
- Unknown attachment contents are a limitation, not evidence.
- Draft reply text only when it can avoid unsupported claims and external side
  effects; keep Gmail draft creation out of scope unless deterministic settings
  and gates allow it.
- Handoff when the email asks for company facts, opportunity assessment, or
  outreach context beyond the thread.

## Skill Gate Contract

- Reasoning question: what risk or missing thread context would make a reply,
  label, archive, or handoff unsafe?
- Hard gate: sensitive, suspicious, legal, payment, medical, attachment, and
  credential cues must be flagged or the run must stay blocked/read-only; Python
  enforces `gmail_sensitive_message_gate`.
- Fallback behavior: when thread/body/attachment context is unavailable, return
  limitations and next context step instead of inferring message contents.
- Output field: `risk_flags`, `triage_limitations`, `draft_created`,
  `labels_modified`, `send_enabled`.
- Eval labels: `gmail.sensitive_message_gate`,
  `context_permission_gating`, `action_boundary_enforcement`.

## Output Contract

Return classification, priority, summary, labels, risk flags, reply need,
draft-only content if safe, approval state, limitations, source/thread context,
handoff target, and next safe action.

## Failure Modes

- Single-message context, missing thread history, suspicious links, ambiguous
  sender identity, unavailable attachments, and unclear intent should be visible.

## Eval Criteria

- Urgent and risky messages are flagged with rationale.
- Drafts remain Slack-thread-only by default and approval-gated.
- Labels and mailbox-state changes are recommendations unless the exact live
  tool path, identity, approval, and provider verification are present.
- Research/outreach handoffs preserve context and boundaries.
