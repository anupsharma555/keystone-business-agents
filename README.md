# Keystone Business Agents

`keystone-business-agents` is a dry-run-first OpenAI Agents SDK project for Keystone Neuroinformatics LLC business development workflows. It provides specialist agents and deterministic fixture-mode workflows for triaging inbound email, researching companies and broader research targets, scouting opportunities, and drafting approval-gated outreach.

Nothing in this repository sends external email automatically.

## Agents

The project centers on four business specialist agents:

- Gmail Inbound Triage Agent: classifies inbound email, recommends labels, flags safety issues, and creates draft-only response recommendations.
- Business Research Analyst: builds source-attributed briefs for companies, institutes, conferences, labs, topics, Zotero collections, and article collections; legacy company-profile workflows still produce fit and confidence scores.
- Opportunity Scout Agent: finds or updates opportunity records, scores priority, and preserves approval gates before outreach.
- Outreach Composer Agent: drafts email or LinkedIn copy only from approved context and marks all output for human approval.

An Orchestrator Agent is the first model control plane for natural-language
`@KNI`, Slack, WorkItem, scheduled-automation, and explicit named-agent
requests. It reads the raw request plus compact memory/context, produces route
advice and blockers, then passes a memo to the selected specialist; explicit
agent mentions are advisory and do not bypass safety gates. A KNI Chief of
Staff Agent handles broad operational synthesis and Slack/workflow coordination.
The first end-to-end dry-run workflow calls the specialists in sequence without
replacing them. For the two search-heavy routes,
`keystone_agents.workflows.run_orchestrated_search_handoff(...)` carries the
orchestrator's `retrieval_hint` directly into Business Research Analyst or
Opportunity Scout execution.

WorkItem workflows use a typed context-pack layer before specialist execution.
Research, opportunity, outreach, and Gmail context packs are derived from the
WorkItem target, selected artifact refs, approved facts, sources, blockers,
approval gates, and next action. Python readiness gates remain authoritative:
the orchestrator and WorkItem runner can select the pack and recommend routing,
clarification, research, or approval, but they cannot bypass approval, source,
recipient, thread, or no-send gates. Each pack also exposes `can_synthesize`,
`missing_requirements`, and `limitation_notes` so a blocked or partial run says
which context was unavailable instead of filling gaps.

Current architecture visual:
`docs/assets/kni-agent-routing-architecture-orchestrator-first-20260525-181618.svg`.

## Agents SDK Architecture

OpenAI Agents SDK usage is centralized in `keystone_agents.sdk`. Agent builders return SDK Agent objects through the shared `build_sdk_agent()` helper, load markdown prompts, attach tools and guardrails, and use Pydantic output schemas.

Live model execution is intentionally separate from agent construction. Tests can construct agents without `KEYSTONE_OPENAI_API_KEY`; live execution fails clearly if that repo-specific credential is missing. Keystone intentionally ignores generic shell `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL` values so those variables can be used by another project.

The global live model default is `gpt-5.4-mini`. During integration testing,
Gmail Triage, Outreach Composer, Orchestrator, Business Research Analyst,
Opportunity Scout, and Chief of Staff also default to OpenAI `gpt-5.4-mini`.
Gemini remains available as an explicit override or fallback through Google's
direct OpenAI-compatible endpoint. All agent models can still be overridden with
`KEYSTONE_*_MODEL` environment variables, including
`KEYSTONE_GMAIL_TRIAGE_MODEL`, `KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL`, and
`KEYSTONE_CHIEF_OF_STAFF_MODEL`.
`MODEL_PROVIDER` defaults to `openai`;
`KEYSTONE_OPENAI_BASE_URL`, `KEYSTONE_GMAIL_TRIAGE_BASE_URL`,
`KEYSTONE_OUTREACH_COMPOSER_BASE_URL`, and `LITELLM_BASE_URL` are optional
overrides for compatible endpoints, including LiteLLM. Keystone does not require
the Python `litellm` package in its runtime environment.

Gemini model runs include local free-tier request context in test-pack and SDK
JSON reports when pricing metadata is available. Unsaved runs report the current
run only; saved SDK runs also use sanitized SQLite audit rows to report observed
same-day Gemini requests against the documented daily/RPM/TPM limits. This is
operator quota context, not a provider billing statement.

Each SDK agent run also has a centralized cost guard.
`KEYSTONE_AGENT_RUN_BUDGET_USD` defaults to `0.25`; the shared runner estimates
cost from provider usage and the local pricing table, then stops before
persistence or downstream workflow steps if an agent run exceeds that budget.
True live runs also block when cost cannot be verified.

## Package

Use `keystone_agents` for all library imports. It is the only supported package
namespace for agent builders, prompts, schemas, tools, storage, and workflows.

