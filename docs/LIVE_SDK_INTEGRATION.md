# Live SDK Integration

This repo keeps live LLM execution behind one narrow bridge:

1. Retrieve and normalize source context in deterministic Python.
2. Convert that context to a typed, sanitized SDK input model.
3. Run the SDK agent with `run_retrieved_sdk_synthesis(...)`, using either a fake/local
   `run_config` for tests or `live=True` for credential-gated live model execution.
4. Validate the structured Pydantic output.
5. Persist only when `--save` or an equivalent explicit save flag is present.

The bridge does not send email, post Slack messages, publish LinkedIn content, schedule
anything, or call CRM systems. Existing live integration flags still own those side
effects: `--live-gmail`, `--live-search`, and `--live-slack` require `--no-dry-run` and
credentials before their tools run.

## CLI Boundaries

The default CLI path remains deterministic Python. It should be used for fixture runs,
local normalization, safety gates, source collection, scoring, and dry-run previews.

`Runner.run` belongs only after a workflow has typed sanitized input and an explicit
execution mode:

- Tests pass `build_local_run_config(...)` with a fake provider.
- Live model execution passes `live=True` and relies on centralized credential checks.
- Trace metadata must be scalar operational metadata only. Do not include full email
  bodies, prompts, draft bodies, secrets, PHI, patient-specific content, or approval
  artifacts.
- `trace_include_sensitive_data` must remain false for SDK synthesis.
- `--save` records sanitized run summaries and structured outputs. Raw source material
  stays in the deterministic retrieval layer unless an existing storage tool explicitly
  supports a redacted record.

`--sdk` in CLIs constructs or describes the SDK boundary only. `--run-sdk` is for
test harnesses that inject a fake/local SDK `run_config`. `--live-sdk` is the
explicit CLI flag for credential-gated live model synthesis. The natural-language
`keystone ask` path also auto-enables the same live SDK execution for explicit
named-agent calls in live-test/full-live environments; pass `--no-live-sdk` to
force the dry-run / WorkItem path.

## Workflow Sequence

### Gmail

Python retrieves Gmail fixture or live Gmail messages, normalizes them to
`GmailMessageEnvelope`, applies deterministic safety checks, and controls labels or draft
creation under existing Gmail flags. The LLM receives `GmailTriageSDKInput` and returns
`EmailTriageResult`. Drafts remain draft-only and approval-gated. Saving records a
sanitized run summary and triage output.

For GT-1 batch priority grouping, Python retrieves multiple sanitized envelopes first, then
the LLM receives `GmailPriorityGroupingSDKInput` and returns `GmailPriorityGroupingResult`.
This path is intentionally non-deterministic model synthesis, but the schema validates that
draft replies appear only in the urgent bucket and that `send_enabled`, `sent`, and
`live_side_effects_enabled` stay false. Local fixtures use `--fixtures`; read-only live Gmail
retrieval uses the existing `--live-gmail --no-dry-run` gate plus `--run-sdk` or `--live-sdk`.

Constrained Gmail SDK validation must cover:

- Pydantic `EmailTriageResult` parsing.
- `draft_reply` implies `needs_reply=true` and `approval_required=true`.
- no automatic send fields or Gmail send tools.
- style profile use only when approved aggregate style context is present.
- eval compatibility with `tests/evals/gmail_triage_cases.json`.

### Researcher / Account Research

Python gathers fixture records or live search results under `--live-search`, dedupes
sources, and normalizes source-attributed context. The LLM receives
`BusinessResearchSDKInput` and returns `CompanyProfile`. The output must keep source
attribution and missing-information notes. Saving writes the profile and agent-run audit
only when requested.

The broader Business Research Analyst can also receive `ResearchSDKInput` and return
`ResearchBrief` for institutes, conferences, labs, topics, Zotero collections,
and article collections. Local/Zotero context remains private research context
unless a workflow explicitly approves it for external use.

### Opportunity Scout

Python owns search execution, fixture loading, dedupe, source extraction, and initial
scoring. The LLM receives `OpportunityScoutSDKInput` for synthesis into
`OpportunityScoutResult`. It may recommend handoff to the Business Research Analyst, but it must not
draft or send outreach as a side effect.

### Outreach

Python loads only approved company, opportunity, CRM, or contact context. The LLM receives
`OutreachComposerSDKInput` and returns `OutreachDraft`. Generated copy is internal until
human approval; Slack approval notifications remain behind `--live-slack --no-dry-run`.
No external email send path exists.

Constrained Outreach SDK validation must cover:

- Pydantic `OutreachDraft` parsing.
- `approval_required=true`, `approval_scope=external_use`, and pending external-use state.
- `send_enabled=false`, `sent=false`, and `can_send_email=false`.
- approved source-backed `facts_used` and `source_ids_used`.
- unsupported Keystone prior-experience or outcome claims rejected by guardrails.
- approved aggregate style-profile behavior.
- eval compatibility with `tests/evals/outreach_composer_cases.json`.

