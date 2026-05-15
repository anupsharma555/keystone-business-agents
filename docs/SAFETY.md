# Keystone Safety Policy

Keystone agents are dry-run first and draft-only by default. They may classify messages, research companies, identify opportunities, and draft outbound copy, but they must not send, publish, schedule, or trigger live external side effects automatically.

## Outbound Copy

- No auto-send: external email and other outbound messages must never be sent automatically.
- Draft-only default: generated outbound copy is a draft until a human approves it.
- Approval required: outbound copy must have explicit human approval before sending, publishing, scheduling, or passing to a live integration.
- No external email may be sent automatically under any default setting.
- Follow-up schedule records are recommendations only. They do not create Gmail scheduled sends,
  cron jobs, background jobs, or CRM tasks.

## Data Boundaries

- Do not process PHI or patient-specific information.
- Legal, financial, security, and contractual content must be flagged for human review.
- Company and opportunity research must include source attribution specific enough for review.
- Do not fabricate company facts, opportunity signals, contacts, relationships, customers, outcomes, or compliance claims.

## Secrets And Logs

- API keys, tokens, credentials, webhook secrets, and service account material must be stored only in environment variables.
- Logs must not expose secrets, raw credentials, authorization headers, tokens, or sensitive fixture values.
- Exceptions and debug traces should redact secret-like values before display or persistence.

## Development Model

- Build and test in dry-run fixture mode first.
- Live integrations require an explicit caller-provided flag for the specific integration.
- Live integrations also require human approval when outbound communication is involved.
- Current live paths are intentionally narrow: Gmail read, Gmail labels, Gmail
  draft creation, SearchProvider-backed research search, live-gated website
  extraction for selected pages, and Slack approval notifications.
- Slack `@KNI` business-agent mode is a request/review bridge only. Slack
  context, background thread results, approval-card posting, message actions,
  Slack history access, live model execution, live search, and Gmail draft
  creation are separate flags, and none enables external sending.
- Gmail live mode is draft-only. External email sending is intentionally not implemented.
- Gmail label management is preview-first for cleanup: managed labels use one primary triage label plus overlays, and obsolete label removal requires an explicit cleanup flag.
- Data-only follow-up recommendations may be saved locally for review, but scheduling remains a
  manual human decision outside Keystone v1.
