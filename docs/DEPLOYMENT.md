# Local-First Deployment

This project does not deploy a public service by default. The production-safe operating model is a local CLI deployment with SQLite audit storage, deterministic dry-run commands, and narrow opt-in live integrations.

The standing safety contract is: dry-run first, draft-only, approval required, no PHI, no auto-send, and no secrets in the repo.

## 1. Local Development Setup

Use a local checkout and run commands from the repository root:

```bash
cd keystone-business-agents
python3 --version
```

Python 3.11 or newer is required. Tests and dry-run scripts do not require OpenAI, Gmail, Slack, SearXNG, hosted web search, Serper, Firecrawl, Apify, Browserless, or other live API credentials.

## 2. Virtual Environment Setup

Create and install into a local virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

Use `.venv/bin/python` in commands so local runs use the repo dependencies.

## 3. Environment Variable Setup

Start from the example file:

```bash
cp .env.example .env
```

Keep these local defaults for normal operation:

```bash
KEYSTONE_LIVE_MODE=false
KEYSTONE_DRY_RUN=true
DATABASE_URL=sqlite:///keystone_agents.db
MODEL_PROVIDER=openai
KEYSTONE_OPENAI_MODEL=gpt-5.4-mini
KEYSTONE_AGENT_RUN_BUDGET_USD=0.25
KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER=openai
KEYSTONE_GMAIL_TRIAGE_MODEL=gpt-5.4-mini
KEYSTONE_GMAIL_TRIAGE_BASE_URL=
KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL=gpt-5.4-mini
KEYSTONE_OPPORTUNITY_SCOUT_MODEL=gpt-5.4-mini
KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER=openai
KEYSTONE_OUTREACH_COMPOSER_MODEL=gpt-5.4-mini
KEYSTONE_CHIEF_OF_STAFF_MODEL_PROVIDER=openai
KEYSTONE_CHIEF_OF_STAFF_MODEL=gpt-5.4-mini
KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false
```

Use the operator mode helper to switch local repo posture:

```bash
python3 scripts/switch_operator_mode.py dry-run
python3 scripts/switch_operator_mode.py live-test
python3 scripts/switch_operator_mode.py full-live
```

In `live-test`, Opportunity Scout and Researcher/company-research paths default to live search,
Gmail Triage defaults to live Gmail reads, GT-1 priority grouping
auto-selects live SDK execution, and Outreach Composer auto-selects live SDK
execution. Draft-only and approval-gated behavior remains unchanged.

The recommended live research ladder is:

- `SearXNG` for broad recall when no explicit provider override is set.
- `Agents hosted web search` as a capped parallel lane beside SearXNG for
  default live research.
- `Trafilatura` as the default live-gated extractor for selected company pages.
- `Serper` only when explicitly selected for a specific run.
- `Firecrawl` as an explicit search provider when configured, and as an
  optional website extractor for selected company pages.
- `Apify` or `Browserless` only as future structured-enrichment candidates;
  current live operations are placeholders and do not execute.
- `Sandbox` only as a later second-pass review over staged artifacts.

Do not commit `.env`, OAuth files, API keys, tokens, credentials, copied request headers, or database snapshots containing sensitive data. `.gitignore` excludes `.env`, `credentials.json`, `token.json`, common OAuth downloads, and local SQLite files; keep new secret filenames covered before use.

## 4. Running Tests

Run the local quality gates before and after documentation, prompt, schema, tool, storage, or workflow changes:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Current verification from the 2026-05-11 stabilization pass:

- `.venv/bin/python -m pytest -q`: 1035 passed.
- `.venv/bin/python -m ruff check .`: all checks passed.
- `.venv/bin/python scripts/run_evals.py --agent all --json`: 22 passed, 0 failed.

Pytest is fixture and mock based. A test that needs live credentials or network access is not acceptable for the default suite.

## 5. Running Health Check

There is no long-running HTTP service or health endpoint. Use this local health sequence:

