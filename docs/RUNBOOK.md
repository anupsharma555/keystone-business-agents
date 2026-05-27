# Runbook

This is the short operator runbook. The fuller local-first deployment guide is `docs/DEPLOYMENT.md`.

Default posture: dry-run first, draft-only, approval required, no PHI, no auto-send, and no secrets in the repo.

## Local Setup

```bash
cd keystone-business-agents
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

Keep normal local defaults:

```bash
KEYSTONE_LIVE_MODE=false
KEYSTONE_DRY_RUN=true
DATABASE_URL=sqlite:///keystone_agents.db
KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false
```

Switch modes with the operator helper instead of hand-editing `.env`:

```bash
python3 scripts/switch_operator_mode.py dry-run
python3 scripts/switch_operator_mode.py live-test
python3 scripts/switch_operator_mode.py full-live
```

In `live-test`, Opportunity Scout and Researcher/company-research paths default to live search,
Gmail Triage defaults to live Gmail reads, GT-1 priority grouping
auto-selects live SDK execution, and Outreach Composer auto-selects live SDK
execution. No-send and approval gates remain enforced.

The live research ladder is:

- `SearXNG` for broad recall when no explicit provider override is set.
- `Agents hosted web search` as a capped parallel lane beside SearXNG for
  default live research.
- `Serper` only when explicitly selected for a specific run.
- `Firecrawl` as an explicit search provider when configured, and as an
  optional website extractor for selected company pages.
- `Trafilatura` as the default live-gated website extractor.
- `Apify` or `Browserless` only as future structured-enrichment candidates;
  current live operations are placeholders and do not execute.
- `Sandbox` only as a later second-pass reviewer over staged retrieval and
  enrichment artifacts.

Current local environment setup is complete for the active workflow: OpenAI model access
for research, orchestration, and outreach drafting; Gemini key availability for Gmail
Triage; Gmail OAuth files; Slack approval notifications; SQLite audit storage; and optional
search/enrichment keys. Google Docs,
Google Drive report writing, Airtable, Google Sheets, and CRM write-back remain intentionally
deferred. Add those only when there is a reviewed live-integration plan, explicit flags,
dry-run previews, secret handling, and approval/audit coverage.

## Agents SDK Baseline

Install `openai-agents>=0.14.5`. The 0.14.5 line is the current Keystone baseline because it
contains the Python SDK tracing/runtime fields used here plus the beta sandbox agent and
human-in-the-loop fixes documented in `docs/SANDBOX_AGENTS.md`.

Gemini compatibility uses Google's direct OpenAI-compatible endpoint by default through the
existing OpenAI Agents SDK provider path. LiteLLM remains optional as an external gateway, not
the in-process Python `litellm` package. Run the LiteLLM proxy in a separate environment or
container only when needed, then set `LITELLM_BASE_URL` or the relevant `KEYSTONE_*_BASE_URL`.
Do not install `litellm` into the Keystone `.venv` unless its OpenAI Python dependency range is
compatible with `openai-agents>=0.14.5`; the health check reports an in-process LiteLLM package
that pins `openai` exactly.

Keystone builds `RunConfig` through `src/keystone_agents/sdk.py` instead of constructing it
inline in agents. Keep live traces named with `KEYSTONE_TRACE_WORKFLOW_NAME`, group related runs
with `KEYSTONE_TRACE_GROUP_ID`, and put only low-risk scalar values in `KEYSTONE_TRACE_METADATA`.
Never put secrets, full email bodies, PHI, prompt text, draft bodies, or copied source content in
trace metadata.

Keep `KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false` for live runs unless a human has approved a
temporary debugging exception. The upstream SDK traces model and tool spans by default, and its
own default may include sensitive inputs and outputs unless Keystone overrides it.

Input guardrails should remain blocking for live Keystone agents when the guardrail prevents
tool execution or token spend. SDK sessions, server-managed conversations, MCP servers, sandbox
execution, and external trace processors should be introduced only behind explicit live flags,
with dry-run fixtures and redaction reviewed first.

## Test And Health Check

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
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

Current verification from the 2026-05-11 stabilization pass:

- `.venv/bin/python -m pytest -q`: 1035 passed.
- `.venv/bin/python -m ruff check .`: all checks passed.
- `.venv/bin/python scripts/run_evals.py --agent all --json`: 22 passed, 0 failed.

Expected safety signals: no email sent, send disabled, and pending approval stops before outreach drafting.

The health check is offline. It reports Python version, package imports, prompt files,
registry-derived agent builder functions, SQLite initialization, dry-run script entrypoints,
masked environment safety, live integration readiness, `AUTO_SEND_EMAIL`, model provider status,
and test fixture availability. It does not make live network calls, does not require credentials
for dry-run mode, does not start OAuth browser flows, and must not print secrets.

Missing optional live credentials do not fail health checks in normal dry-run mode. They become warnings when a live intent flag is configured, such as `KEYSTONE_ENABLE_LIVE_GMAIL=true`, `KEYSTONE_ENABLE_LIVE_SLACK=true`, or `KEYSTONE_ENABLE_LIVE_RESEARCH=true`. Gmail is reported separately as `disabled`, `ready`, or `misconfigured`. Treat any enabled `AUTO_SEND_EMAIL` value as a high-severity warning because this repo is draft-only. If Gmail live credentials are configured but no approval queue channel is set, configure `SLACK_CHANNEL_APPROVALS` or keep Gmail live mode disabled. If `SLACK_BOT_TOKEN` is configured but `SLACK_CHANNEL_APPROVALS` is missing, Slack approval notifications are not ready. If this checkout is intentionally in live-test posture with `KEYSTONE_DRY_RUN=false`, health remains a warning by design.

## Static Agent Evals

Static evals measure deterministic fixture-mode agent quality beyond unit coverage.
They do not call live models, Gmail, Slack, SearXNG, hosted web search, Serper,
Apify, Browserless, or other external APIs.

Run all four specialist eval suites:

```bash
.venv/bin/python scripts/run_evals.py --agent all --markdown
```

Run one suite:

```bash
.venv/bin/python scripts/run_evals.py --agent gmail --json
.venv/bin/python scripts/run_evals.py --agent company --markdown
.venv/bin/python scripts/run_evals.py --agent scout --markdown
.venv/bin/python scripts/run_evals.py --agent outreach --markdown
```

Eval cases live in `evals/static/`:

- `gmail_triage_cases.json`
- `business_research_analyst_cases.json`
- `opportunity_scout_cases.json`
- `outreach_composer_cases.json`

The static eval API is `keystone_agents.evals.run_static_evals()`. Scoring is
deterministic and checks category correctness, risk flags, source attribution,
fit and ranking quality, unsupported claims, approval gates, tone, and outbound
copy constraints.

## Dry-Run Agents

```bash
.venv/bin/python scripts/run_gmail_triage.py --fixture tests/fixtures/sample_email_consulting.txt --markdown
.venv/bin/python scripts/run_company_research.py --company Curebase --fixture tests/fixtures/sample_company_curebase.json --markdown
.venv/bin/python scripts/run_opportunity_scout.py --topic "behavioral health AI" --markdown
.venv/bin/python scripts/run_outreach_draft.py --fixture sample_company_curebase --opportunity-fixture sample_lead_curebase --markdown
.venv/bin/python scripts/run_outreach_draft.py --fixture sample_company_curebase --opportunity-fixture sample_lead_curebase --include-call-prep --markdown
.venv/bin/python scripts/run_orchestrator.py --input "find behavioral health AI companies"
.venv/bin/python scripts/run_chief_of_staff.py --input "which KNI Slack workflow should handle calendar prep?"
.venv/bin/python scripts/export_pipeline_table.py --provider dry-run --object-type opportunities --json
.venv/bin/python scripts/export_pipeline_table.py --dashboard
```

Add `--save` only when local SQLite audit rows are wanted.

## Agent Registry Check

Registered SDK agent cards are available through the CLI. Use this when adding
or changing an agent, prompt, tool, schema, live flag, eval, or validation path:

```bash
keystone agents list
.venv/bin/python -m keystone_agents.cli agents list --json
```

Expected routes: `gmail_triage`, `business_research_analyst`,
`opportunity_scout`, `outreach_composer`, `orchestrator`, and
`chief_of_staff`.

## WorkItem Natural-Language Smoke Test

Use WorkItem mode when you want a stateful natural-language workflow with
artifact refs, approval gates, blockers, and next actions. Each advance records
the selected typed context pack plus readiness-gate status in the WorkItem
timeline, including `can_synthesize`, `missing_requirements`, and
`limitation_notes` so blocked or partial runs explain what context was missing.
`keystone ask` without `--agent` is the preferred natural-language
entrypoint because it also records the `ManualRequestPlan` in WorkItem metadata,
timeline event metadata, and JSON output.

Current architecture is Orchestrator-first and schema-light. For normal
natural-language asks, Keystone captures the raw request and compact context,
runs Orchestrator preflight, applies Python safety/source/approval gates, then
calls the selected specialist with both the raw request and Orchestrator memo.
The Orchestrator can also review the specialist output and feed back one repair
or deepening pass when the manager loop permits it. Planning notes should stay
in existing `ManualRequestPlan`, `OrchestratorResult`, WorkItem timeline events,
`decision_trace`, audit notes, and context packs rather than new broad intent
schemas.

Specialists should remain close to the user request. The Orchestrator memo is
context and control-plane guidance, not a replacement for the raw operator text.
Explicit `@KNI <agent>` mentions are treated as advisory route signals; they do
not skip Orchestrator preflight, deterministic gates, approval checks, or
specialist output review.

```bash
DB_URL=sqlite:////tmp/keystone-context-pack-smoke.db

