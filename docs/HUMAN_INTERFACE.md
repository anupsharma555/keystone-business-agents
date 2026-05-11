# Human Interface

## Current Operating Model

The implemented human interface is local-first:

- CLI is the developer and admin interface for all current runs.
- SQLite is the system of record and audit layer.
- Gmail is an optional live source and draft surface for labeled email only.
- Sent-email style profiling is optional and stores aggregate redacted profile data only.
- Slack is an optional approval notification surface, not a command center yet.
- CRM access is local fixture/table-mirror context only; no live CRM provider is enabled.
- Local dashboard exports are markdown or JSON generated from SQLite.

No implemented path sends email, publishes messages, schedules outreach, or approves work
autonomously.

## Operator Dashboard Decision

Near-term operation does not need a web app or API. The current local dashboard and table mirror
exports are sufficient because SQLite remains canonical and the review commands are read-only by
default. Operators can inspect approval queue items, outreach drafts, outreach tracking,
opportunities, company profiles, feedback, and recent agent runs without enabling live provider
writes.

Use the decision report when checking whether a proposed dashboard change fits the current phase:

```bash
.venv/bin/python scripts/export_pipeline_table.py --dashboard-decision
```

Sufficient now:

- SQLite is the source of truth for saved records, approval state, audit rows, feedback, drafts,
  contacts, companies, and outreach tracking.
- `scripts/export_pipeline_table.py --dashboard` gives a local markdown dashboard for human
  review.
- Table mirror exports provide copy/paste-safe markdown or JSON without calling live table
  providers.
- Full inbound email bodies, full approval draft text, and body-like sensitive fields are
  omitted, hashed, summarized, or redacted.
- The dashboard/export path does not approve work, send email, create Gmail drafts, schedule
  follow-ups, update CRM, or write to Slack, Airtable, Google Sheets, Gmail, or any live provider.

A later lightweight dashboard or API should be local and read-only first. It should expose
SQLite-backed views for summary, approvals, drafts, opportunities, companies, outreach tracking,
and agent runs; bind to localhost by default; reuse the current redaction rules; and keep all
write-like actions in separate explicit commands with approval gates and audit logging.

## Recommended Future Model

The intended operator model is:

- Slack as command center and approval cockpit.
- Gmail as the source and draft surface for email.
- Optional aggregate sent-email style profiles for draft guidance after human approval.
- SQLite as the durable audit record.
- CLI as the developer/admin interface.
- Airtable or Google Sheets as optional visual dashboards that mirror SQLite later.
- HubSpot, Airtable, or Google Sheets as optional CRM providers only after a reviewed
  live-integration design.

Airtable, Google Sheets, HubSpot, Slack slash commands, live CRM writes, and scheduled runs are
planned interfaces only. They are not implemented live write paths.

Future Airtable or Google Sheets support should be added for human readability only at first.
Use them as redacted dashboards that mirror SQLite rows, not as the canonical database. The
recommended link model is:

```text
SQLite canonical record -> Airtable review row -> Google Sheets report row
```

Each mirrored row should carry stable references such as `sqlite_record_id`, `object_type`,
`object_id`, `source_agent`, `approval_status`, `airtable_record_url`,
`google_sheet_row_url`, `last_mirrored_at`, and `mirror_checksum`. External edits must be
read-only until an explicit import command validates the change and writes it back to SQLite.
Approvals in Airtable or Google Sheets must not unlock Gmail drafts or outreach by themselves.

## Implemented Interfaces

CLI:

```bash
.venv/bin/python scripts/run_gmail_triage.py --fixture tests/fixtures/sample_email_consulting.txt --markdown
.venv/bin/python scripts/run_company_research.py --company Curebase --fixture tests/fixtures/sample_company_curebase.json --markdown
.venv/bin/python scripts/run_opportunity_scout.py --topic "behavioral health AI" --markdown
.venv/bin/python scripts/run_outreach_draft.py --fixture sample_company_curebase --opportunity-fixture sample_lead_curebase --markdown
.venv/bin/python scripts/list_approvals.py
.venv/bin/python scripts/export_pipeline_table.py --dashboard
```

Live opt-in:

- Gmail: `--live-gmail --no-dry-run`, with `--label-filter` unless `--allow-inbox` is explicit.
- Gmail label mutation: also requires `--apply-labels`; use `--preview-labels` first.
- Gmail draft creation: also requires `--create-draft` plus a local
  `approved_for_send/send` approval record for the target `gmail_draft`.