```bash
.venv/bin/python scripts/health_check.py
.venv/bin/python scripts/health_check.py --verbose
.venv/bin/python scripts/health_check.py --json
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/run_orchestrator.py --input "health check: triage a consulting inquiry"
.venv/bin/python scripts/run_keystone_pipeline.py \
  --email-fixture tests/fixtures/sample_email_consulting.txt \
  --company-fixture tests/fixtures/sample_company_curebase.json \
  --approval-state pending \
  --markdown
```

Healthy output shows SQLite initialization, a deterministic route, `Email sent: false`, `Send enabled: false`, and a stop before drafting when approval is only `pending`.

The health check is offline. It verifies Python version, runtime imports, prompt files,
registry-derived agent builders, SQLite initialization, dry-run script entrypoints, masked
environment safety, optional live integration readiness, and draft-only/no-auto-send status.
Missing live credentials are OK in normal dry-run mode. If a live flag such as
`KEYSTONE_ENABLE_LIVE_GMAIL=true`, `KEYSTONE_ENABLE_LIVE_SLACK=true`, or
`KEYSTONE_ENABLE_LIVE_RESEARCH=true` is configured, missing credentials become explicit warnings
before an operator runs the corresponding live CLI command. If the local checkout is intentionally
in live-test posture with `KEYSTONE_DRY_RUN=false`, health reports a safety warning even when
credentials are present.

## 6. Running Each Agent In Dry-Run

Gmail triage:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --markdown
```

Researcher / Company Research:

```bash
.venv/bin/python scripts/run_company_research.py \
  --company Curebase \
  --fixture tests/fixtures/sample_company_curebase.json \
  --markdown
```

Opportunity Scout:

```bash
.venv/bin/python scripts/run_opportunity_scout.py \
  --topic "behavioral health AI" \
  --markdown
```

Outreach Composer:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --markdown
```

Orchestrator:

```bash
.venv/bin/python scripts/run_orchestrator.py \
  --input "find behavioral health AI companies for business research"
```

Chief of Staff:

```bash
.venv/bin/python scripts/run_chief_of_staff.py \
  --input "summarize what KNI Slack workflow should handle calendar prep"
```

Add `--save` only when you want local SQLite audit rows.

## 7. Running Human-Loop Demo

First show that the pipeline stops at the approval gate:

```bash
.venv/bin/python scripts/run_keystone_pipeline.py \
  --email-fixture tests/fixtures/sample_email_consulting.txt \
  --company-fixture tests/fixtures/sample_company_curebase.json \
  --approval-state pending \
  --markdown
```

Then run the approved-for-drafting path and save the audit trail:

```bash
.venv/bin/python scripts/run_keystone_pipeline.py \
  --email-fixture tests/fixtures/sample_email_consulting.txt \
  --company-fixture tests/fixtures/sample_company_curebase.json \
  --approval-state approved_for_drafting \
  --save \
  --markdown
```

Create a draft approval request in dry-run Slack mode:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --request-approval \
  --approval-decision pending \
  --save \
  --markdown
```

Terminal decisions are also auditable:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --approval-decision rejected \
  --approval-notes "Not approved for outreach." \
  --save \
  --json
```

## 8. Enabling Live Search

SearXNG, Agents SDK hosted web search, Serper, Firecrawl, and Tavily are
available through the shared `SearchProvider` interface. Live search is opt-in
and should be used with bounded result counts. Prefer SearXNG plus a capped
Agents hosted web-search lane, with Trafilatura selected-page extraction, for
routine live research:

```bash
export SEARCH_PROVIDER=searxng
export SEARXNG_BASE_URL="http://127.0.0.1:18080"
export KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK=true
export KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL=true
export KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN=2
export KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true
export KEYSTONE_WEBSITE_EXTRACTOR=trafilatura
export KEYSTONE_AGENT_HTML_REVIEW=true
export KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES=2
.venv/bin/python scripts/run_opportunity_scout.py \
  --topic "behavioral health AI" \
  --max-results 3 \
  --live-search \
  --no-dry-run \
  --save \
  --markdown
```

