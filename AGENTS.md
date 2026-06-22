# Keystone Business Agents Guide

## Project Model

This repository is an OpenAI Agents SDK project, not a plain scripting project. Implement Keystone business capabilities as SDK agents with prompt files, structured outputs, explicit tools, and dry-run-safe execution.

For non-trivial changes:

1. Plan the approach.
2. Implement the smallest maintainable change.
3. Verify correctness with tests or a clear validation plan.

Keep final responses concise. Avoid distracting implementation metadata. If file references are useful, group them at the end.

## Agent Architecture

Every Keystone agent must:

- Expose a `build_*_agent()` function that returns an SDK `Agent`.
- Load instructions from markdown prompt files. Do not embed long prompts directly in Python modules.
- Receive shared prompt context through `compose_instructions()`, including the
  shared memory policy and writing style policy.
- Use Pydantic structured output models.
- Use explicit tool wrappers. Agent modules compose tools; tool modules own integration boundaries.
- Support dry-run fixture mode for development and tests.
- Require an explicit flag for every live integration.
- Have an `AgentSpec` registry entry with builder, schema, prompts, tools,
  live flags, eval or validation paths, handoff description, and safety notes.

The registry in `src/keystone_agents/agent_registry.py` is the canonical agent
card source for navigation, CLI inspection, and extension tests. Keep registry
metadata current when adding prompts, tools, schemas, live flags, or eval
coverage.

The Orchestrator is the first LLM control plane for natural-language `@KNI`,
Slack, WorkItem, scheduled-automation, and explicit named-agent requests. It
reads the raw request, compact Slack/thread context, WorkItem state,
`decision_trace`, audit notes, local memory, context packs, and prior agent-run
summaries before deterministic routes or specialist calls execute. Explicit
agent mentions are routing advice, not authority to bypass Orchestrator
preflight, specialist ownership, approval gates, or deterministic safety checks.

Keep the planning layer schema-light. Reuse `ManualRequestPlan`,
`OrchestratorResult`, WorkItems, context packs, specialist output schemas, audit
events, and `decision_trace` entries. Do not add broad intent taxonomies or
large route-specific execution schemas for every new natural-language request.
Planner rationale belongs in compact trace/event metadata and should describe
the assumptions, missing context, selected capability, and next safe action.

Agent capabilities should be schema-first and tool-general, not narrow
deterministic lanes for individual natural-language requests. Deterministic
intent classification is acceptable when it is a bounded routing hint, safety
gate, fixture fallback, validation step, typed tool execution path, arithmetic
step, approval check, or side-effect blocker. It should not replace the
agent's natural-language interpretation layer with phrase-specific branches
that make normal follow-up asks fail. For structured systems such as Airtable,
Google Sheets, CRM records, Gmail, Slack, or local data stores, first expose a
bounded schema/context model and generic read/query/write tools, then let the
agent map user intent onto that schema. If mapping is ambiguous, ask for
clarification; do not silently fall back to a canned one-record preview or a
hard-coded path.

## Tool And Search Architecture

Tool modules own provider boundaries. Agent modules may compose tools from
`src/keystone_agents/tools/`, but they should not import provider SDKs or make
network calls directly.

## Tool Use Decision Rules

Agents should choose tools from the shape of the task, not from brittle keyword
lanes. Prefer the narrowest typed tool or helper that can produce bounded,
inspectable state:

- Use schema tools first for structured systems. For Airtable, Sheets, Gmail,
  Slack, CRM, or local stores, inspect schema/context before reading records or
  preparing writes.
- Use deterministic helpers for exact arithmetic, filtering, record matching,
  deduplication, data-quality checks, source ranking, write-plan construction,
  and approval gates. Let the model explain helper outputs; do not let the
  model freehand arithmetic or record identity.
- Use Google Workspace tools for internal artifacts, docs, sheets, and Drive
  folders when the operator asks to save or share internal work. Live writes
  require the relevant live flags and approval/reference metadata.
- Use Airtable tools for table records, not browser automation. Writes must be
  scoped create/update operations with exact table/field mapping and approval
  references; no deletes, schema changes, attachment uploads, or silent bulk
  overwrites.
