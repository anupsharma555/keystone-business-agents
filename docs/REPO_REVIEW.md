# Keystone Business Agents Repository Review

Review date: 2026-07-25

Architecture snapshot: canonical-ownership tranche through `74fa4c66`. See
`docs/ARCHITECTURE_REORGANIZATION.md` for its immutable baseline, migration
status, compatibility boundary, and offline evidence.

This is a documentation-only review of the current implemented state. It does
not add features, run live agents, or imply live capabilities beyond the code
paths described here.

## Summary

`keystone-business-agents` is a Python 3.11+ OpenAI Agents SDK project for
dry-run-first Keystone business development workflows. It implements four
business specialist agents, an Orchestrator Agent, a read-only KNI Chief of
Staff Agent, typed SDK runtime harnesses, deterministic fixture wrappers,
WorkItem orchestration, typed context packs, local SQLite persistence, approval
gates, source attribution, and narrow opt-in live integrations.

The standing safety contract is unchanged: no auto-send, draft-only outbound
workflows, human approval required, no PHI, no secrets in logs or fixtures, and
live integrations only behind explicit flags and credentials.

Current review update:

- The 2026-07-25 architecture tranche established canonical implementation
  packages behind supported top-level compatibility facades. It did not change
  the agent graph, public schemas, provider boundaries, or operational command
  surface.
- The repo is now closer to a production Agents SDK integration than a plain
  scripting project. The main review concern is runtime precision and efficiency
  across Slack, CLI, WorkItem, and direct SDK paths.
- Recent architecture work added or documented quality budgets, tool tiers,
  compact runtime instruction profiles, repo-local skill bundles, source-aware
  final synthesis, richer retrieval diagnostics, and diverse-ask handling gaps.
- This pass did not rerun the full test, lint, or eval suite. The older
  2026-05-11 stabilization record remains useful historical evidence, but it
  should not be treated as current verification for this worktree.

## Current Architecture

- SDK layer: `src/keystone_agents/sdk.py` centralizes OpenAI Agents SDK imports, agent
  construction, prompt metadata, tool wrapping, guardrail wrapping, local/fake run config
  support, prompt-cache metadata, usage capture, sessions, and credential-gated live SDK execution.
- Runtime layer: `src/keystone_agents/run.py` provides typed SDK execution helpers that
  convert typed inputs into prompts and validate Pydantic final outputs.
- Request composition: `src/keystone_agents/runtime/request.py` owns
  request-scoped store and session-service composition.
- Model layer: `model_provider.py` and `config.py` define provider settings, OpenAI or
  compatible base URL handling, tracing settings, and live credential validation.
- Capability admission: `src/keystone_agents/capabilities/profile.py` owns the
  compiled capability profile used to bound model, tool, retrieval, and write
  access.
- Registry layer: `agent_registry.py` is the canonical agent card source for
  builders, schemas, prompts, tools, live flags, eval paths, handoff
  descriptions, and safety notes.
- Agent layer: `src/keystone_agents/agents/*.py` defines the four business specialist
  builders, Orchestrator builder, Chief of Staff builder, deterministic fixture helpers,
  and typed `run_*_sdk(...)` helpers.
- Schema layer: `src/keystone_agents/schemas/*.py` defines Pydantic outputs, source
  records, approval states, contact context, feedback, WorkItems, context packs,
  manual request plans, and table mirror objects.
- Semantic authority: `src/keystone_agents/authority/semantic.py` owns
  canonical execution intent and semantic reconciliation.
- Planning compatibility:
  `src/keystone_agents/planning/compatibility.py` owns bounded compatibility
  planning; `src/keystone_agents/manual_request.py` remains its supported
  public facade.
- Tool layer: `src/keystone_agents/tools/*.py` owns Gmail, Slack,
  SearchProvider, website extraction, storage, approval, Apify, Browserless,
  and web-scrape boundaries.
- Tool policy layer: `agent_tool_policy.py` classifies tools into tiers such as
  `core_read`, `web_search`, `deep_retrieval`, `diagnostic`, `internal_write`,
  and `publish`.
