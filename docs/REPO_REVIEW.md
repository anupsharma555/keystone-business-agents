# Keystone Business Agents Repository Review

Review date: 2026-04-21

This is a documentation-only review of the current implemented state. It does not add
features or imply live capabilities beyond the code paths described here.

## Summary

`keystone-business-agents` is a Python 3.11+ OpenAI Agents SDK project for dry-run-first
Keystone business development workflows. It implements four specialist agent builders,
typed SDK runtime harnesses, deterministic fixture wrappers, local SQLite persistence,
approval gates, source attribution, and narrow opt-in live integrations.

The standing safety contract is unchanged: no auto-send, draft-only outbound workflows,
human approval required, no PHI, no secrets in logs or fixtures, and live integrations only
behind explicit flags and credentials.

Current verification from this implementation pass:

- `.venv/bin/python -m pytest`: 685 passed.
- `.venv/bin/python -m ruff check .`: all checks passed.

## Current Architecture

- SDK layer: `src/keystone_agents/sdk.py` centralizes OpenAI Agents SDK imports, agent
  construction, prompt metadata, tool wrapping, guardrail wrapping, local/fake run config
  support, and credential-gated live SDK execution.
- Runtime layer: `src/keystone_agents/run.py` provides typed SDK execution helpers that
  convert typed inputs into prompts and validate Pydantic final outputs.
- Model layer: `model_provider.py` and `config.py` define provider settings, OpenAI or
  compatible base URL handling, tracing settings, and live credential validation.
- Agent layer: `src/keystone_agents/agents/*.py` defines the four specialist builders,
  orchestrator builder, deterministic fixture helpers, and typed `run_*_sdk(...)` helpers.
- Schema layer: `src/keystone_agents/schemas/*.py` defines Pydantic outputs, source
  records, approval states, contact context, feedback, and table mirror objects.
- Tool layer: `src/keystone_agents/tools/*.py` owns Gmail, Slack,
  SearchProvider, website extraction, storage, approval, Apify, Browserless,
  and web-scrape boundaries.
- Storage layer: `src/keystone_agents/storage/sqlite_store.py` persists local audit and
  review state.
- CLI layer: `scripts/*.py` exposes fixture workflows, health checks, evals, approval
  administration, feedback, and table exports.
- Test layer: `tests/*.py` validates SDK construction and fake execution, live safety
  gates, schemas, guardrails, fixtures, mocked integrations, storage, evals, and reporting.

`keystone_agents` is the only supported package namespace.

## Implemented Specialist Agents

Gmail Inbound Triage:

- `build_gmail_triage_agent()` returns an SDK agent-like object with `EmailTriageResult`
  output.
- Fixture mode classifies email, labels, risk flags, draft recommendations, and approval
  state.
- Live Gmail can list recent messages, fetch a message, apply labels, and create a reply
  draft when explicitly enabled.
- `send_email` is intentionally not implemented.

Business Research Analyst:

- `build_business_research_analyst_agent()` returns an SDK agent-like object with `CompanyProfile`
  output for legacy company-profile workflows; `build_business_research_analyst_research_brief_agent()` returns
  `ResearchBrief` for broader research.
- Fixture mode produces source-attributed company profiles, claim evidence, fit scores,
  confidence, risks, and missing information.
- Optional live search can gather source records through SearXNG, Serper, or
  Firecrawl.
- Broader research can summarize institutes, conferences, topics, Zotero collections,
  and article collections from approved source context.

Opportunity Scout:

- `build_opportunity_scout_agent()` returns an SDK agent-like object with
  `OpportunityScoutResult` output.
- Fixture mode ranks source-backed opportunity records with component score breakdowns and
  Business Research Analyst handoff recommendations.
- Optional live search can collect opportunity source records through SearXNG,
  Serper, or Firecrawl.
- Outreach drafting is deliberately not part of the scout flow.

Outreach Composer:

- `build_outreach_composer_agent()` returns an SDK agent-like object with `OutreachDraft`
  output.
- Fixture mode drafts concise email and LinkedIn copy from approved source-backed context.
- Unsupported claims are flagged and excluded.
- Output is always approval-gated and send-disabled.

Orchestrator:

- `build_orchestrator_agent()` returns an SDK agent-like object with deterministic routing
  helpers.
- The current CLI route is dry-run only and does not replace the specialist workflows.

## Typed SDK Runtime Status

Implemented:

- Typed inputs in `src/keystone_agents/models.py`.
- `TypedAgentRunResult`.
- `run_typed_sdk_agent(...)` and `run_typed_sdk_sync(...)`.
- Specialist helpers: `run_gmail_triage_sdk`, `run_business_research_analyst_sdk`,
  `run_opportunity_scout_sdk`, and `run_outreach_composer_sdk`.