- Use the KNI Finance Operations local app only as a read-only finance
  operations context source unless a separate write integration is approved.
  The canonical local app is
  `/Users/anup/Desktop/AllFiles/Professional/KeystoneNeuroinformatics/kni-finance-ops-local/`
  at `http://127.0.0.1:8765`; bridge through its documented JSON API or
  exports for data reads. If Anup explicitly asks from CLI or Slack for a
  business agent to visualize or read the finance operations webpage, read-only
  page inspection is allowed, but form submission and mutation controls remain
  off-limits without a separately approved integration.
- Use Gmail and Slack structured tools for messages, drafts, labels, thread
  context, and posting decisions. Browser tools should not replace provider
  APIs for business-system reads or writes.
- Use extraction providers first for web content: Trafilatura/Firecrawl/Crawl4AI
  style extraction, source ranking, and claim extraction. Use Playwright only
  when static extraction is weak, a JS-rendered page must be inspected, or
  Orchestrator needs early route diagnostics.
- Use Playwright as read-only backend/headless rendered-browser diagnostics
  only. It must use a temporary non-persistent profile and must not open a user-screen browser. It may capture text, links, status, title, and optional
  screenshots under `artifacts/playwright-images`; it must not click, submit
  forms, authenticate, download files, use local files, or perform mutations.
- Use `capture_browser_diagnostics` when a page, dashboard, local app, or
  customer-facing site needs backend browser evidence about console messages,
  page errors, failed requests, response statuses, or resource loading. Pair it
  with `summarize_rendered_page_diagnostics` before presenting findings.
- Use OpenAI file search/vector stores for durable approved docs, runbooks,
  schemas, prior traces, and artifact history when those stores are configured;
  keep secrets, PHI, raw private messages, and unapproved artifacts out of
  hosted stores.
- Use Sandbox/Codex-style workspace review only for repo-local diagnosis, test
  runs, generated reports, and draft artifact review. It must not trigger live
  business side effects.
- If a tool/helper is missing for a repeated task, add a bounded schema and
  helper before adding prompt-specific branches. After implementing each tool or helper, run its focused tests before broad integration tests.
- If the right schema, record identity, source basis, live flag, or approval
  scope is missing, ask a targeted clarification or return an exact blocker
  instead of guessing.

## Local KNI Document QA

Local Keystone Neuroinformatics document questions must use one general
retrieval-and-reasoning path. Do not add deterministic branches for individual
natural-language questions such as one formation-record wording, one insurance
broker wording, or one follow-up phrasing. A missed answer is usually a
retrieval contract, evidence ranking, context-pack, or synthesis issue, not a
reason to hardcode the expected answer in Python.

The deterministic layer may detect that the request needs local KNI document
access, expose or call bounded local KNI search/read tools, refresh or report
the local index status, rank candidate documents by source quality and
sensitivity, enforce `local_only=true` and `send_enabled=false`, block sensitive
content, mark review-required material, and validate that a local-document
answer includes evidence paths and uncertainty.

The deterministic layer must not compose the substantive answer for arbitrary
who/what/which questions; encode expected names, vendors, dates, organizers,
brokers, agencies, or counterparties for a single query; replace model
interpretation with phrase-specific routing; or treat the first retrieved source
or a prefetch summary as the final answer.

The Chief of Staff should interpret bounded document evidence itself. It should
distinguish roles found in documents, such as organizer, signer, registered
agent, broker, producer, agency, insurer, coverholder, underwriter, owner, or
accountable lead, and state uncertainty when the document evidence does not
support the requested role. If a local KNI Slack test fails, improve the shared
local-document retrieval/evidence contract or add a general eval that exercises
the contract across multiple wording variants; do not add a one-off shortcut
for the observed prompt.

Search-heavy agents use the shared `SearchProvider` contract. Current provider
implementations are:

- `dry-run`: default, no network calls.
- `searxng`: broad-recall live search when explicitly live-enabled.
- `agents-web-search`: OpenAI Agents SDK hosted web search, enabled as a capped
  parallel lane beside SearXNG for default live research.
- `exa`: capped semantic deepening lane for configured live search runs, and an
  explicit provider for Exa-first comparison runs.