- Skill layer: `skills/*/SKILL.md` holds reusable reasoning and output
  contracts selected per agent/request, replacing dependence on one monolithic
  skills prompt for registered agents.
- Receipt and recovery layer: `src/keystone_agents/receipts/` owns mutation
  classification, receipt journaling and normalization, idempotency, and
  provider recovery behind the supported legacy imports.
- Workflow layer: `src/keystone_agents/orchestration/stages.py` exposes the
  public executable-stage boundary. `workflow_runner.py`, `work_items.py`, and
  context-pack builders still own WorkItem execution, Orchestrator preflight,
  deterministic gates, specialist execution, review, and artifact creation.
- Optional graph layer: `src/keystone_agents/langgraph_workflow.py` remains the
  LangGraph execution backend over the same WorkItem and public-stage
  contracts.
- Presentation layer: `src/keystone_agents/presentation/` owns public-result
  assembly, terminal consistency, and renderers behind supported legacy
  imports.
- Storage layer: `src/keystone_agents/storage/sqlite_store.py` persists local audit and
  review state.
- CLI layer: `src/keystone_agents/cli.py` is the supported lightweight public
  facade; `src/keystone_agents/entrypoints/cli_impl.py` owns the command
  implementation. `scripts/*.py` exposes fixture workflows, health checks,
  evals, approval administration, feedback, and table exports.
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
- Optional live search can gather source records through SearXNG plus capped
  hosted web search, with Exa, Tavily, Firecrawl, and other provider lanes
  available through the shared retrieval policy when explicitly configured.
- Broader research can summarize institutes, conferences, topics, Zotero collections,
  and article collections from approved source context.
- Current review focus: propagate quality budgets consistently, preserve
  requested output shape, avoid company-profile assumptions for source/literature
  asks, and keep source triage typed before final synthesis.

Opportunity Scout:

- `build_opportunity_scout_agent()` returns an SDK agent-like object with
  `OpportunityScoutResult` output.
- Fixture mode ranks source-backed opportunity records with component score breakdowns and
  Business Research Analyst handoff recommendations.
- Optional live search can collect opportunity source records through SearXNG
  plus capped hosted web search, with deepening lanes available through shared
  retrieval policy and route-aware quality budgets.
- Outreach drafting is deliberately not part of the scout flow.
- Current review focus: distinguish exact matches from adjacent leads, preserve
  hard filters and zero-result answers, and require primary evidence for active
  formal opportunities such as roles, grants, RFPs, pilots, or trials.

Outreach Composer:

- `build_outreach_composer_agent()` returns an SDK agent-like object with `OutreachDraft`
  output.
- Fixture mode drafts concise email and LinkedIn copy from approved source-backed context.
- Unsupported claims are flagged and excluded.
- Output is always approval-gated and send-disabled.

Orchestrator:

- `build_orchestrator_agent()` returns an SDK agent-like object with deterministic routing
  helpers.
- It is the first LLM control plane for natural-language `@KNI`, Slack,
  WorkItem, scheduled automation, and explicit named-agent requests.
- It should preserve raw operator wording, attach preflight context, let Python
  enforce deterministic gates, and pass compact specialist briefs rather than
  replacing specialist workflows.
- Current review focus: capture ask-shape dimensions such as evidence depth,
  strict filters, output form, prior-context dependency, permission state, cost
  mode, and stop condition without adding a broad intent taxonomy.

Chief of Staff:

- `build_chief_of_staff_agent()` returns an SDK agent-like object with
  `ChiefOfStaffResult` output.
- Deterministic mode plans read-only KNI Slack operations routing across calendar,
  Gmail, business-agent, and Slack runtime workflows.
- The agent cannot post to Slack, send Gmail, write calendar events, or write repositories.
- Current review focus: keep operational architecture/review asks from being
  over-routed into company or opportunity research when they contain generic
  words such as `review`, `research`, or `summarize`.