- Slack approval notification: `--request-approval --live-slack --no-dry-run`.
- Live search: `--live-search --no-dry-run` plus provider configuration.
- Live CRM: not implemented. CRM write-like operations remain dry-run local table-mirror
  previews and require approval metadata before use.

## Agent Interfaces

### 1. Opportunity Scout

Implemented:

- Triggered by CLI in fixture mode by default.
- Optional live search through `SearchProvider` for SearXNG, Serper, or Firecrawl
  when explicitly enabled.
- Broad live searches use multi-lane retrieval and can run bounded adaptive
  follow-up queries plus result-page deepening when accepted records under-fill.
- Produces ranked `OpportunityRecord` objects with source links, score breakdowns, handoff
  recommendations, and approval gates.
- Saves opportunities and audit rows to SQLite only when `--save` is passed.
- Does not draft outreach.

Planned:

- Manual Slack command or scheduled run.
- Posting ranked opportunities to `#opportunities`.
- Slack actions: `Research`, `Save`, `Reject`, `Draft Outreach`.

### 2. Business Research Analyst

Implemented:

- Triggered by CLI, fixture/company inputs, broader research targets, or
  allowlisted local/Zotero collection context.
- Optional live search through `SearchProvider` when explicitly enabled.
- Produces source-attributed `ResearchBrief` records for broader research and
  legacy `CompanyProfile` records with fit scores, confidence, risks, missing
  information, and claim evidence for company workflows.
- Saves company profiles and source records to SQLite only when `--save` is passed.

Planned:

- Trigger from a Slack opportunity action.
- Post markdown research briefs to a Slack thread.
- Richer CRM/contact context beyond local fixture inputs.

### 3. Gmail Inbound Triage Agent

Implemented:

- Triggered by CLI fixture input or live Gmail label scope.
- Live Gmail can list recent messages, retrieve one message, preview or apply labels, and create
  a reply draft only when each action is explicitly enabled.
- Gmail attachments are screened by metadata only. Attachment bodies are not downloaded, read,
  OCR'd, rendered, visualized, summarized, or sent to an LLM.
- Requires `--create-draft` before creating Gmail drafts.
- Requires a local `approved_for_send/send` approval before creating live Gmail drafts.
- `send_email` is intentionally not implemented and raises `NotImplementedError`.

Planned:

- Trigger by a Slack command or scheduled narrow-label poller.
- Post draft approval requests into a Slack approval queue by default.
- Optional attachment review or visualization as a separate approved workflow with narrow
  message scope, MIME allowlists, sandboxed extraction/rendering, redacted audit logs, and
  source records for any attachment-derived claims.

### 4. Outreach Composer

Implemented:

- Triggered by CLI from approved fixture/company/opportunity/contact context.
- Produces an `OutreachDraft` with email copy, LinkedIn note, source-backed facts,
  personalization rationale, unsupported-claim flags, and pending approval status.
- Can include data-only follow-up schedule recommendations for human review.
- Can create a Slack approval notification in dry-run or explicit live Slack mode.
- Never sends email.
  Follow-up recommendations do not schedule Gmail, create CRM tasks, or start background jobs.

Planned:

- Trigger only from an approved opportunity or company profile in Slack.
- Optionally create a Gmail draft after approval. Sending stays manual.

### 5. Email Style Profiler

Implemented:

- Builds aggregate `EmailStyleProfile` records from local fixtures or explicit live Gmail `SENT`
  sampling.
- Stores source IDs, hashes, lengths, redacted sample summaries, and aggregate style fields.
- Does not store raw sent-email bodies, apply Gmail labels, create drafts, or send email.
- Generated profiles default to pending and are ignored until `approved_for_drafting`.

Planned:

- Human-review UI for comparing pending style profiles and approving one active profile.

### 6. Local CRM Provider

Implemented:

- Exposes a local-only `CRMProvider` seam for listing leads, fetching account context,
  previewing status changes, and previewing report-link or note attachments.
- Uses local contact/CRM context and table-mirror rows only.
- Write-like operations are dry-run by default, approval-aware, and produce audit notes instead
  of syncing to any external CRM.
- Does not call HubSpot, Airtable, Google Sheets, or any network CRM API.

Planned:

- Future provider implementations may support HubSpot, Airtable, or Google Sheets after explicit
  live flags, narrow object scopes, approval checks, and audit logging are designed.

## Proposed Slack Channels