.venv/bin/python -m keystone_agents.cli ask \
  --database-url "$DB_URL" \
  "research NeuroFlow"

.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "continue" \
  --database-url "$DB_URL" \
  --max-results 2

.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "draft outreach to NeuroFlow" \
  --database-url "$DB_URL"
```

Expected behavior: research creates a company profile artifact, continue scouts
opportunities, and outreach blocks until approved source-backed context exists.
Approve the selected company profile, then continue:

```bash
.venv/bin/python -m keystone_agents.cli work-items approve-context <work_item_id> \
  --artifact company_profile:1 \
  --database-url "$DB_URL"

.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "continue" \
  --database-url "$DB_URL"

.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "continue" \
  --database-url "$DB_URL" \
  --langgraph \
  --json

.venv/bin/python -m keystone_agents.cli work-items timeline <work_item_id> \
  --database-url "$DB_URL" \
  --json
```

`--langgraph` routes the same WorkItem advancement through the optional graph
wrapper. If the `orchestration` extra is not installed, local dry-run execution
uses the dependency-free node contract and still records a
`langgraph_orchestration` timeline event. With LangGraph installed, the same
entrypoint can use graph thread IDs and future checkpointers for resumable
approval workflows.

Expected final safety signal: Outreach Composer creates a draft-only approval
item, status becomes `needs_approval`, no external message is sent, and
`external_use_readiness` remains false unless separately approved.

For Slack parity, new `@KNI workitem "..."` starts should route through
`keystone ask` and then save WorkItem state. WorkItem management actions such as
`continue`, `show`, `timeline`, `select`, and `approve-context` remain direct
`work-items` subcommands. Set `KNI_BUSINESS_AGENTS_LANGGRAPH=true` in the
`keystone-slack` environment to route Slack WorkItem advancement through the
same optional graph wrapper. Slack button actions such as continue, run again,
more research, find contact, and revise draft now attach Orchestrator preflight
context before advancing the WorkItem and record `orchestrator_action_review`
metadata after execution. When the Slack bridge invokes
`scripts/handle_slack_agent_action.py --feedback-jsonl`, progress events include
Orchestrator preflight, manager-loop review, repair/deepening decisions, and
completion status; feedback events are read-only and do not approve side
effects.

Use live SDK synthesis over local Gmail fixtures for GT-1 priority grouping. This calls the
model but does not read Gmail, create Gmail drafts, apply labels, send email, or post Slack:

```bash
KEYSTONE_OPENAI_API_KEY=... \
.venv/bin/python scripts/run_gmail_triage.py \
  --priority-grouping \
  --fixtures \
  tests/fixtures/sample_email_consulting.txt \
  tests/fixtures/sample_email_collaboration.txt \
  tests/fixtures/sample_email_newsletter.txt \
  tests/fixtures/sample_email_vendor.txt \
  --live-sdk \
  --test-pack-report-dir artifacts/test-pack \
  --json
```

The report directory receives sanitized `gmail-triage-gt1-priority-grouping.md` and
`gmail-triage-gt1-priority-grouping.json` artifacts. They record the test-pack status,
bucket assignments, draft/approval flags, and no-send fields without storing full draft text
or raw Gmail bodies.

Use live search plus live SDK synthesis for the Business Research Analyst BR-1 company focused brief. This
retrieves live source records first, then runs the structured brief model; it does not send
email, post Slack messages, write CRM data, or publish anything:

```bash
KEYSTONE_OPENAI_API_KEY=... \
.venv/bin/python scripts/run_company_research.py \
  --company Curebase \
  --improvement-case br-1 \
  --live-search \
  --search-provider serper \
  --no-dry-run \
  --live-sdk \
  --orchestrator-review \
  --json
```

## Local Review Reports

Markdown reports are local-only reviewer artifacts. They do not write to Google Docs, Google
Drive, Slack, Gmail, Airtable, Sheets, or any other live destination.

```bash
.venv/bin/python scripts/run_company_research.py --company Curebase --fixture tests/fixtures/sample_company_curebase.json --markdown
.venv/bin/python scripts/run_opportunity_scout.py --topic "behavioral health AI" --markdown
.venv/bin/python scripts/run_outreach_draft.py --fixture sample_company_curebase --opportunity-fixture sample_lead_curebase --markdown
.venv/bin/python scripts/run_keystone_pipeline.py \
  --email-fixture tests/fixtures/sample_email_consulting.txt \
  --company-fixture tests/fixtures/sample_company_curebase.json \
  --approval-state approved_for_drafting \
  --markdown