- `tavily`: optional capped deeper-research provider with local credit tracking;
  used for explicit deeper-search asks or when runtime flags enable it.
- `firecrawl`: explicit live search provider when configured with
  `FIRECRAWL_API_KEY`; Firecrawl is also a secondary page extraction provider.
- `serper`: disabled while API credits are unavailable. It must not run unless
  `KEYSTONE_SERPER_ENABLED=true` is deliberately set after credits are restored.

The SDK `search_web` tool is inert unless live mode, non-dry-run mode, and live
research are enabled. With no `SEARCH_PROVIDER` override, it resolves to
SearXNG plus a capped Agents hosted web-search parallel lane, with Exa/Tavily
available as capped deepening lanes according to shared retrieval policy and
runtime flags.
Planner agents may set live-search intent and constraints, but provider
selection stays in the shared Python retrieval policy.

Medium-term search architecture goal: Keystone agents should produce enriched,
detailed, succinct, and query-relevant answers that are more useful than
standard generic LLM search. Search-capable agents should combine the original
request, provider recall, selected source URLs, extracted page text, WorkItem
context, approved memory, and specialist reasoning before synthesis. Novel
open-source, free-tier, or low-cost capabilities such as Exa, Tavily,
Trafilatura, Firecrawl, Crawl4AI-style extraction, hosted file search, or future
MCP tools may be added when they materially improve retrieval, extraction,
permission scoping, or cross-client reuse. Add them through the shared provider
or tool boundary with dry-run tests, explicit live flags, credential checks,
source attribution, budget controls, and compact diagnostics. Do not add
provider-specific prompt branches or broad live tools merely because an
integration exists.

This repo's local SearXNG runtime is separate from the `keystone-slack` repo's
runtime. Keystone Business Agents uses the `kba-searxng` Colima profile and
`SEARXNG_BASE_URL=http://127.0.0.1:18080`; `keystone-slack` uses its own
`kni-searxng` profile on port `8080`. When Slack scheduled automations delegate
to Business Agents child runners, do not preflight or start the Slack repo's
`8080` SearXNG instance for those child runs. Let the Business Agents child
environment and retrieval policy own the `18080` runtime and degrade with
runner diagnostics if that endpoint is unavailable.

Scheduled Business Agent automations that include research must preserve the
same agent path used by manual `@KNI` runs: Orchestrator preflight/planning,
deterministic retrieval, specialist SDK synthesis, Orchestrator output review,
and renderer-owned Slack output. Do not replace specialist agents with
template-only summaries. Live search should
record provider and reachability diagnostics, pass source context into the
appropriate specialist, read selected article URLs when available, and avoid
presenting weak generic search results as research leads.

Website extraction is a separate live-gated path. Use Trafilatura by default for
selected company pages, or Firecrawl when `KEYSTONE_WEBSITE_EXTRACTOR=firecrawl`.
`KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK` may switch between `trafilatura` and
`firecrawl` when the primary extractor fails or returns no usable claims.
`KEYSTONE_AGENT_HTML_REVIEW=true` enables a capped Agents SDK second-pass review
over already retrieved HTML/text when deterministic extraction is weak. Keep it
bounded with `KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES`; it may add source-backed
claim candidates but must not replace deterministic URL/source records.

Apify and Browserless wrappers remain placeholder/dry-run boundaries. Do not
document or implement them as live production paths until a reviewed provider
adapter, explicit live flags, credential checks, tests, and source-attribution
rules exist.

## WorkItem Context Packs

Stateful WorkItem execution uses typed context packs as the contract between
orchestration, deterministic gates, and specialist agents. Do not pass loose
hand-built JSON summaries to specialists when a WorkItem context pack is
available.

Context packs must be derived from WorkItem state and related local references:

- target, route, request text, and next action
- approved facts and source refs
- selected artifact refs and retrieval metadata
- blockers and approval gates
- recipient, thread, objective, or channel metadata where relevant

Use the four specialist packs:

- `ResearchContextPack`
- `OpportunityContextPack`
- `OutreachContextPack`
- `GmailContextPack`