### Orchestrator

Deterministic `route_request(...)` remains the default router and runs hard safety gates
first. Optional LLM routing can synthesize `OrchestratorResult` only from sanitized request
and workflow-state summaries. Post-processing forces approval gates, no-send fields, and
intended handoff metadata before any caller sees the route.

For the search-heavy routes, the control-plane helper
`run_orchestrated_search_handoff(...)` can execute Researcher/company research or Opportunity Scout
with the same `OrchestratorResult.retrieval_hint` that routing produced, instead of
requiring callers to pass retrieval guidance manually.

## Minimal Harness

Use `keystone_agents.run.run_retrieved_sdk_synthesis(...)` for new live SDK paths. The
helper centralizes the sequence:

- `retrieve`: deterministic Python context retrieval.
- `normalize`: typed sanitized input construction.
- SDK execution through `run_typed_sdk_agent(...)`.
- Pydantic output validation.
- optional `save_agent_run(...)` audit when `save=True`.

The helper rejects unsafe trace metadata before retrieval and requires a storage adapter
before a save-requested model call can start.

The same shared runner enforces `KEYSTONE_AGENT_RUN_BUDGET_USD`, which defaults
to `0.25` per agent SDK run. Cost is estimated from provider-reported usage and
the checked-in pricing table; over-budget runs fail before persistence or
downstream workflow continuation. For true live runs, unknown provider usage or
missing pricing also blocks continuation because Keystone cannot verify the run
stayed under budget.

## Fake/Local First

Exercise constrained SDK paths with fake/local SDK configs before any live model call:

```bash
.venv/bin/python -m pytest tests/test_sdk_execution.py -k "gmail_constrained or outreach_constrained"
```

These tests use `build_local_run_config(...)` with an in-process fake model provider.
They validate structured outputs, approval gates, no-send constraints, style-profile
prompting, source attribution, and static-eval compatibility without credentials,
network access, Gmail, Slack, CRM, LinkedIn, scheduling, or publishing.

## Minimal Live Command Shape

Use live SDK synthesis only with non-sensitive fixture or approved context. These
commands run model synthesis only; they do not send email, post Slack messages,
schedule follow-ups, update CRM, publish LinkedIn content, or create Gmail drafts.

OpenAI-backed Gmail triage:

```bash
KEYSTONE_OPENAI_API_KEY=... \
KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER=openai \
KEYSTONE_GMAIL_TRIAGE_MODEL=<openai-model> \
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --live-sdk \
  --json
```

OpenAI-backed Gmail priority grouping over local fixtures:

```bash
KEYSTONE_OPENAI_API_KEY=... \
KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER=openai \
KEYSTONE_GMAIL_TRIAGE_MODEL=<openai-model> \
.venv/bin/python scripts/run_gmail_triage.py \
  --priority-grouping \
  --fixtures \
  tests/fixtures/sample_email_consulting.txt \
  tests/fixtures/sample_email_collaboration.txt \
  tests/fixtures/sample_email_newsletter.txt \
  tests/fixtures/sample_email_vendor.txt \
  --test-pack-report-dir artifacts/test-pack \
  --live-sdk \
  --json
```

The report artifacts are sanitized operator evidence for the GT-1 test-pack format. They
include bucket assignments, check status, draft/approval flags, and no-send fields; they do
not include raw Gmail bodies or full draft reply text.

Gemini-backed Gmail triage through Gemini's direct OpenAI-compatible endpoint:

```bash
GEMINI_API_KEY=... \
KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER=gemini \
KEYSTONE_GMAIL_TRIAGE_MODEL=<gemini-model> \
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --live-sdk \
  --json
```

Set `KEYSTONE_GMAIL_TRIAGE_BASE_URL=http://localhost:4000/v1` only when routing
through a running LiteLLM or compatible gateway.

Optional OpenAI-backed outreach composition:

```bash
KEYSTONE_OPENAI_API_KEY=... \
KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER=openai \
KEYSTONE_OUTREACH_COMPOSER_MODEL=gpt-5.4-mini \
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --contact-fixture sample_contact_curebase_approved \
  --email-style-profile-fixture sample_email_style_profile_approved \
  --live-sdk \
  --json
```

Gemini-backed outreach composition defaults to Google's direct
OpenAI-compatible endpoint:

```bash
GEMINI_API_KEY=... \
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --contact-fixture sample_contact_curebase_approved \
  --email-style-profile-fixture sample_email_style_profile_approved \
  --live-sdk \
  --json
```

Set `KEYSTONE_OUTREACH_COMPOSER_BASE_URL` or `LITELLM_BASE_URL` only when
intentionally routing through LiteLLM or another reviewed OpenAI-compatible
gateway.

Do not combine SDK synthesis with outbound/live side-effect flags such as
`--live-gmail`, `--create-draft`, `--request-approval`, or `--live-slack`. Keep
`KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false`; the CLI rejects sensitive trace
payloads for SDK synthesis.