## Recent Architecture Changes

- Quality budgets now exist for search-heavy Business Research Analyst and
  Opportunity Scout paths. They map fast, balanced, and deep modes to model
  settings, max turns, retrieval result counts, hosted-search caps, page
  verification, repair, contact enrichment, and reuse behavior.
- Tool tiers are explicit. Search-heavy builders can attach narrower tool
  surfaces for lower-cost or safer runs, and the remaining risk is inconsistent
  tier propagation across every entrypoint.
- Runtime instructions are more compact. `compose_instructions()` now has a
  compact repo guide profile for normal agent execution, with the full guide
  preserved as an escape hatch.
- Repo-local `SKILL.md` bundles are now the preferred reusable reasoning
  surface. Selection is still partly Python-trigger driven, so skill metadata
  should eventually own trigger phrases and default route applicability.
- User-facing final synthesis is more source-aware. The synthesis input can
  include ordered sources, extracted source summaries, provider-result samples,
  source-context notes, and source-triage notes.
- Retrieval diagnostics are richer. Search-heavy paths track provider results,
  search queries, source-focus metadata, source coverage, source lanes, and
  quality-assessment reasons.
- Diverse ask handling remains the highest-leverage design area. The next
  durable improvement is to preserve ask shape and stop conditions as structured
  fields, then make source triage a typed contract.

## Typed SDK Runtime Status

Implemented:

- Typed inputs in `src/keystone_agents/models.py`.
- `TypedAgentRunResult`.
- `run_typed_sdk_agent(...)` and `run_typed_sdk_sync(...)`.
- Specialist helpers: `run_gmail_triage_sdk`, `run_business_research_analyst_sdk`,
  `run_opportunity_scout_sdk`, `run_outreach_composer_sdk`, and
  `run_chief_of_staff_sdk`.
- Fake/local model execution in tests without API keys.
- Missing `KEYSTONE_OPENAI_API_KEY` fails only when live SDK execution is requested.
- SDK usage and local cost/caching metadata are captured where available.
- Scoped SDK sessions support continuity for selected live WorkItem and Chief
  of Staff follow-up paths.

Live SDK execution remains opt-in and credential-gated. Agent construction remains testable
without credentials.

## Live Integration Status

Implemented live opt-in paths:

- Gmail read, message retrieval, label application, and draft reply creation.
- Slack approval notifications.
- SearXNG, hosted web search, explicit Serper, Firecrawl, and Tavily search through `SearchProvider`.
- Live-gated website extraction for selected company pages through Trafilatura
  or Firecrawl.
- Optional capped deepening/search lanes through the shared retrieval policy
  when configured and explicitly live-enabled.

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
- LangGraph replacing the SDK agent layer.

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
- `agent_run_logs`
- `contacts`
- `crm_contexts`
- `follow_up_schedules`
- `outreach_tracking`
- `email_style_profiles`
- `memory_items`
- `memory_index`
- `outreach_examples`
- `work_items`
- `work_item_events`
- `work_item_artifacts`

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

The repo has several local quality surfaces:

- `scripts/run_evals.py`: static JSON evals in `evals/static/` for the four business
  specialist agents.
- `scripts/run_local_evals.py`: JSONL seed evals in `evals/local/` with prompt version traceability.
- `docs/AGENT_IMPROVEMENT_TEST_PACK.md`: standing scenario inventory for
  diverse and deterministic asks across Orchestrator, Chief of Staff, Gmail
  Triage, Business Research Analyst, Opportunity Scout, and Outreach Composer.
- Prompt/skill contract tests cover selected shared and specialist skill
  behavior.

The executable eval surfaces are deterministic and offline. They do not call
model APIs, Gmail, Slack, SearXNG, hosted web search, Serper, Apify,
Browserless, or other live services.

## Remaining Gaps

- Quality budgets and tool tiers exist, but every CLI, Slack, WorkItem, direct
  SDK, manager-loop, and final-synthesis path still needs consistent propagation.