## Reference Repositories

These public repositories were used as architectural references only:

- `DanielJD1216/Email-Inbox-Agent---Doo-Made` for Gmail triage, labels, OAuth boundaries, and draft-only behavior.
- `DimiMikadze/mira` for multi-source company research, confidence scoring, structured profiles, and source attribution.
- `harshalsp0011/Lead-Intelligence-Platform` for pipeline persistence, scores, approvals, drafts, audit flow, and human review gates.
- `kaymen99/sales-outreach-automation-langgraph` for lead research, qualification, CRM context, and personalized outreach workflow patterns.

No reference code is copied into this repository.

## Setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

Optional live-model configuration can be copied from `.env.example`. Dry-run scripts and tests do not need an API key.

```bash
cp .env.example .env
```

For OpenAI dashboard setup, project-scoped key handling, tracing, trace grading,
datasets/evals, Agent Builder exploration, and future model migration, see
`docs/OPENAI_AGENT_PLATFORM_SETUP.md`.
For the local folder layout mapped to Agents SDK concepts, see
`docs/AGENTS_SDK_CONFORMANCE.md`.
For documentation navigation and extension guides, start with `docs/INDEX.md`.
For the `@KNI` Slack bridge configuration, Slack scope requirements, and
approval boundaries, see `docs/SLACK_BUSINESS_AGENT_MODE.md`.

The default database is local SQLite:

```bash
.venv/bin/python scripts/init_db.py
```

`DATABASE_URL` may be set to a SQLite URL such as `sqlite:///keystone_agents.db` or `sqlite:////tmp/keystone_agents.db`.
For new local workspaces, prefer `KEYSTONE_HOME=.keystone`; when `DATABASE_URL`
is unset, Keystone then stores SQLite state at
`.keystone/state/keystone_agents.db`.

Stored audit rows include canonical UTC timestamps plus derived America/New_York
timestamps and ET calendar dates for local business-day filtering.

## Deployment And Operations

This repo is operated local-first by default. Use `docs/DEPLOYMENT.md` for setup,
health checks, dry-run commands, live search/Gmail/Slack enablement, rollback,
audit review, security checks, cost controls, and daily or weekly routines.
Use `docs/RUNBOOK.md` as the shorter operator checklist.

Use the operator mode switcher to move between dry-run, live-test, and
full-live without hand-editing the repo `.env`:

```bash
python3 scripts/switch_operator_mode.py dry-run
python3 scripts/switch_operator_mode.py live-test
python3 scripts/switch_operator_mode.py full-live
```

The intended retrieval ladder for live research is:

- `SearXNG` for broad recall when no explicit provider override is set.
- `Agents hosted web search` as a capped parallel lane beside SearXNG for
  default live research. The default cap is 2 hosted web-search requests per
  run through `KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN`.
- `Serper` only when `SEARCH_PROVIDER=serper` or `--search-provider serper` is
  explicitly selected.
- `Firecrawl` as an explicit `SearchProvider` option when configured, and as an
  optional website extraction provider when `KEYSTONE_WEBSITE_EXTRACTOR=firecrawl`.
- `Trafilatura` as the default live-gated website extraction provider for
  selected company pages.
- `Opportunity Scout` uses a pre-retrieval structured search plan, then runs
  bounded multi-lane search. Manual Slack/CLI calls can opt into a live LLM
  planner before retrieval; if that planner is unavailable, Scout falls back to
  the local structured planner. Broad prompts can search across company growth,
  collaboration, researcher, institute, conference, grant, trial, and role
  lanes. When a run under-fills, Scout can run bounded adaptive follow-up
  queries and SearXNG-compatible result-page deepening.
- Manual and opportunity planners structure retrieval intent, desired count, and
  safety constraints. They do not select providers; Business Research Analyst,
  Opportunity Scout, Orchestrator handoffs, and Chief of Staff delegated
  research use the shared retrieval policy above.

For local live search, this repo owns a separate SearXNG runtime from
`keystone-slack`:

```bash
./scripts/manage_searxng_headless.sh start
export SEARCH_PROVIDER=searxng
export SEARXNG_BASE_URL=http://127.0.0.1:18080
export KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK=true
export KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL=true
export KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN=2
export KEYSTONE_AGENT_HTML_REVIEW=true
export KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES=2
```

The runtime uses Colima profile `kba-searxng`, Docker context
`colima-kba-searxng`, and host port `18080`. The Slack repo can keep its own
`kni-searxng` profile on port `8080` without sharing lifecycle state.

  `KEYSTONE_OPPORTUNITY_FOLLOWUP_RESULT_CAP` controls follow-up results per
  query and is capped at 8.