```

Expected report sections include source links, facts used, risks, missing information, approval
status, and recommended next action. Full inbound email bodies are omitted; body-like exports use
summaries or hashes. Reports must not contain API keys, OAuth tokens, PHI, full prompt text, or
unapproved outbound-send instructions.

## Human-Loop Demo

Show the gate:

```bash
.venv/bin/python scripts/run_keystone_pipeline.py \
  --email-fixture tests/fixtures/sample_email_consulting.txt \
  --company-fixture tests/fixtures/sample_company_curebase.json \
  --approval-state pending \
  --markdown
```

Then allow drafting and save the audit trail:

```bash
.venv/bin/python scripts/run_keystone_pipeline.py \
  --email-fixture tests/fixtures/sample_email_consulting.txt \
  --company-fixture tests/fixtures/sample_company_curebase.json \
  --approval-state approved_for_drafting \
  --save \
  --markdown
```

Request draft approval in dry-run Slack mode:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --request-approval \
  --approval-decision pending \
  --save \
  --markdown
```

Create a local follow-up recommendation as data only:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --include-follow-up-schedule \
  --follow-up-date 2026-05-01 \
  --save \
  --markdown
```

This saves a `follow_up_schedules` row only when `--save` is passed. It does not schedule a
Gmail send, create a background job, create a CRM task, or authorize outbound copy.

## Live Search Providers

Search defaults to `SEARCH_PROVIDER=dry-run`, which never makes network calls. Live company
research and opportunity scouting should use SearXNG plus a capped Agents hosted
web-search lane for routine discovery, and Trafilatura for selected-page
extraction. Serper remains available only when explicitly selected for a
specific run.

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
  --search-provider searxng \
  --save \
  --markdown
```

To dedupe against known pipeline state, pass a local JSON state file. With `--save` and no
`--existing-state`, the CLI also checks existing SQLite `opportunities` rows before saving new
Scout records.

```bash
.venv/bin/python scripts/run_opportunity_scout.py \
  --topic "trial technology" \
  --max-results 3 \
  --live-search \
  --no-dry-run \
  --search-provider serper \
  --existing-state tests/fixtures/opportunity_scout_existing_state.json \
  --json
```

For SearXNG, set `SEARCH_PROVIDER=searxng` and `SEARXNG_BASE_URL`, or pass
`--search-provider searxng`.

For the repo-local SearXNG instance:

```bash
./scripts/manage_searxng_headless.sh start
export SEARXNG_BASE_URL="http://127.0.0.1:18080"
export KEYSTONE_SEARXNG_TRANSIENT=true
```

This uses the `kba-searxng` Colima profile and port `18080`, separate from the
`keystone-slack` SearXNG runtime. Live company and Opportunity Scout retrieval
transiently start this local runtime when a run needs SearXNG and the endpoint
is not already reachable. They stop it afterward only when the current process
started it; an already-running runtime is left alone.

Slack scheduled automations that delegate to Business Agents should preserve
this boundary. The Slack repo has its own `kni-searxng` profile on port `8080`,
but Business Agents child runners load this repo's `.env` and should use
`18080`. Do not add Slack-side `8080` SearXNG preflights for those delegated
Business Agents runs; let the child runner report search diagnostics if the
Business Agents endpoint is unavailable.

For Firecrawl search, set `SEARCH_PROVIDER=firecrawl`, `FIRECRAWL_API_KEY`, and
optionally `FIRECRAWL_BASE_URL`, or pass `--search-provider firecrawl`.

For Opportunity Scout OS-1 improvement runs, fallback providers should be explicit
non-Serper exceptions, such as SearXNG or Firecrawl, while Serper credits are unavailable:

```bash
.venv/bin/python scripts/run_opportunity_scout.py \
  --improvement-case os-1 \
  --live-search \
  --search-provider searxng \
  --fallback-search-provider firecrawl \
  --no-dry-run \
  --live-sdk \
  --orchestrator-review \
  --json
```

The fallback does not bypass input safety guardrails. If a query is blocked as sensitive, the run
stops before trying the backup provider. Search-provider outputs are checked per result; unsafe
individual hits are dropped instead of failing the whole search run.

Live search is implemented for Researcher company-research paths and opportunity scouting. It requires
`--live-search --no-dry-run` and provider configuration. Use small result counts first and
preserve source attribution. The shared `SearchProvider` supports dry-run, SearXNG,
Agents hosted web search, Serper, Firecrawl, and Tavily. The deterministic
retrieval ladder defaults to SearXNG plus a capped Agents hosted web-search lane
when no explicit provider is selected; an explicit `--search-provider` uses that provider first, and
`--fallback-search-provider` can recover from provider configuration or provider response errors
without bypassing input guardrails.

Opportunity Scout live search is provider-gated: dry-run providers are rejected in live mode,
results are normalized into source bundles, duplicate source URLs and duplicate companies are
collapsed, and existing approved/drafted/rejected/archived pipeline state is skipped instead
of creating a new opportunity. High-priority or under-corroborated records are marked for
Business Research Analyst handoff. Review `search_provider`, `search_provider_sequence`,
`search_providers_used`, `provider_error_fallback_used`, `search_queries`,
`raw_search_result_count`, `deduped_candidate_count`, `source_bundle_quality_notes`,
`score_breakdown`, and `business_research_analyst_handoff_recommendation` before handing
records to Business Research Analyst. No outreach copy is generated.

For broad Opportunity Scout prompts, search is intentionally multi-lane rather
than role-only: company growth, collaboration, researcher, institute, conference,
grant, trial, and role lanes can all run in one pass. If accepted records
under-fill, Scout can run bounded coverage-aware follow-up queries, adaptive
follow-up queries, and SearXNG-compatible page deepening capped at page 3. The
follow-up result cap defaults to 8 and can be lowered with
`KEYSTONE_OPPORTUNITY_FOLLOWUP_RESULT_CAP` to conserve provider credits.
Coverage follow-up is controlled by `KEYSTONE_ENABLE_SEARCH_COVERAGE_FOLLOWUP`
and `KEYSTONE_SEARCH_COVERAGE_FOLLOWUP_QUERY_CAP`.

Run `scripts/run_search_coverage_eval.py` against
`evals/provider/search_coverage_cases.jsonl` when deciding whether current providers miss
useful websites. This eval measures source-lane and expected-domain recall; use
browser extraction evals only after URLs have already been discovered.

Tavily is budget-aware when used as a provider or fallback. Keystone requests
`include_usage=true` and records provider-reported credits when present; if a
response omits usage, it estimates search credits from `TAVILY_SEARCH_DEPTH`
(`basic`, `fast`, and `ultra-fast` = 1 credit/request; `advanced` = 2). Defaults
are free-plan friendly:

```bash
export TAVILY_SEARCH_DEPTH=basic
export KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT=1000
export KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT=850
export KEYSTONE_TAVILY_CREDIT_ENFORCEMENT=warn
```

The default `warn` mode does not block retrieval. Set
`KEYSTONE_TAVILY_CREDIT_ENFORCEMENT=block` only when the local monthly cap should
prevent additional Tavily requests. Budget context appears in retrieval metadata
as `tavily_credit_budget` and provider usage includes `credits_used`.

Website extraction is a separate live-gated enrichment step for selected company pages. Enable it
only when needed:

```bash
export KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true
export KEYSTONE_WEBSITE_EXTRACTOR=trafilatura
export KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK=firecrawl
export FIRECRAWL_API_KEY="..."
```

Trafilatura is the default extractor. Firecrawl can be selected with
`KEYSTONE_WEBSITE_EXTRACTOR=firecrawl` or used as a fallback. Extracted page text is converted
into source-backed claim candidates; it is not a generic crawl or outbound action.

Apify and Browserless remain dry-run or placeholder paths. Their live network operations are not
implemented.

Future Browserless evaluation options only:

These capabilities are not implemented, not wired into any current agent, and not available to
agent tools today. They are documented only as candidates for a future reviewed integration.

- Smart Scrape: send one URL and receive structured content, with Browserless-managed escalation
  from basic HTTP fetch to headless browser rendering and CAPTCHA handling when needed.
- Search: web, news, and image search with geo-targeting and time filters, returning LLM-ready
  results for source-attributed research.
- Map: send one URL and discover site pages through sitemaps and link extraction before deciding
  what to research.
- Crawl: crawl and scrape a configured site with depth, filters, and output formats.

If these paid Browserless cloud features are added later, they must become explicit provider
tools behind `ScrapeProvider` or `SearchProvider`, remain dry-run by default, require
`BROWSERLESS_API_KEY` plus an explicit live flag, preserve source attribution, respect site terms
and privacy constraints, and stay out of pytest network paths.

## Source Quality

Researcher/company research and opportunity scouting score every source locally with `SourceQualityScore`.
The scorer classifies sources as `company_site`, `linkedin`, `news`, `funding_database`,
`academic`, `government`, `social`, or `unknown`, then combines credibility, recency, and
relevance into a 0 to 100 source confidence score.

Use source confidence as a review signal:

- Company websites are high-confidence for company-controlled facts.
- Peer-reviewed and government sources are high-confidence evidence.
- Reputable news and funding databases are useful but should be corroborated.
- LinkedIn is useful for persona and signal context, but weaker for factual claims.
- Social posts are signals, not definitive facts.
- Stale sources and unknown domains lower confidence.

Reports show source confidence for Researcher/company research and opportunity records. Low-confidence or
single-source findings should be researched further before outreach.

Business Research Analyst source enrichment starts from source records rather than snippets whenever
possible. Dry-run page/profile inputs are cleaned, converted to claim candidates, normalized to
`SourceRecord`, deduped, ranked, and then synthesized into `CompanyProfile`. Contradictions,
stale source evidence, missing website/profile evidence, missing data points, and unsupported
claims remain explicit review signals. Browserless and Apify live paths stay gated and are not
used by tests.

## Researcher Data Points

The Business Research Analyst mirrors Mira's configurable company research target pattern with local
schema-safe `ResearchDataPoint` records for legacy `CompanyProfile` outputs. Broader
`ResearchBrief` outputs use source-cited facts, article summaries, unknowns, limitations, and
next steps for institutes, conferences, topics, Zotero collections, and article collections.
Keystone defaults cover business model, customer segment, behavioral health relevance,
AI/data science relevance, research signals, funding/growth signal, compliance sensitivity,
and consulting fit for company runs.

Completed data points must include confidence and source IDs. Missing data points stay explicit
with a missing reason. Profile confidence considers source quality plus source-backed data point
coverage, so a company with weak or missing data points should be reviewed before outreach.

## Outreach Context And Call Prep

Outreach drafts are based only on approved lead, company, opportunity, contact, CRM, Keystone
positioning, and source-backed evidence records. The draft payload includes `OutreachContext`
with facts used, blocked facts, source IDs, and the pending external-use approval state. Missing
or unapproved context blocks drafting or is flagged instead of being used.

Use `--include-call-prep` only when an internal call-prep artifact is wanted. Call prep is
draft-only internal material and includes discovery questions, meeting objectives, known
source-backed facts, unknowns, risks, and a suggested next step. It must not include unsupported
claims, medical/legal/tax/regulatory advice, PHI, draft bodies, or sending behavior.

## Local Memory And Dedup

Keystone memory is local, typed, source-aware data. It is not hidden prompt mutation and it does
not create live side effects. Memory records are saved in SQLite as safe summaries plus source IDs,
with a separate local text index for retrieval. Raw inbound email bodies, secrets, PHI,
patient-specific content, unapproved claims, and full sent-email archives must not be stored as
memory.

The first supported memory loops are:

- Company, broader research, and opportunity memory from source-backed Business Research Analyst and Opportunity Scout
  outputs.
- Aggregate email style profiles derived only from approved drafts and human feedback.
- Workflow dedup markers for completed or pending company research, opportunity discovery,
  company outreach, and matching outbound email drafts.

Before repeating work, agents should check workflow dedup memory for the relevant action:

- `company_research`: avoid researching the same company again unless the operator requests a
  refresh.
- `opportunity`: avoid adding the same outreach opportunity twice.
- `outreach_company`: avoid drafting outreach to the same company twice.
- `outbound_email`: avoid recreating the same email draft for the same company, contact, subject,
  and body hash.

Dedup records are data-only review aids. They do not approve outreach, send email, schedule
follow-ups, or block a human override when a refresh is intentional.

## Tool Audit Events

Local saves record tool-level audit events in SQLite `tool_events` so reviewers can reconstruct
what storage tools did during a run. Events include tool name, agent name when known, run id when
known, redacted input and output summaries, hashes, dry-run state, status, errors, and ET/UTC
timestamps.

Tool events are summaries, not payload archives. They must not contain API keys, tokens, full
email bodies, PHI, or draft bodies. Body-like fields are represented by hashes and lengths.

## Local Operator Dashboard And Table Mirror

The local operator dashboard and table mirror produce markdown or JSON for human review.
SQLite remains the source of truth for saved records. These commands do not start a server,
do not create a web frontend, and do not write to Airtable, Google Sheets, Slack, Gmail, or
any other live system.

Dashboard decision:

```bash
.venv/bin/python scripts/export_pipeline_table.py --dashboard-decision
```

Near-term decision: use the SQLite-backed local dashboard and table mirror exports. They are
sufficient for review of approvals, drafts, outreach tracking, opportunities, companies,
feedback, and recent agent runs. A later dashboard/API should start as localhost-only,
SQLite-backed, redacted, and GET-only. Provider writes, sends, approval updates, CRM syncs,
and scheduled actions need separate reviewed commands with explicit flags, approval gates, and
audit logging.