Python gates are authoritative. The orchestrator may recommend routing,
clarification, more research, or approval, but it must not bypass these gates:

- outreach recipient readiness
- approved claims readiness
- source sufficiency
- research sufficiency
- opportunity objective clarity
- Gmail thread readiness
- external-use approval readiness

Keep legacy context helpers as compatibility shims only if needed; new code
should prefer `build_context_pack_for_route()` or the route-specific typed pack
builders.

## Orchestrator-First Execution

Every natural-language entrypoint should preserve this order unless a narrower
deterministic health/status command is being handled:

1. Capture the raw request and available context without rewriting away the
   operator's wording.
2. Run Orchestrator preflight to produce route advice, planner rationale,
   blockers, safety notes, retrieval hints, and compact context for downstream
   agents.
3. Let Python apply deterministic gates for safety, approvals, arithmetic,
   record identity, source sufficiency, live-provider boundaries, and exact
   write scopes.
4. Call the selected specialist with the raw request plus the Orchestrator memo
   and typed context pack. Specialists must still read the original request.
5. Review specialist output through Orchestrator or deterministic review before
   rendering. Slack and CLI feedback may stream preflight, review, repair, and
   completion events in real time.

Direct Slack WorkItem actions such as continue, run again, more research, find
contact, and revise draft keep their deterministic route and optional LangGraph
wrapper, but they must attach Orchestrator preflight context and record
`orchestrator_action_review` metadata after specialist execution. Direct
send/post/share wording should route to the owning specialist's approval gate,
usually Outreach Composer, and remain draft-only or blocked unless a scoped
approval explicitly permits the requested non-send action.

## Runtime Model Policy

Keystone supports per-agent runtime model configuration. Keep model selection
centralized in the model provider helpers and agent builders; do not hardcode
model names inside prompts, tools, or CLI branches.

- The Orchestrator is the control plane. It defaults to `gpt-5.4-mini` through
  `KEYSTONE_ORCHESTRATOR_MODEL` for hybrid LLM/deterministic review. Python
  remains authoritative for safety gates, approvals, and side-effect blocking.
- Main operating agents default to OpenAI `gpt-5.4-mini` during integration
  testing. Schema interpretation, tool selection, memory use, source synthesis,
  and SDK handoffs should stay on the same audited provider path unless
  explicitly changed.
  Gmail Triage and Outreach Composer may still use Gemini Flash through explicit
  per-agent provider/model overrides. The runtime still uses the OpenAI Agents
  SDK provider path with Chat Completions compatibility for Gemini; it does not
  use a separate Gemini SDK adapter. Gemini may also be enabled as a backup with
  `KEYSTONE_ENABLE_GEMINI_FALLBACK=true` for cases where the primary OpenAI live
  SDK attempt is unavailable.
  LiteLLM remains an optional external gateway override through
  `LITELLM_BASE_URL` or agent-specific base URLs. Do not require or import the
  Python `litellm` package in this repo unless a future dependency review proves
  it is compatible with the pinned OpenAI Agents SDK baseline.
- Explicit builder arguments override agent-specific environment variables.
  Agent-specific variables override the global `MODEL_PROVIDER` and
  `OPENAI_MODEL` settings.
- Live model execution must fail before a model call when the selected provider
  lacks required credentials or gateway configuration.
- All live model runs and ad hoc Agents SDK smoke tests for this repo should
  use the repo-specific `KEYSTONE_OPENAI_API_KEY` from the local `.env` or shell
  environment. Do not rely on a generic `OPENAI_API_KEY` from another project;
  if a raw SDK snippet requires `OPENAI_API_KEY`, map `KEYSTONE_OPENAI_API_KEY`
  into that process explicitly without printing the secret.
- Current SDK conversation continuity uses option 2, local Agents SDK sessions.
  Do not use `result.to_input_list()`, server-managed continuation IDs, or
  `result.to_state()` with SDK interruptions as the default state mechanism
  unless a future assessment deliberately changes the state model.
- Live SDK runs can hit organization/project model rate limits, especially TPM
  limits on large structured prompts. Treat HTTP 429 as a recoverable provider
  throttle: wait for the reported reset or retry-after window, then retry with
  bounded exponential backoff and jitter. Do not hot-loop retries because failed
  requests can still count against per-minute limits.