Opportunity Scout live search is multi-lane for broad prompts and can deepen
under-filled searches by running bounded adaptive follow-up queries and
SearXNG-compatible page-2 searches. The follow-up per-query result cap defaults
to 8:

```bash
export KEYSTONE_OPPORTUNITY_FOLLOWUP_RESULT_CAP=8
```

Manual Slack and CLI calls can add a planning stage before retrieval or routing.
The default `KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY=openai` uses the dedicated
OpenAI `gpt-5.4-mini` planner for each new live natural-language ask. Set it to
`target` or `target_with_openai_fallback` only for controlled provider
experiments. WorkItem-backed `keystone ask` runs persist the resulting
`ManualRequestPlan` in WorkItem metadata, timeline event metadata, and JSON
output for auditability.

Direct live manual calls use script-backed execution paths where available:
Business Research Analyst, Opportunity Scout, and Gmail Triage run through their
reviewed scripts. In `live-test` / `full-live` mode, explicit named-agent
`keystone ask` / `@KNI` calls auto-enable live SDK model execution unless
`--no-live-sdk` is passed. Outreach Composer blocks until approved
WorkItem/source context is present and remains draft-only.

For SearXNG:

```bash
export SEARCH_PROVIDER=searxng
export SEARXNG_BASE_URL="http://127.0.0.1:18080"
export KEYSTONE_SEARXNG_TRANSIENT=true
```

`KEYSTONE_SEARXNG_TRANSIENT=true` lets one-off Slack/CLI live-search runs start
the repo-local Colima/SearXNG runtime on demand and stop it afterward only when
that run started it.

For Firecrawl search:

```bash
export SEARCH_PROVIDER=firecrawl
export FIRECRAWL_API_KEY="..."
export FIRECRAWL_BASE_URL="https://api.firecrawl.dev"
```

For Tavily fallback/deepening search:

```bash
export TAVILY_API_KEY="..."
export TAVILY_SEARCH_DEPTH=basic
export KEYSTONE_TAVILY_SEARCH_FALLBACK=true
export KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT=1000
export KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT=850
export KEYSTONE_TAVILY_CREDIT_ENFORCEMENT=warn
```

Tavily should usually stay behind SearXNG plus the capped Agents hosted
web-search lane as a coverage deepener. The
local ledger records provider-reported credits when Tavily returns `usage`, and
otherwise falls back to search-depth estimates: `basic`, `fast`, and
`ultra-fast` cost 1 credit/request; `advanced` costs 2. `warn` keeps searches
running while surfacing budget context in retrieval metadata. Use `block` only
when the local monthly cap should stop Tavily before the network request.

For company research:

```bash
.venv/bin/python scripts/run_company_research.py \
  --company Curebase \
  --live-search \
  --no-dry-run \
  --max-results 3 \
  --save \
  --markdown
```

Do not paste search API keys into prompts, fixtures, logs, or committed docs.

Website extraction is separate from search. It is live-gated by
`KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true`, defaults to Trafilatura, and can use
Crawl4AI with `KEYSTONE_WEBSITE_EXTRACTOR=crawl4ai` or Firecrawl with
`KEYSTONE_WEBSITE_EXTRACTOR=firecrawl`. Optional fallback among `trafilatura`,
`crawl4ai`, and `firecrawl` is controlled by `KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK`;
prefer Crawl4AI before Firecrawl when local browser extraction is available.

Apify and Browserless are not live production paths in the current implementation. They return
dry-run placeholders or raise clear `NotImplementedError` for live execution.

## 9. Enabling Live Gmail Draft-Only Mode

Gmail live mode can read messages, apply labels, and optionally create Gmail reply drafts. It never sends email.

Keep Gmail disabled unless you are intentionally testing live Gmail:

```bash
export KEYSTONE_ENABLE_LIVE_GMAIL=false
.venv/bin/python scripts/health_check.py --verbose
```