- Ask-shape fields are not yet first-class. The manual plan captures target,
  intent, count, constraints, and side-effect policy, but not enough evidence
  depth, source type, strictness, output-form, prior-context, cost, or stop
  condition detail.
- Source triage exists as a skill and context concept, but not yet as a strongly
  typed retained/review/rejected/deepen contract enforced before final
  synthesis.
- Formal opportunity answers still need hard gates for active status,
  eligibility, deadline, geography, official-source evidence, and adjacent-vs-
  rejected candidate separation.
- Live Gmail draft creation is available, but automatic creation after approval
  is not wired as a full end-to-end Slack workflow.
- Apify and Browserless live operations are placeholders.
- Table mirror live providers for Airtable and Google Sheets are placeholders.
- SQLite is local only; there is no hosted database, backup automation, or encryption-at-rest
  implementation.
- Research and scoring remain hybrid heuristic/source-driven rather than fully
  model-mediated.
- LangGraph is implemented as optional WorkItem graph execution. It remains outside
  core dependencies and does not replace SDK agent contracts, WorkItems, or context packs.

## Recommended Next Work

1. Add ask-shape fields to the manual/orchestrator plan and specialist briefs:
   evidence depth, source preference, strict-filter mode, output form,
   prior-context dependency, permission state, cost mode, and stop condition.
2. Promote source triage to a typed contract with retained, review-only,
   rejected, deepen, and rejection-reason fields.
3. Propagate quality budgets and tool tiers consistently through CLI, Slack,
   WorkItem, direct SDK, manager-loop, and final synthesis paths.
4. Add hard gates for active formal-opportunity claims and zero-result
   exact-match searches.
5. Expand prompt-size and selected-skill budget checks so instruction growth is
   visible before it becomes latency and cost drift.
6. Turn representative no-side-effect Slack and CLI scenarios into a recurring
   cost/latency benchmark.
7. Keep LangGraph limited to optional WorkItem graph execution until SDK agent
   contracts and the audit layer remain stable under real use.

## Status Table

| Area | Status | Notes |
| --- | --- | --- |
| Python baseline | Implemented | Python 3.11+ required; historical local verification used Python 3.13. |
| SDK builders | Implemented | Four business specialists plus Orchestrator and Chief of Staff. |
| Typed SDK runtime | Implemented | Fake/local tests and live credential gate. |
| Agent registry | Implemented | Canonical agent card source for builders, schemas, prompts, tools, live flags, evals, handoffs, and safety notes. |
| WorkItems/context packs | Implemented | Typed state and route-specific context packs for specialist execution. |
| Quality budgets | Partially implemented | Available for search-heavy agents; propagation still needs audit across all entrypoints. |
| Tool tiers | Partially implemented | Explicit tiers exist; broad builders still need consistent tier selection by caller. |
| Repo-local skills | Partially implemented | Selected `SKILL.md` bundles are active; metadata-driven selection is still a recommended improvement. |
| Fixture workflows | Implemented | Default path for all specialists and pipeline. |
| Gmail live | Live opt-in | Read, label, draft-only. No send. |
| Slack live | Live opt-in | Approval notifications only. |
| Search live | Live opt-in | SearXNG plus capped hosted web search by default; explicit Serper, Firecrawl, and Tavily through `SearchProvider`. |
| Website extraction | Live opt-in | Selected company pages through Trafilatura or Firecrawl. |
| Source-aware synthesis | Partially implemented | Ordered sources, extracted summaries, provider samples, and source-context notes are available; typed source triage remains open. |
| SQLite audit | Implemented | Agent runs, approvals, queue, feedback, sources, tool events. |
| Approval queue | Implemented locally | Approval does not send or publish. |
| Table mirror | Fixture/SQLite implemented | Airtable and Google Sheets live writes are not implemented. |
| Apify/Browserless | Placeholder | Dry-run or `NotImplementedError` for live. |
| LangGraph | Optional graph runtime | WorkItem graph execution behind optional dependency. |