- Prefer reducing the prompt and output budget before retrying large eval runs:
  pass compact source-backed facts instead of full nested profiles, cap expected
  output size when supported, and use a smaller approved model such as a mini or
  nano variant for smoke/eval runs when quality requirements allow. Request a
  higher OpenAI limit only when compact prompts and reasonable backoff are not
  enough.

## Output Formatting Model

Agent final outputs are structured data first. Use Pydantic schemas as the
contract between agents, tools, storage, tests, and renderers. Do not rely on
free-form model markdown as the canonical output shape.

External factual confirmations must be source-visible in the first user-facing
answer. If an answer includes source-backed external facts, current claims,
dates, deadlines, rates, filing obligations, policies, company facts, roles, or
opportunity signals, include the relevant source URLs in the visible Slack/CLI
summary or explicitly state that source verification is still needed. Do not
hide citations only in structured `sources`, trace metadata, artifacts, or
follow-up actions.

Human-facing narrative should be LLM-synthesized from bounded structured inputs
whenever live or local SDK synthesis is explicitly requested, including company
research summaries, opportunity summaries, Gmail triage notes, outreach drafts,
approval rationales, and recommendations. Deterministic renderers still own the
final Slack, Markdown, report, dashboard, and CLI layout, and Python remains
authoritative for schema validation, safety gates, source attribution,
approval/no-send rules, and side-effect blocking. The LLM may vary copy inside
bounded fields such as `draft_reply`, `email_body`, `linkedin_note`, summaries,
rationales, and recommendations, but not the canonical output shape.

For live named-agent runs, every user-facing response should combine
deterministic tool/helper output with an LLM synthesis layer. Tools and helpers
provide the source-backed facts, search results, schema reads, calculations,
record matches, validation failures, and approval state. The live LLM then
synthesizes the answer, explains relevance, filters weak or off-target evidence,
states what was and was not found, and writes the response in clear operator
language. Do not post raw artifact lists, route metadata, or workflow status as
the main answer unless the user explicitly asked for debugging details.

Outbound email fields must be plain text, draft-only, approval-gated, and source
backed. Approved aggregate email style profiles may guide greetings, paragraph
shape, preferred phrases, CTA style, and signoffs. A style profile may specify a
personal signoff such as `Sincerely,\nAnup`; that affects draft text only and
must never weaken approval, no-send, length, PHI, or unsupported-claim rules.

## Codex Agent Mention Shorthand

When the operator sends a Codex message that starts with `@KNI`, treat it as a
request to run the local natural-language agent CLI. Prepend the repo-local
runner and pass the mention through unchanged:

```bash
.venv/bin/python scripts/ask_agent.py @KNI orchestrator agent "find 5 behavioral health AI companies"
```

For example, a Codex message like:

```text
@KNI business research analyst "research Lindus Health"
```

should be executed as:

```bash
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health"
```

Default behavior remains dry-run-safe. In live-test/full-live environments, the
CLI auto-enables live SDK model execution for explicit named-agent mentions; use
`--no-live-sdk` when the operator asks to force dry-run / WorkItem handling.
This shorthand must never imply permission to send, publish, schedule, post to
Slack, create Gmail drafts, or write externally.

## Safety Rules

- No external email may be sent automatically.
- Outbound email, LinkedIn, Slack, CRM, or other outbound copy defaults to draft-only behavior.
- Human approval is required before outbound copy is sent, published, scheduled, or handed to a live integration.
- Do not process PHI or patient-specific information.
- Flag legal, financial, security, and contractual content for human review.
- Company, opportunity, local collection, and broader research must include source attribution.
- Store API keys, tokens, credentials, and secrets only in environment variables.
- Logs, traces, exceptions, fixtures, and test snapshots must not expose secrets.

## Sandbox Agent Rules

Sandbox Agents are optional workspace-review tools, not the default execution path. Use ordinary SDK agents when sanitized structured inputs are enough. Consider Sandbox Agents only when the task genuinely needs a filesystem workspace, such as mounted research folders, generated report review, document batches, local validation commands, or resumable workspace inspection.