- Manual Orchestrator and Business Research Analyst calls can use the shared
  `ManualRequestPlan` layer before execution. It extracts target agent, target,
  objective, desired count, and safety constraints while keeping outreach
  approval-gated and all side effects disabled. Set
  `KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY` to `target_with_openai_fallback`,
  `target`, or `openai` to control planner model routing.
- WorkItem-backed `keystone ask` runs retain the manual plan in the WorkItem
  target metadata, event audit metadata, and JSON result. Planned desired counts
  can raise WorkItem retrieval breadth up to the existing runner cap.
- `Apify` or `Browserless` only as future structured-enrichment candidates;
  current live operations are placeholders and do not execute.
- `Sandbox` only as a later second-pass review over staged artifacts.

In `live-test`, Opportunity Scout and Researcher/company-research paths default
to live search, Gmail Triage defaults to live Gmail read paths, explicit
`keystone ask` / `@KNI` specialist calls default to live SDK model execution,
GT-1 priority grouping auto-promotes to `--live-sdk`, and Outreach Composer
auto-promotes to `--live-sdk`. Draft-only, approval-gated, and no-send rules
still apply.

## Dry-Run Commands

All commands below use fixtures or local deterministic logic by default:

```bash
.venv/bin/python scripts/run_gmail_triage.py --fixture tests/fixtures/sample_email_consulting.txt
.venv/bin/python scripts/run_company_research.py --company Curebase --fixture tests/fixtures/sample_company_curebase.json
.venv/bin/python scripts/run_opportunity_scout.py --topic "behavioral health AI"
.venv/bin/python scripts/run_outreach_draft.py --fixture sample_company_curebase --opportunity-fixture sample_lead_curebase
.venv/bin/python scripts/run_orchestrator.py --input "find 5 behavioral health AI companies"
.venv/bin/python scripts/run_keystone_pipeline.py --email-fixture tests/fixtures/sample_email_consulting.txt --company-fixture tests/fixtures/sample_company_curebase.json --markdown
```

The packaged CLI provides a smaller navigation layer while preserving the scripts:

```bash
keystone health
keystone init-db
keystone agents list
keystone route --input "find 5 behavioral health AI companies"
keystone ask @KNI orchestrator agent "find 5 behavioral health AI companies"
keystone ask @KNI business research analyst "summarize the Zotero cache on depression"
keystone ask --agent opportunity_scout "find psychiatry AI opportunities"
keystone evals --agent all
```

From an uninstalled checkout, use the wrapper:

```bash
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health"
```

If the console script is unavailable in a local checkout, use the module
entrypoint for the same CLI:

```bash
.venv/bin/python -m keystone_agents.cli agents list
```

`keystone ask` never sends, posts, schedules, or writes externally. In the
default dry-run environment, calls without `--agent` use WorkItem mode and save
local SQLite WorkItem/artifact state plus the `ManualRequestPlan`; direct
`--agent` calls resolve the named specialist without model execution. In
`live-test` / `full-live` mode, explicit named-agent calls through `--agent` or
`@KNI <agent>` auto-enable live SDK model execution and still use
Orchestrator-first interpretation before specialist execution unless
`--no-live-sdk` is passed. Direct live manual calls use the reviewed
script-backed paths for Business Research Analyst, Opportunity Scout, Gmail
Triage, and Chief of Staff; Outreach Composer blocks until approved
WorkItem/source context is available.

WorkItem mode provides the stateful natural-language workflow path:

```bash
.venv/bin/python -m keystone_agents.cli work-items advance \
  --input "research NeuroFlow" \
  --database-url sqlite:////tmp/keystone_agents.db
.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "continue" \
  --database-url sqlite:////tmp/keystone_agents.db
.venv/bin/python -m keystone_agents.cli work-items approve-context <work_item_id> \
  --artifact company_profile:1 \
  --database-url sqlite:////tmp/keystone_agents.db
```

Each advance records the selected context pack, readiness-gate summary, missing
requirements, and limitation notes in the WorkItem timeline/result. Outreach
remains draft-only and normally stops at
`needs_approval` after an outreach draft artifact is created.

Use `--save` on CLI scripts to write local SQLite audit records. No storage write occurs unless `--save` is passed.

## Secret Hygiene Scan

Run the repo-wide tracked-file secret scan locally:

```bash
.venv/bin/python scripts/scan_repo_secrets.py
.venv/bin/python scripts/scan_repo_secrets.py --json
```

The scanner checks git-tracked text files for high-signal credential patterns
(OpenAI keys, GitHub PATs, Slack tokens, AWS access key IDs, Google key/token
shapes, and private key headers). It exits non-zero when a likely secret is
detected. CI also enforces this via `tests/test_architecture.py::test_no_obvious_repo_secrets_present`.