The health check reports Gmail as `disabled`, `ready`, or `misconfigured`. Missing OAuth files are acceptable while Gmail is disabled. If `KEYSTONE_ENABLE_LIVE_GMAIL=true`, the health check expects both local OAuth files below to exist and be structurally valid.

Configure OAuth files locally. These filenames are ignored by Git:

```bash
export KEYSTONE_ENABLE_LIVE_GMAIL=true
export GOOGLE_CREDENTIALS_FILE=credentials.json
export GOOGLE_TOKEN_FILE=token.json
.venv/bin/python scripts/health_check.py --verbose
```

`credentials.json` should be a Google Desktop OAuth client file. `token.json` should be the local authorized-user token file created outside the test suite. Do not commit these files, paste their JSON into `.env`, include them in fixtures, or print their contents in logs.

Safe read-only check:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1
```

Preview labels before modifying Gmail:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1 \
  --preview-labels
```

Apply labels only after preview:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1 \
  --apply-labels
```

Draft creation requires an additional explicit flag and should only happen after the generated draft text has been reviewed:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1 \
  --create-draft
```

Live draft creation also requires a local `approved_for_send/send` approval record for the
target `gmail_draft`. This creates a Gmail draft only; approval does not send email.

Operational constraints:

- Use a narrow label filter. Do not process the full inbox by default.
- Keep `--max-messages 1` until behavior is reviewed.
- Keep label mutation separate behind `--apply-labels`.
- Treat Gmail drafts as pending human approval.
- Sending remains manual in Gmail and is not implemented in this repo.

## 10. Operator-Controlled Periodic Gmail Triage Plan

The Email Inbox Agent reference includes a scheduling idea, but Keystone does not
install or run an active scheduler. Periodic Gmail triage is an operator routine:
a human starts each run, reviews output, and decides whether to save audit rows
or request approvals.

Safe dry-run periodic check:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --save \
  --markdown
```

For repeated operator jobs, prefer the automation controller over direct script
invocation. The first supported stage is weekly Opportunity Scout dry-run with
SQLite audit rows and no external writes:

```bash
.venv/bin/python scripts/run_keystone_automation.py --json \
  weekly-opportunity \
  --stage dry-run \
  --max-opportunities 3 \
  --database-url sqlite:///.keystone/state/keystone_agents.db
```

After clean dry-runs and approval-queue review, the controller can run live
research/model synthesis while still avoiding Gmail writes:

```bash
.venv/bin/python scripts/run_keystone_automation.py --json \
  weekly-opportunity \
  --stage live-research \
  --confirm-live \
  --live-sdk \
  --max-opportunities 3 \
  --database-url sqlite:///.keystone/state/keystone_agents.db
```

The controller adds health preflight, a lock file, bounded item counts, pending
approval backlog checks, redacted child errors, and a JSON summary with
`send_enabled=false` and `email_sent=false`.

A human operator can repeat that command at a chosen cadence, then review:

```bash
.venv/bin/python scripts/list_approvals.py --status pending
sqlite3 keystone_agents.db \
  "select id, agent_name, status, dry_run, created_at_utc from agent_runs order by id desc limit 10;"
```

Scheduled live Gmail processing is not enabled by default. Do not create cron,
launchd, Task Scheduler, daemon, or background-service entries from this repo
unless the automation controller is used with an operator-reviewed launch plan.
Any scheduler design must use all of these gates:

- A narrow Gmail label filter, never full-inbox processing.
- Explicit live flags such as `--live-gmail --no-dry-run`.
- Small limits such as `--max-messages 1` until the operator expands scope.
- No auto-send and no Gmail `messages.send` implementation.
- Label mutation only with `--apply-labels` after preview.
- Draft creation only with `--create-draft` after human review and local
  `approved_for_send/send` approval.
- `--save` audit logging for every run and every attempted side effect.
- Approval queue review before any generated draft is externally used.
- Health-check and rollback instructions documented before activation.
- Dependency audit, pytest, and ruff checks passing before unattended operation.

## 11. Enabling Live Slack Approval Notifications

Slack notifications post approval requests only. They do not approve, reject, send, schedule, or publish anything.

Configure locally:

```bash
export SLACK_BOT_TOKEN="<set-locally>"
export SLACK_CHANNEL_APPROVALS="C0123456789"
```

Fixture approval notification:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --request-approval \
  --live-slack \
  --no-dry-run \
  --markdown
```