- Fake/local model execution in tests without API keys.
- Missing `KEYSTONE_OPENAI_API_KEY` fails only when live SDK execution is requested.

Live SDK execution remains opt-in and credential-gated. Agent construction remains testable
without credentials.

## Live Integration Status

Implemented live opt-in paths:

- Gmail read, message retrieval, label application, and draft reply creation.
- Slack approval notifications.
- SearXNG, Serper, and Firecrawl search through `SearchProvider`.
- Live-gated website extraction for selected company pages through Trafilatura
  or Firecrawl.

Live paths require both explicit CLI flags and configuration. The default test suite makes no
network calls and requires no live credentials.

Fixture-only or placeholder paths:

- Apify actor execution.
- Browserless rendering.
- Generic live web scraping.
- Airtable and Google Sheets table mirroring.
- CRM writes and Google Docs writes.

Not implemented:

- Gmail send or any external email sending.
- Autonomous approval.
- Scheduled outbound campaigns.
- Server-backed storage.
- LangGraph orchestration.

## Storage And Audit Status

SQLite is the implemented local system of record. Current schema version is tracked through
`schema_migrations` and covers:

- `agent_runs`
- `emails`
- `companies`
- `opportunities`
- `outreach_drafts`
- `approvals`
- `approval_queue`
- `sources`
- `feedback`
- `tool_events`

Storage redacts secret-like values and stores body-like sensitive fields as hashes and
summaries where appropriate. Outreach draft bodies are retained as approval artifacts after
redaction. Tool events capture redacted summaries, hashes, dry-run state, status, errors, and
timestamps.

## Safety Status

Safety is enforced through prompts, schemas, guardrails, CLI checks, storage redaction, and
tests:

- Dry-run mode is default.
- Live paths require explicit flags and credentials.
- No external email send path is implemented.
- Drafts are approval-gated.
- PHI and patient-specific workflows are blocked.
- Legal, financial, security, contractual, and professional-advice content is escalated.
- Company and opportunity claims require source attribution.
- Unsupported Keystone prior-experience and outcome claims are flagged.
- Outbound copy enforces length limits and no em dashes.

## Evals And Quality

The repo has two local eval surfaces:

- `scripts/run_evals.py`: static JSON evals in `tests/evals/` for the four specialist agents.
- `scripts/run_local_evals.py`: JSONL seed evals in `evals/` with prompt version traceability.

Both are deterministic and offline. They do not call model APIs, Gmail, Slack, Serper, Apify,
Browserless, or other live services.

## Remaining Gaps

- Slack slash commands and scheduled jobs are planned, not implemented.
- Live Gmail draft creation is available, but automatic creation after approval is not wired as
  a full end-to-end Slack workflow.
- Apify and Browserless live operations are placeholders.
- Table mirror live providers for Airtable and Google Sheets are placeholders.
- SQLite is local only; there is no hosted database, backup automation, or encryption-at-rest
  implementation.
- Research and scoring remain heuristic and source-driven rather than fully model-mediated.
- LangGraph is documented for later durable orchestration but not implemented.

## Recommended Next Work

1. Keep docs, prompts, and eval metadata synchronized with implemented behavior.
2. Harden the approval queue around reviewer ownership, expiry, and revision workflows.
3. Add operational backup and retention guidance for local SQLite.
4. Add Slack command handlers only after the SQLite approval model is stable.
5. Expand source-backed research quality before adding more live providers.
6. Defer LangGraph until the SDK agent contracts and audit layer remain stable under real use.

## Status Table

| Area | Status | Notes |
| --- | --- | --- |
| Python baseline | Implemented | Python 3.11+ required; local verification used Python 3.13. |
| SDK builders | Implemented | Four specialists plus orchestrator. |
| Typed SDK runtime | Implemented | Fake/local tests and live credential gate. |
| Fixture workflows | Implemented | Default path for all specialists and pipeline. |
| Gmail live | Live opt-in | Read, label, draft-only. No send. |
| Slack live | Live opt-in | Approval notifications only. |
| Search live | Live opt-in | SearXNG, Serper, and Firecrawl through `SearchProvider`. |
| Website extraction | Live opt-in | Selected company pages through Trafilatura or Firecrawl. |
| SQLite audit | Implemented | Agent runs, approvals, queue, feedback, sources, tool events. |
| Approval queue | Implemented locally | Approval does not send or publish. |
| Table mirror | Fixture/SQLite implemented | Airtable and Google Sheets live writes are not implemented. |
| Apify/Browserless | Placeholder | Dry-run or `NotImplementedError` for live. |
| LangGraph | Planned | Not implemented. |