## Tests

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m pip_audit --skip-editable --progress-spinner off
```

Tests use fixtures and mocks only. They do not require OpenAI, Gmail, Slack,
SearXNG, hosted web search, Serper, Firecrawl, Apify, Browserless, or other
live API keys.

Use `.venv/bin/python scripts/check_quality.py` for the full local gate with
coverage, ruff, and dependency audit.

## Safety Model

The safety model is enforced through prompts, schemas, guardrails, tool boundaries, and tests:

- Dry-run mode is the default.
- Live integrations require explicit CLI flags and credentials.
- Gmail live mode is limited to reading, labeling, and draft creation behind `--live-gmail --no-dry-run`.
- Slack approval notifications require `--request-approval --live-slack --no-dry-run`.
- Slack `@KNI` business-agent mode keeps Slack context, background thread
  results, approval-card posting, message actions, Slack history access, live
  model execution, live search, and Gmail draft creation as separate flags.
- Live search requires `--live-search --no-dry-run`.
- PHI and patient-specific content are blocked.
- Medical, legal, tax, and regulatory advice are blocked.
- Unsupported Keystone prior-experience or outcome claims are flagged.
- Outbound copy rejects em dashes.
- Drafts require human approval.
- Storage redacts secret-like values and avoids storing full sensitive email bodies by default.
- Inbound email bodies and triage draft replies are stored as hash plus short redacted summary only.
- Outreach draft email bodies are stored after redaction because they are the local human-approval artifact; no draft may be sent or handed to a live integration without approval.

## No Auto-Send Policy

Outbound communication is draft-only. The codebase does not expose an implemented Gmail send tool, SendGrid send path, scheduler-based outbound flow, or automatic external messaging workflow. All generated outreach remains pending human approval.

## Current Status

Implemented:

- Shared OpenAI Agents SDK helper layer and model-provider configuration.
- Four business specialist SDK agent builders, plus Orchestrator and KNI Chief
  of Staff agent builders.
- Shared `skills.md` and `tools.md` prompt contracts that document agent capabilities,
  current tools, future tool gaps, and safe tool-addition rules.
- Guarded local helper tools for approved contact/CRM context lookup, approval queue
  inspection, and draft-only approval item creation.
- Typed SDK runtime harnesses for Gmail triage, business research, opportunity scouting, and outreach composition. Fake/local run configs work without API keys; live SDK execution is credential-gated.
- Canonical `AgentSpec` registry and agent cards for registered builder,
  prompt, tool, schema, live-flag, eval, validation, handoff, and safety
  metadata.
- Orchestrator Agent with intended handoff metadata and deterministic routing.
- KNI Chief of Staff Agent for Slack operations routing, automation inventory
  review, and bounded internal operating-layer writes.
- Deterministic dry-run fixture wrappers for triage, company research, opportunity scouting, and outreach drafting.
- Typed WorkItem context packs and readiness gates for research, opportunity,
  outreach, and Gmail paths, with timeline metadata for pack selection and gate
  status.
- End-to-end dry-run Keystone workflow with approval-gated markdown report.
- Conservative automation controller for staged weekly opportunity runs and
  scoped Gmail preview/apply/draft stages, with health preflight, lock file,
  bounded counts, pending-approval checks, redacted errors, and no-send summary.
- Top 3 opportunity-to-outreach dry-run loop across company, conference,
  journal call, contract/RFP, grant, trial, researcher, and institute lanes,
  with source-backed contact-path fallback and provider-performance telemetry.
- SQLite storage and audit logging for agent runs, emails, companies, opportunities, outreach drafts, manual outreach tracking, approvals, approval queue items, feedback, sources, and tool events.
- SQLite schema migration tracking for local storage evolution.
- Explicit live Gmail read, label, and draft-only operations.
- Explicit live search for Researcher/company research and opportunity scouting
  through `SearchProvider`. Default live research uses SearXNG plus capped
  hosted web search; Serper and Firecrawl remain explicit configured options.
- Live-gated website extraction for selected company pages through Trafilatura
  by default or Firecrawl when explicitly configured, with optional fallback
  between those two extractors.
- Optional capped Agents SDK HTML/text review for weak deterministic extraction,
  exposed to Business Research Analyst, Opportunity Scout, Chief of Staff, and
  Orchestrator as `extract_research_claims_from_html`.
- Explicit live Slack approval notifications.
- GitHub Actions CI for Python 3.11, ruff, pytest, and dependency audit.

Not implemented:

- Live email sending.
- Autonomous approval.
- Live production pipeline execution outside the narrow explicit integrations above.
- Live Apify, Browserless, Airtable, Google Sheets, CRM, or Google Docs writes
  as automatic production side effects. Chief of Staff exposes dry-run/internal
  review publishing surfaces; live providers must be added as reviewed adapters.
- LangGraph orchestration.
- Server-backed storage.