Outreach draft approval notification:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --request-approval \
  --live-slack \
  --no-dry-run \
  --markdown
```

## 12. What Not To Enable Yet

Do not enable or add these paths without a reviewed design, tests, approval gates, and rollback plan:

- Live email sending, SendGrid sending, Gmail `messages.send`, or automatic external messaging.
- Autonomous approval or model-decided approval.
- Uncontrolled scheduled live Gmail processing, outbound campaigns, or follow-ups.
- Cron, launchd, Task Scheduler, daemon, or background service code.
- Live production pipeline mode outside the existing narrow Gmail, SearchProvider,
  website-extraction, and Slack paths.
- CRM writes, Google Docs writes, or Airtable writes as source-of-truth state.
- Full-inbox Gmail processing without a label filter and one-message review period.
- PHI or patient-specific workflows.
- Secret storage in prompts, fixtures, traces, logs, SQLite rows, or committed files.
- LangGraph orchestration replacing the current SDK agent contracts.
- Server-backed storage replacing local SQLite without migrations and backup procedures.

## 13. Rollback Plan

For any unexpected live behavior:

1. Stop running live commands and remove live flags.
2. Reset local defaults: `KEYSTONE_LIVE_MODE=false` and `KEYSTONE_DRY_RUN=true`.
3. Unset or remove affected credentials from `.env` and the shell.
4. Revoke or rotate affected API keys, OAuth tokens, and Slack bot tokens.
5. Preserve the SQLite database and terminal output for review.
6. Inspect audit rows to identify the command, object id, dry-run/live mode, and skipped or attempted side effects.
7. Add or update a regression test before re-enabling the path.
8. If a local database needs rollback, restore the last copied SQLite file and rerun `scripts/init_db.py`.

## 14. Log And Audit Review

SQLite is the local audit layer. Initialize it before saved runs:

```bash
.venv/bin/python scripts/init_db.py
```

Quick table review:

```bash
sqlite3 keystone_agents.db ".tables"
sqlite3 keystone_agents.db \
  "select id, agent_name, status, dry_run, created_at_utc from agent_runs order by id desc limit 10;"
sqlite3 keystone_agents.db \
  "select id, object_type, object_id, decision, scope, reviewer, decision_at_et from approvals order by id desc limit 10;"
sqlite3 keystone_agents.db \
  "select id, company_name, approval_state, created_at_et from outreach_drafts order by id desc limit 10;"