Supported object types:

- `opportunities`
- `companies`
- `drafts`
- `approvals`
- `feedback`
- `audit_records`
- `contacts`
- `crm_contexts`
- `outreach_tracking`

SQLite dashboard export:

```bash
.venv/bin/python scripts/export_pipeline_table.py --dashboard
```

SQLite table exports:

```bash
.venv/bin/python scripts/export_pipeline_table.py --source sqlite --object-type opportunities
.venv/bin/python scripts/export_pipeline_table.py --source sqlite --object-type approvals
.venv/bin/python scripts/export_pipeline_table.py --source sqlite --object-type feedback
.venv/bin/python scripts/export_pipeline_table.py --source sqlite --object-type audit_records
.venv/bin/python scripts/export_pipeline_table.py --source sqlite --object-type outreach_tracking
```

Dry-run examples:

```bash
.venv/bin/python scripts/export_pipeline_table.py --provider dry-run --object-type opportunities
.venv/bin/python scripts/export_pipeline_table.py --provider dry-run --object-type companies --json
.venv/bin/python scripts/export_pipeline_table.py --provider dry-run --object-type approvals --json
```

The output includes the table name, rows, and a source-field to table-column mapping. Full email
bodies and full draft text are omitted from exports; body fields are represented with summaries
or hashes. Treat mirrored draft and approval records as manual-review artifacts only. They do
not grant approval and do not send or publish anything.

Outreach tracking records are manual lifecycle snapshots for draft ID, channel, sent status,
reply status, outcome, and next step. They are separate from draft generation and Gmail draft
creation. Updating tracking does not send outreach, schedule follow-ups, publish copy, or prove
that a reply was received unless the operator entered that status from an external source.

```bash
.venv/bin/python scripts/update_outreach_tracking.py \
  --draft-id 1 \
  --company-name Curebase \
  --lifecycle-status sent_manually \
  --outreach-sent \
  --outcome pending_reply
```

Live provider placeholders:

```bash
.venv/bin/python scripts/export_pipeline_table.py --provider airtable --object-type opportunities
.venv/bin/python scripts/export_pipeline_table.py --provider google-sheets --object-type opportunities
```

These fail clearly unless `--live` is passed. Even with `--live`, Airtable and Google Sheets
mirroring are not implemented yet and must not be used as a production path. Future live
implementations must require explicit credentials, explicit live flags, tests with mocked providers,
audit logging, and review of all outbound table fields before any API write.

Future Airtable or Google Sheets support should be treated as a human-readable mirror, not a
replacement system of record. SQLite must remain canonical for approval gates, audit logs,
source IDs, feedback, and workflow status. A safe first implementation should be one-way:

```text
SQLite canonical rows -> redacted table mirror -> Airtable or Google Sheets review dashboard
```

Mirrored rows should include stable cross-links rather than own the workflow state:

- `sqlite_record_id`
- `object_type`
- `object_id`
- `source_agent`
- `approval_status`
- `sqlite_dashboard_link` or local dashboard reference
- `airtable_record_url`
- `google_sheet_row_url`
- `last_mirrored_at`
- `mirror_checksum`

External table edits should not unlock Gmail drafts, approve outreach, or update CRM records
by themselves. If reviewer edits are later imported from Airtable or Google Sheets, use an
explicit import command that validates the approval transition, writes the result back to SQLite,
records an audit event, and rejects stale rows when `mirror_checksum` no longer matches.
Never mirror secrets, OAuth tokens, full inbound email bodies, PHI, raw private notes, or
unapproved outbound-send instructions.

## Automation Controller

Use `scripts/run_keystone_automation.py` for any scheduled or repeated operator job. It
adds a health preflight, exclusive lock file, bounded item counts, stage-specific live
flags, JSON audit summary, redacted child errors, and pending-approval backlog checks.
Global flags such as `--json`, `--lock-file`, and `--no-health-preflight` go before
the subcommand.

Chief of Staff can now audit the local automation inventory, recent runs, channel
bindings, pending approvals, and active WorkItems without live writes:

```bash
.venv/bin/python -m keystone_agents.cli automations audit \
  --database-url sqlite:///.keystone/state/keystone_agents.db
```

List configured automations or recent runs:

```bash
.venv/bin/python -m keystone_agents.cli automations list \
  --database-url sqlite:///.keystone/state/keystone_agents.db

.venv/bin/python -m keystone_agents.cli automations runs \
  --database-url sqlite:///.keystone/state/keystone_agents.db
```

The Chief of Staff publishing tools support local/dry-run Google Doc reports,
Airtable-shaped review rows, and internal Slack summaries. SQLite and WorkItems
remain canonical. Scoped Airtable record reads/writes use typed tools and require
allowed tables, explicit live flags, write env gates, and an approval or command
audit reference.

Finance/tax tracker setup for `2026 Finance & Tax Tracker`:

```bash
# Configure later with a scoped Airtable PAT:
# AIRTABLE_BASE_ID=app...
# AIRTABLE_ACCESS_TOKEN=pat...
# AIRTABLE_ALLOWED_TABLES="Business Income,Business Expenses,Personal Income,Personal Expenses,Tax Payments"
# AIRTABLE_WRITE_DRY_RUN=true

.venv/bin/python scripts/run_chief_of_staff.py \
  "chief of staff inspect the 2026 Finance & Tax Tracker Airtable schema and save a bounded context note"
```

The first pass should run with `AIRTABLE_WRITE_DRY_RUN=true`: fetch schema,
read 1-3 records from each allowed table, prepare a dry-run write, and save
schema/rule summaries only. After validating one test create/update, set
`AIRTABLE_ALLOW_WRITES=true` and `AIRTABLE_WRITE_DRY_RUN=false` only for the
approved command window. Google Drive running-update notes should be written as
scoped internal artifacts under `KNIOps/Finance Tax Tracker Updates`; do not use
them as final tax advice. Tracker semantics should be captured in
`documents/finance_tax_tracker_context.md`: `Estimated Tax Periods` (formerly
`Quarter`) is the authoritative estimated-tax period field, `Total Expenses` is
the preferred expense amount, `Tax Payments` are excluded from expense totals
unless explicitly requested, and the default tax profile is U.S. federal,
Pennsylvania, and Philadelphia.

First scheduled job should be weekly Opportunity Scout dry-run with local audit storage:

```bash
.venv/bin/python scripts/run_keystone_automation.py --json \
  weekly-opportunity \
  --stage dry-run \
  --max-opportunities 3 \
  --database-url sqlite:///.keystone/state/keystone_agents.db
```

This saves local SQLite audit rows and approval queue items only. It does not use live
search, live model execution, Gmail, or live Slack. By default it blocks if pending
approval items already exist; review, expire, archive, or explicitly pass
`--allow-pending-approvals` before accumulating more queue items.

After several clean dry-runs, enable live research/model calls without Gmail writes:

```bash
.venv/bin/python scripts/run_keystone_automation.py --json \
  weekly-opportunity \
  --stage live-research \
  --confirm-live \
  --live-sdk \
  --max-opportunities 3 \
  --database-url sqlite:///.keystone/state/keystone_agents.db
```

Optional live Slack approval notification requires both `--notify-slack` and
`--live-slack`. Slack approval notifications do not approve, send, schedule, or
publish anything.

Gmail automation remains staged and scoped. Start with label previews only:

```bash
.venv/bin/python scripts/run_keystone_automation.py --json \
  gmail-triage \
  --stage label-preview \
  --confirm-live \
  --label-filter "Keystone/Triage" \
  --gmail-query "newer_than:1d" \
  --max-messages 1
```

Apply labels only after at least two reviewed successful previews:

```bash
.venv/bin/python scripts/run_keystone_automation.py --json \
  gmail-triage \
  --stage label-apply \
  --confirm-live \
  --apply-labels-approved \
  --successful-preview-count 2 \
  --label-filter "Keystone/Triage" \
  --gmail-query "newer_than:1d" \
  --max-messages 1
```

Gmail draft creation is a final gated stage. It requires a target Gmail query, one
message maximum, operator confirmation, and the existing local
`approved_for_send/send` approval record for the selected Gmail message:

```bash
.venv/bin/python scripts/run_keystone_automation.py --json \
  gmail-triage \
  --stage draft-create \
  --confirm-live \
  --draft-create-approved \
  --label-filter "Keystone/Triage" \
  --gmail-query "rfc822msgid:<message-id@example.com>" \
  --max-messages 1
```

Sending remains manual and outside Keystone v1.

## Live Gmail Draft-Only

Default state:

```bash
export KEYSTONE_ENABLE_LIVE_GMAIL=false
.venv/bin/python scripts/health_check.py --verbose
```

The expected Gmail health status is `disabled`. OAuth files are optional in this state.

Enable live Gmail only for a scoped, human-run check. Put OAuth files at ignored local paths:

```bash
export KEYSTONE_ENABLE_LIVE_GMAIL=true
export GOOGLE_CREDENTIALS_FILE=credentials.json
export GOOGLE_TOKEN_FILE=token.json
.venv/bin/python scripts/health_check.py --verbose
```

`credentials.json` is the Google Desktop OAuth client file. `token.json` is the local authorized-user token file generated outside this repo's tests. Both filenames are ignored by Git. Do not commit them, paste their JSON into `.env`, add them to fixtures, or print their contents in logs.

Read one scoped message without modifying Gmail labels or drafts:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1
```

Preview label changes without modifying Gmail:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1 \
  --preview-labels
```

Apply recommended Keystone labels only after reviewing the preview:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1 \
  --apply-labels
```

Keystone-managed Gmail labels use one primary category label plus overlay labels. Primary
labels include consulting opportunity, collaboration, vendor, suspicious, newsletter, and other.
Overlay labels include action required, manual review, security review, PHI blocked, finance
review, legal review, and draft pending approval.

Obsolete Keystone-managed labels are not removed unless `--cleanup-labels` is passed. Use
`--preview-labels --cleanup-labels` first to review the intended removals before applying them
with `--apply-labels --cleanup-labels`.

Create a Gmail draft only after human review records a local approval for
`object_type=gmail_draft`, the Gmail `message_id` as `object_id`, `decision=approved_for_send`,
and `scope=send`:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --live-gmail \
  --no-dry-run \
  --label-filter UNREAD \
  --max-messages 1 \
  --create-draft
```

This creates a draft only. Approval does not send email, and sending is not implemented.

Current Gmail attachment handling is metadata-only. Keystone records filename, MIME type,
size, attachment-id presence, and risk flags, but it does not download, read, OCR, preview,
render, visualize, summarize, or send attachment bodies to an LLM.

Future attachment review should be a separate, explicit live feature. It must require a narrow
message scope, human approval, MIME allowlists, size limits, malware-risk screening, sandboxed
rendering or extraction, redacted audit logs, and source records for any attachment-derived
claim. Attachment content must not be used in draft replies unless a reviewed workflow records
the source, confidence, reviewer approval, and limitations. Do not process PHI, secrets,
contracts, financial instructions, or patient-specific content through attachment OCR or
visualization without a separate reviewed policy.

## Operator-Controlled Periodic Gmail Triage

There is no active Gmail scheduler in this repo. Periodic triage is a human-run
operator procedure, not a background process. Start with fixture or dry-run
commands, review output, then decide whether any saved queue item needs action.

Safe periodic dry-run command:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --save \
  --markdown
```

Review local results:

```bash
.venv/bin/python scripts/list_approvals.py --status pending
sqlite3 keystone_agents.db \
  "select id, agent_name, status, dry_run, created_at_utc from agent_runs order by id desc limit 10;"
```

Scheduled live Gmail processing is not enabled by default. Do not add cron,
launchd, Task Scheduler, daemon, or background service entries yet. Any future
operator-approved scheduler must:

- Require narrow Gmail labels, never full-inbox polling.
- Require explicit live flags such as `--live-gmail --no-dry-run`.
- Keep small limits such as `--max-messages 1` until reviewed.
- Preserve no-auto-send behavior and no Gmail send implementation.
- Use `--save` so audit rows capture each run and attempted side effect.
- Keep label mutation separate behind `--apply-labels` after a `--preview-labels` review.
- Keep Gmail draft creation separate behind `--create-draft` and human review.
- Route generated drafts through the approval queue before external use.

## Live Slack Approval Notifications

```bash
export SLACK_BOT_TOKEN="<set-locally>"
export SLACK_CHANNEL_APPROVALS="C0123456789"
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --request-approval \
  --live-slack \
  --no-dry-run \
  --markdown