- `#opportunities`: opportunity findings and scout handoffs.
- `#account-research`: company research briefs and source review threads.
- `#draft-approvals`: Gmail and outreach draft approvals.
- `#agent-ops`: failures, skipped side effects, live-mode warnings, and operational alerts.
- `#agent-audit`: optional read-only feed of important persisted state transitions.

## Proposed Slack Commands

These commands are not implemented yet:

- `/keystone scout [topic]`
- `/keystone research [company] [url]`
- `/keystone triage [message_id]`
- `/keystone draft [company_or_opportunity_id]`
- `/keystone approve [object_id]`
- `/keystone reject [object_id] [reason]`
- `/keystone status [object_id]`

## Approval Lifecycle

Implemented approval queue states:

- `pending`
- `approved`
- `rejected`
- `revise`
- `archived`
- `expired`

Approval records are stored in SQLite. Slack may notify humans about a queue item, but SQLite
remains the source of truth.

Approval is not a send action. An approved queue item records local reviewer intent only. It
does not send email, publish copy, schedule follow-up, or enable an external send integration.

Local admin commands:

```bash
.venv/bin/python scripts/list_approvals.py
.venv/bin/python scripts/list_approvals.py --status all --json
.venv/bin/python scripts/update_approval.py approval_123 approved --notes "Reviewed"
.venv/bin/python scripts/update_approval.py approval_123 revise --notes "Shorten and cite sources"
```

## Object Status Model

Opportunity:

- `candidate`
- `saved`
- `research_requested`
- `researched`
- `approved_for_drafting`
- `drafted`
- `rejected`
- `archived`

Company profile:

- `draft`
- `source_review_needed`
- `reviewed`
- `approved_for_opportunity_scoring`
- `rejected`
- `archived`

Gmail triage:

- `triaged`
- `labeled`
- `draft_pending_approval`
- `draft_created`
- `manual_review`
- `closed`

Outreach draft:

- `pending_approval`
- `approved`
- `needs_revision`
- `rejected`
- `gmail_draft_created`
- `closed`

Outreach tracking:

- `not_started`
- `draft_pending_approval`
- `draft_approved`
- `draft_created`
- `sent_manually`
- `reply_received`
- `closed_no_reply`
- `bounced`
- `paused`

## Failure And Error Reporting

Current implementation:

- CLI commands fail with clear messages for missing live flags, dry-run conflicts, or missing
  credentials.
- Failed saved runs and tool events can be persisted to SQLite.
- Health checks keep missing optional live credentials non-failing in dry-run mode and warn
  when a live intent flag is configured without required credentials.

Future Slack reporting should post failures to `#agent-ops` with agent name, object id,
operation, dry-run/live mode, redacted error, and next safe action.

Never post or persist secrets, OAuth tokens, API keys, PHI, full email bodies, or private
customer notes.

## Audit Logging Rules

Persist when saving:

- command or trigger source
- agent name
- input summary and stable input hash
- output object id
- source links
- approval status and reviewer
- live versus dry-run mode
- skipped side effects and reasons
- redacted tool event summaries
- errors with redacted context

Never persist:

- API keys
- OAuth access or refresh tokens
- PHI or patient-specific content
- unsupported factual claims as accepted facts
- full private email bodies unless a future reviewed design explicitly allows it

## What Must Stay Manual

- Sending any email.
- Approving outbound copy.
- Accepting unsupported claims.
- Handling legal, finance, security, PHI, or contractual content.
- Deciding whether sensitive or private context can be used in outreach.
- Enabling new live integrations.
- Syncing CRM status, notes, or report links to any external CRM.
- Changing provider credentials or OAuth scopes.

## What Can Later Become Automated

- Scheduled Opportunity Scout runs with narrow topics and rate limits.
- Slack reminders for pending approvals.
- Handoff from high-scoring opportunities to the Business Research Analyst.
- Saving rejected or low-priority opportunities with reasons.
- SQLite dashboard mirroring into Airtable or Google Sheets for human-readable review.
- Explicit import of reviewed Airtable or Google Sheets status changes back into SQLite after
  validation against the approval state machine.
- Approved CRM status or report-link syncs after a reviewed live CRM integration exists.
- Gmail draft creation after explicit approval.
- Gmail attachment reading, OCR, rendering, or visualization after a separate reviewed
  attachment-safety design.
- LangGraph-based durable orchestration.

Any future automation must preserve dry-run defaults, explicit live flags, approval gates,
source attribution, audit logs, and no-send behavior.