```

Audit review should confirm source attribution, approval state, reviewer notes where applicable, dry-run versus live mode, and that secrets or PHI are absent.

## 15. Security Checklist

- `.env`, OAuth files, token files, and SQLite files are not committed.
- Real API keys are stored only in shell environment or local `.env`.
- `KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false`.
- No PHI or patient-specific content is used in fixtures, prompts, live runs, logs, or approvals.
- Gmail live runs use `--label-filter` and small `--max-messages`.
- Scheduled live Gmail processing is disabled unless a reviewed operator plan says otherwise.
- Gmail draft creation uses `--create-draft` only after human review and local
  `approved_for_send/send` approval.
- Slack approval posts contain draft review context, not secrets or raw credentials.
- Storage rows are reviewed for redaction after new tool paths are added.
- Legal, financial, security, contractual, or professional-advice content is routed to human review.

## 16. Cost-Control Checklist

- Prefer fixture commands and dry-run reports.
- Use `--max-results 3` for first live search checks.
- Keep `KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN` low for first hosted
  web-search checks.
- Use `--max-messages 1` for live Gmail checks.
- Do not pass `--sdk` unless intentionally validating SDK construction or live model behavior.
- Keep global `KEYSTONE_OPENAI_MODEL=gpt-5.4-mini` unless a model change is
  being tested deliberately. Main operating agents default to OpenAI `gpt-5.4-mini`,
  including Gmail Triage and Outreach Composer.
- Keep `KEYSTONE_AGENT_RUN_BUDGET_USD=0.25` unless intentionally testing a
  different per-agent SDK run budget. The shared SDK runner estimates cost from
  provider usage and the local pricing table, then stops before persistence or
  downstream steps if the run exceeds the budget. If provider usage or pricing
  metadata is unavailable on a true live run, the shared runner blocks
  continuation rather than assuming the run was free.
- Gemini remains available for Gmail Triage and Outreach Composer only through
  explicit `KEYSTONE_*_MODEL_PROVIDER=gemini` plus model settings. Confirm
  `GEMINI_API_KEY` before live Gemini SDK runs. Set
  `KEYSTONE_GMAIL_TRIAGE_BASE_URL`, `KEYSTONE_OUTREACH_COMPOSER_BASE_URL`, or
  `LITELLM_BASE_URL` only when intentionally routing through LiteLLM or another
  reviewed gateway.
- To use Gemini as a backup when OpenAI live SDK execution is unavailable, set
  `KEYSTONE_ENABLE_GEMINI_FALLBACK=true` and keep
  `KEYSTONE_GEMINI_FALLBACK_MODEL=gemini-2.5-flash` unless deliberately testing
  another Gemini model.

Rendered-browser diagnostics are optional. Install only when needed:

```bash
python -m pip install '.[browser]'
python -m playwright install chromium
```

Then set `KEYSTONE_PLAYWRIGHT_ENABLED=true` for explicit live rendered-page
checks. Optional screenshots are written under
`KEYSTONE_PLAYWRIGHT_IMAGE_DIR=artifacts/playwright-images`; absolute or
non-artifact paths are ignored. The Keystone Playwright tool is read-only,
HTTP(S)-only, backend/headless, browser-free in default tests, and scoped to
Business Research Analyst, Opportunity Scout, Orchestrator, and Chief of Staff.
It uses a temporary non-persistent browser profile and must not open a
user-screen browser. If the managed Playwright browser binary is missing, the
tool falls back to the locally installed Chrome channel for headless
diagnostics. Use `capture_browser_diagnostics` for console, page-error,
failed-request, response-status, and resource-loading evidence, then pass that
payload to `summarize_rendered_page_diagnostics` before publishing findings.
- Review `gemini_free_tier_usage` in SDK/test-pack reports for Gemini daily
  request-limit context. Unsaved runs report only the current run; saved SDK
  runs aggregate sanitized same-day SQLite audit rows. Treat this as operator
  quota context, not a provider billing statement.
- Keep main operating agents on OpenAI-compatible providers by default. Gemini
  is available only through explicit `KEYSTONE_*` overrides or the reviewed
  fallback path.
- Save audit rows with `--save` so repeated investigations do not require repeated live calls.
- Review live-search output before increasing result counts.
- Disable unused provider keys in `.env`.

## 17. Daily Operating Routine

1. Confirm `.env` is dry-run first. Do not rely on an unattended scheduler.
2. Run the local health sequence.
3. Run any Gmail triage first against fixtures, then against one labeled live message if needed.
4. Review pending approvals before creating any draft in Gmail.
5. Check SQLite audit rows for errors, rejected items, and unexpected live flags.
6. Preserve a copy of the SQLite database before substantial live review sessions.

## 18. Weekly Operating Routine

1. Run full pytest and ruff.
2. Review `.env.example` against current integrations and docs.
3. Review Slack approval channel activity and unresolved approval requests.
4. Review SQLite audit tables for stale drafts, expired approvals, and repeated failures.
5. Rotate or validate live credentials according to the team security schedule.
6. Review live-search usage and result counts for cost control.
7. Update fixtures and tests for any production issue before expanding live scope.