```

Slack notifications do not approve, send, schedule, or publish anything.

For Slack `@KNI` business-agent mode, keep the bridge flags distinct:
`KNI_BUSINESS_AGENTS_SLACK_CONTEXT_ENABLED` routes mentions into local
business-agent workflows; `KNI_BUSINESS_AGENTS_LIVE_SDK` enables model
synthesis/planning; `KNI_BUSINESS_AGENTS_LIVE_SEARCH` enables retrieval;
`KNI_BUSINESS_AGENTS_LIVE_SLACK` posts approval cards; and
`KNI_BUSINESS_AGENTS_LIVE_GMAIL_DRAFTS` creates Gmail drafts only after local
approval. Slack history context stays disabled unless
`KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED` and the reviewed Slack history
scopes are intentionally enabled. See `docs/SLACK_BUSINESS_AGENT_MODE.md`.

## Natural-Language Agent Mentions

Use the packaged CLI to speak to a named agent from the terminal:

```bash
keystone ask @KNI orchestrator agent "route this request"
keystone ask @KNI business research analyst "research Lindus Health"
keystone ask @KNI opportunity scout "find psychiatry AI opportunities"
keystone ask @KNI outreach composer "draft only after approved context"
keystone ask @KNI gmail triage "classify this sanitized email text"
```

From an uninstalled checkout, use:

```bash
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health"
.venv/bin/python -m keystone_agents.cli ask @KNI business research analyst "research Lindus Health"
```

`keystone ask` never sends, posts, schedules, or writes externally. In the
default dry-run environment, calls with no `--agent` run WorkItem mode and save
local SQLite WorkItem/artifact state plus the manual plan. Direct `--agent`
mentions still pass through the Orchestrator-first interpretation path when
live planning/model execution is enabled: the named agent is a requested route,
not a hard bypass. In `live-test` / `full-live` mode, explicit named-agent calls
through `--agent` or `@KNI <agent>` auto-enable live SDK model execution. Use
`--no-live-sdk` to force the dry-run / WorkItem path.

`@KNI keystone ask ...` is accepted as a Slack-friendly alias for the same
natural-language entrypoint. With live SDK enabled, Chief of Staff `ask` runs
use local Agents SDK SQLite sessions by default for conversation continuity.
WorkItem advancement also uses scoped sessions when live SDK is enabled because
the WorkItem id gives a durable boundary. Other direct specialist `ask` runs
stay stateless unless a session is inherited from Slack/WorkItem context or the
operator opts in. Override the logical session with `--sdk-session-id`, store
history somewhere else with `--sdk-session-db`, or disable it with
`--no-sdk-session`. Session ids are hashed before use; WorkItems and SQLite
artifacts remain the canonical audit state.

Use `--live-manual-plan` when you want the LLM planner to interpret a flexible
manual request before execution. The planner and Orchestrator cooperate as the
control plane: planner output is compact guidance for routing and constraints,
while Orchestrator preflight reads the raw request and current context before
specialists run. If the planner cannot run, Keystone falls back to local
structured planning. The provider policy is controlled by
`KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY`:

- `target_with_openai_fallback`: try the target agent provider first, then
  OpenAI Orchestrator fallback.
- `target`: use only the target agent provider.
- `openai`: always use the OpenAI Orchestrator planner provider.

With live SDK enabled, the CLI uses the supported script-backed execution paths
for Business Research Analyst, Opportunity Scout, and Gmail Triage. Outreach
Composer blocks unless approved WorkItem/source context is already available; it
does not use default fixtures for live manual drafting.

The same alias parser accepts Slack app mentions such as:

```text
<@SLACK_BOT_ID> business research analyst research Lindus Health
```

Slack event handling should call `parse_slack_agent_mention(...)` before
dispatching to the same no-send, approval-gated agent workflows.

## Approval Queue

The local `approval_queue` table is the reviewer backlog for draft and research artifacts.
It is stored in SQLite and is safe to use without Slack or Gmail credentials.

List pending items:

```bash
.venv/bin/python scripts/list_approvals.py
```

List another status or print JSON:

```bash
.venv/bin/python scripts/list_approvals.py --status revise
.venv/bin/python scripts/list_approvals.py --status all --json
```

Update a reviewer decision:

```bash
.venv/bin/python scripts/update_approval.py approval_123 approved --notes "Reviewed"
.venv/bin/python scripts/update_approval.py approval_123 rejected --notes "Not a fit"
.venv/bin/python scripts/update_approval.py approval_123 revise --notes "Needs source attribution"
.venv/bin/python scripts/update_approval.py approval_123 archived --notes "Closed"
```

Approving a queue item records local review state only. It does not send email, post copy,
schedule outreach, or publish externally.

## Local Contact And CRM Context

Contact and CRM/account context is local-only. Use JSON fixtures or SQLite records with
`approval_state=approved_for_drafting`; pending or unsupported context is flagged and ignored
for personalization.

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --contact-fixture sample_contact_curebase_approved \
  --crm-context-fixture sample_crm_context_curebase \
  --markdown
```

No HubSpot, Airtable, Google Sheets, or live CRM calls are implemented for this path.

Follow-up schedule records are local recommendations tied to drafts or accounts. They require
human approval and manual execution outside Keystone v1. Do not add cron, Gmail scheduled send,
CRM task creation, or any background worker for follow-ups yet.

## Outreach Templates And Private Example RAG

Canonical outreach templates live in
`src/keystone_agents/templates/outreach/*.json`. They are schema-validated by
`OutreachTemplateRecord` and loaded through `keystone_agents.outreach_templates` or the
`list_outreach_templates` and `load_outreach_template` tools. The current approved template IDs
are `low_pressure_intro` and `research_workflow_intro`.

Templates are repo-versioned, not SQLite-first, because they are reusable drafting policy:
reviewers need normal code review diffs, deterministic tests, stable prompt inputs, and easy
rollback. SQLite can store selected template IDs on drafts and audit rows, but it is not the
canonical source for template language or safety constraints.

Private Gmail examples are captured only into local SQLite with
`scripts/capture_gmail_thread_examples.py`. Capture reads complete threads from a scoped Gmail
label, converts them into sanitized `OutreachExampleDocument` records, stores them in the
`outreach_examples` table and local FTS index, and leaves Gmail read-only. It does not apply
labels, create drafts, send email, archive threads, or write back to Gmail.

Private examples are local-only and must not be committed. They are derived from private mailbox
history and may contain sensitive relationship context even after sanitization. Keep the SQLite
database, OAuth files, exported previews, and any local capture artifacts out of git.

Examples become approved for drafting only when the operator captures from a curated success
label and passes `--approved-for-drafting`. Without that flag, examples remain unavailable for
retrieval. Use the approval flag only after reviewing that the Gmail label contains successful,
non-sensitive outreach threads and that the metadata is appropriate for reuse.

Outreach Composer uses example RAG only when `--use-example-rag` or `--example-query` is passed.
With `--database-url`, it retrieves approved sanitized examples from local SQLite and converts
them into `OutreachExampleGuidance`. Without approved local matches, it falls back to the small
repo-approved deterministic guidance library. Examples guide tone, structure, pacing, CTA, reply
pattern, and follow-up pattern only. They never provide facts about the current prospect.

Never store or send these to the LLM by default: raw Gmail bodies, raw headers, secrets, API keys,
OAuth tokens, PHI, patient-specific information, full private contact details, draft-send
instructions, or unredacted private mailbox content. Retrieval returns only sanitized summaries
and guidance with `raw_body_included=false`.

Run local fixture tests for the template and example path:

```bash
.venv/bin/python -m pytest \
  tests/test_outreach_example_library.py \
  tests/test_outreach_composer.py \
  tests/test_memory.py
```

Capture labeled successful threads for local inspection. Start small and omit approval while
reviewing the sanitized output:

```bash
.venv/bin/python scripts/capture_gmail_thread_examples.py \
  --live-gmail \
  --no-dry-run \
  --label-filter "Keystone/Library/Success" \
  --max-threads 3 \
  --company-type "clinical trial technology" \
  --opportunity-type "clinical AI evaluation" \
  --template-id low_pressure_intro \
  --database-url sqlite:///keystone_agents.db \
  --json
```

Approve examples for drafting only from a reviewed success label:

```bash
.venv/bin/python scripts/capture_gmail_thread_examples.py \
  --live-gmail \
  --no-dry-run \
  --label-filter "Keystone/Library/Success" \
  --max-threads 3 \
  --company-type "clinical trial technology" \
  --opportunity-type "clinical AI evaluation" \
  --template-id low_pressure_intro \
  --approved-for-drafting \
  --database-url sqlite:///keystone_agents.db \
  --json
```

Inspect saved examples:

```bash
sqlite3 keystone_agents.db \
  "select example_id, source_label, approved_for_drafting, raw_body_included, created_at_et from outreach_examples order by id desc limit 10;"
```

Inspect retrieval results:

```bash
.venv/bin/python -c "from keystone_agents.storage.sqlite_store import SQLiteStore; result = SQLiteStore('sqlite:///keystone_agents.db').retrieve_outreach_examples('clinical AI evaluation compare notes', limit=3); print(result.model_dump_json(indent=2))"
```

Run outreach with a selected template:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --template-id low_pressure_intro \
  --markdown
```

Run outreach with approved example RAG:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --use-example-rag \
  --example-query "clinical AI evaluation compare notes" \
  --database-url sqlite:///keystone_agents.db \
  --markdown
```

Run outreach with both template and example guidance:

```bash
.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --template-id research_workflow_intro \
  --use-example-rag \
  --example-query "clinical research workflow context request" \
  --database-url sqlite:///keystone_agents.db \
  --markdown
```

Disable example retrieval by omitting `--use-example-rag` and `--example-query`. If a wrapper
adds an example query automatically, set `--max-examples 0` for that run.

## Optional Email Style Profiles

Email style profiles are opt-in aggregate preference records. They can guide greeting patterns,
signoffs, sentence length, directness, CTA style, formality, formatting, preferred phrases,
avoided phrases, and approved short snippets. They must not contain raw sent-email bodies and
must not enable sending.

Build a pending aggregate profile from fixture sent-mail samples:

```bash
.venv/bin/python scripts/build_email_style_profile.py \
  --fixture sample_sent_email_style_messages \
  --profile-id sent-default \
  --save \
  --json
```

Generated profiles default to `approval_state=pending` and are ignored by Gmail triage and
outreach composer until a human marks the profile `approved_for_drafting` for
`approval_scope=drafting`. The profiler stores hashes, lengths, source IDs, and redacted
sample summaries only. It does not store raw sent bodies, create drafts, apply labels, or send
email. Samples that trigger PHI, secret, security, or professional-advice guardrails are excluded
from profile inference; reviewer output includes sample, usable, and excluded counts plus redacted
subject and body summaries.

Live sent-mail sampling is disabled unless explicitly requested by an operator:

```bash
.venv/bin/python scripts/build_email_style_profile.py \
  --live-gmail \
  --no-dry-run \
  --max-messages 5 \
  --profile-id sent-default \
  --save \
  --json
```

The live profiler samples only Gmail `SENT` messages through the existing Gmail read path. Keep
limits small, review the redacted summaries, then approve the aggregate profile locally before
use. Do not use raw sent bodies, sensitive content, secrets, PHI, or patient-specific material
as prompt context.

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --sender-name Alex \
  --email-style-profile-fixture sample_email_style_profile_approved \
  --markdown

.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --email-style-profile-fixture sample_email_style_profile_approved \
  --markdown
```

Approved profiles saved in SQLite can be used by ID:

```bash
.venv/bin/python scripts/run_gmail_triage.py \
  --fixture tests/fixtures/sample_email_consulting.txt \
  --email-style-profile-id sent-default \
  --markdown

.venv/bin/python scripts/run_outreach_draft.py \
  --fixture sample_company_curebase \
  --opportunity-fixture sample_lead_curebase \
  --email-style-profile-id sent-default \
  --markdown
```

Only profiles approved for drafting are used. Pending or rejected style profiles are ignored or
flagged, and all drafts still require human approval.

## Do Not Enable Yet

- Live email sending or any automatic outbound messaging.
- Autonomous approval.
- Scheduled live Gmail processing, outbound campaigns, or follow-ups.
- Cron, launchd, Task Scheduler, daemon, or background service code.
- CRM, Google Docs, Airtable, or server-backed storage as live write paths.
- Full-inbox Gmail processing without scoped labels.
- Gmail attachment downloading, reading, OCR, rendering, or visualization.
- PHI or patient-specific workflows.
- Secrets in prompts, fixtures, traces, logs, SQLite rows, or Git.

## Rollback

1. Stop live commands and remove live flags.
2. Reset `KEYSTONE_LIVE_MODE=false` and `KEYSTONE_DRY_RUN=true`.
3. Unset affected credentials and rotate or revoke them.
4. Preserve SQLite and terminal output for review.
5. Inspect audit rows for the command, object id, live mode, and attempted side effects.
6. Restore the last copied SQLite database if needed.
7. Add a regression test before re-enabling the path.

## Log And Audit Review

```bash
sqlite3 keystone_agents.db ".tables"
sqlite3 keystone_agents.db \
  "select id, agent_name, status, dry_run, created_at_utc from agent_runs order by id desc limit 10;"
sqlite3 keystone_agents.db \
  "select id, object_type, object_id, decision, scope, reviewer, decision_at_et from approvals order by id desc limit 10;"
sqlite3 keystone_agents.db \
  "select id, company_name, approval_state, created_at_et from outreach_drafts order by id desc limit 10;"
```

Review for unexpected live flags, missing approval records, stale drafts, failed runs, secrets, and PHI.

## Security Checklist

- `.env`, OAuth files, token files, and SQLite files are not committed.
- Credentials live only in local environment variables or local `.env`.
- `KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false`.
- Trace metadata must not contain API keys, tokens, full email bodies, PHI, prompts, tool payloads, or draft bodies.
- Gmail live runs use `--label-filter` and `--max-messages 1` first.
- Scheduled live Gmail processing is disabled by default.
- Drafts remain pending approval.
- Legal, finance, security, contractual, and professional-advice content is escalated.

## Cost-Control Checklist

- Prefer fixtures and dry-run.
- Keep first live search runs at `--max-results 3` and keep hosted web search
  capped.
- Keep first live Gmail runs at `--max-messages 1`.
- Avoid `--sdk` unless intentionally validating SDK construction or live model behavior.
- Save audit rows to avoid repeating live calls.
- Disable unused provider keys.

## Daily Routine

1. Confirm dry-run defaults and no unattended scheduler.
2. Run health check.
3. Process fixtures before any live integration.
4. Review pending approvals before draft creation.
5. Review SQLite audit rows for failures and unexpected live flags.
6. Copy the SQLite database before substantial live review.

## Weekly Routine

1. Run pytest and ruff.
2. Review unresolved approvals and stale drafts.
3. Review audit tables for repeated failures.
4. Validate or rotate live credentials.
5. Review live-search usage and result counts.
6. Update fixtures and tests for any production issue before expanding live scope.