Keep the harness and control plane outside sandbox compute. The host harness owns user intent, approvals, live-integration flags, model configuration, tracing policy, audit records, artifact review, and artifact release. The sandbox may inspect scoped files and create draft artifacts, but it must not trigger email, Slack, CRM, LinkedIn, scheduling, publication, or other live side effects.

For sensitive repositories or workspaces:

- Stage only the minimum files needed into a scoped temporary folder before mounting.
- Never mount home, root, broad unrelated workspaces, credential folders, `.ssh`, `.aws`, `.gnupg`, `.config`, `.env`, OAuth token files, or API credential files.
- Do not place secrets, PHI, patient-specific content, private customer notes, or credentials in prompts, manifests, logs, snapshots, or artifacts.
- Mount folders through explicit `Manifest` and `LocalDir` entries with workspace-relative paths.
- Require generated sandbox outputs to land under `artifacts/` and treat them as drafts.
- Review and redact artifacts in the host harness before moving them out of the sandbox or using them downstream.
- Keep sandbox execution optional for tests through import guards; tests must pass when Sandbox SDK classes are unavailable.
- Use `run_sandbox_workspace_review(..., execute=False)` for setup previews. Actual sandbox execution requires `execute=True` plus either `live=True` for credential-gated SDK execution or an injected fake/local runner in tests.

See `docs/SANDBOX_AGENTS.md` for the fuller setup plan and Unix-local scaffolding shape.

## Development Defaults

- Develop dry-run first with deterministic fixtures.
- Do not implement live integrations unless explicitly requested.
- Do not invent APIs, endpoints, credentials, schemas, or production behavior.
- Add or update tests when changing agents, prompts, tools, schemas, safety logic, or outbound workflows.

## Repo-Local Codex Skills

Repo-specific Codex-facing skills live under `codex-skills/`. Use them only for
this repository; they are not global skills and should not be copied into other
repos without review. When a task matches one of these entries, read the
matching `SKILL.md` before touching repo files.

- `codex-skills/kba-agent-contract-change/SKILL.md`: use when changing SDK
  agents, prompts, schemas, tools, context packs, handoffs, runtime skills,
  `AgentSpec` metadata, or related tests.
- `codex-skills/kba-new-agent/SKILL.md`: use when creating, shaping, or
  integrating a novel Keystone agent, including workflow specialists, context
  specialists, Chief-of-Staff callable specialists, workflow-specific registry
  metadata, runtime skill bundles, tool policies, handoff contracts, LLM
  reasoning preservation, and eval coverage.
- `codex-skills/kba-eval-readiness-triage/SKILL.md`: use when diagnosing or
  changing Promptfoo evals, Slack eval readiness, eval dashboards, route
  compaction, or trace summary behavior.
- `codex-skills/kba-search-provider-eval/SKILL.md`: use when changing or
  evaluating retrieval providers, source attribution, extraction, provider
  budgets, or search/browser extraction evals.
- `codex-skills/kba-workitem-orchestrator-ops/SKILL.md`: use when operating or
  debugging `@KNI`, Orchestrator-first routing, WorkItems, context packs,
  approval gates, Slack action continuations, or local state/audit flows.
- `codex-skills/kba-live-sdk-smoke-and-cost/SKILL.md`: use when running or
  debugging live SDK smoke tests, model/provider configuration,
  `KEYSTONE_OPENAI_API_KEY`, SDK sessions, traces, rate limits, budget guards,
  request-cache behavior, or cost telemetry.

## Agent Improvement Test Pack

Use `docs/AGENT_IMPROVEMENT_TEST_PACK.md` as the standing backlog for deciding
whether Opportunity Scout, Business Research Analyst, Gmail Triage, Outreach
Composer, or cross-agent collaboration needs improvement. When iterating on an
agent, map the relevant pack cases to existing coverage first, then add the
smallest useful pytest fixture, static eval, local JSONL eval, or documented
manual validation path before changing behavior.

Do not treat a pack item as complete only because the document exists. A case is
complete only when it has passing automated coverage or an explicit validation
path, and any fix preserves dry-run defaults, approval gates, source
attribution, no-PHI handling, and no-send behavior.
