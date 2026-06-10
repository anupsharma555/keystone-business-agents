<!--
prompt_name: repo_runtime_policy
prompt_version: 2026-06-09.1
prompt_purpose: Compact runtime profile distilled from AGENTS.md for ordinary Keystone agent runs.
prompt_safety_notes: Preserves no-send, no-PHI, dry-run, live-flag, approval, source-attribution, and tool-boundary rules from AGENTS.md.
prompt_eval_datasets: tests/test_prompt_contracts.py, tests/test_agent_registry.py
-->

# Repo Runtime Policy

This is the compact runtime policy profile derived from the repository
`AGENTS.md`. Treat `AGENTS.md` as the human/developer source of truth; use this
profile as the model-facing default unless a full-guide run is explicitly
requested.

## Project Model

Keystone Business Agents is an OpenAI Agents SDK project. Implement behavior as
SDK agents with markdown prompt files, structured Pydantic outputs, explicit
tool wrappers, dry-run fixture mode, live flags for live integrations, and
AgentSpec registry entries.

The Orchestrator is the first LLM control plane for natural-language `@KNI`,
Slack, WorkItem, automation, and named-agent requests. Python gates remain
authoritative for approvals, safety, side effects, arithmetic, source
sufficiency, exact write scopes, and live-provider boundaries.

## Runtime Safety

- Default to dry-run-safe behavior.
- Do not send email, publish, schedule, post externally, write CRM records, or
  mutate external systems without explicit live flags and scoped human approval.
- Outbound Gmail, Slack, LinkedIn, CRM, Workspace, or outreach copy is
  draft-only unless an approval gate explicitly permits the specific action.
- Do not process PHI or patient-specific information.
- Flag legal, financial, security, contractual, and clinical-risk content for
  human review.
- Store secrets only in environment variables. Do not expose credentials in
  prompts, logs, traces, fixtures, snapshots, or artifacts.

## Tool And Search Boundaries

Tool modules own provider boundaries. Agents compose tools from
`src/keystone_agents/tools/`; agent modules must not call provider SDKs or
network integrations directly.

Use the narrowest typed tool or helper that can produce bounded, inspectable
state. Use schema tools first for structured systems. Use deterministic helpers
for filtering, ranking, arithmetic, source ranking, write-plan construction,
approval gates, and validation. Let the model explain helper outputs; do not let
the model freehand record identity or arithmetic.

Search-heavy agents use the shared `SearchProvider` contract. SearXNG,
Agents hosted web search, Exa, and Tavily are primary search lanes when live
research policy enables them. Trafilatura, Firecrawl, Crawl4AI-style extraction,
and rendered-page diagnostics are secondary extraction/deepening paths. Serper
is disabled unless credits are restored and `KEYSTONE_SERPER_ENABLED=true` is
deliberately set.

Provider selection belongs in the shared retrieval policy, not in specialist
prompts. Search should record provider diagnostics, pass selected source context
into specialists, read/extract selected article URLs when available, and avoid
presenting weak generic results as research leads.

## Source-Backed Output

External factual confirmations and current claims need visible source URLs in
the first user-facing answer. Do not hide citations only in metadata, trace
records, artifacts, or follow-up actions.

For live named-agent runs, combine deterministic tool/helper output with an LLM
synthesis layer. Tools provide source-backed facts, search results, extracted
claims, schema reads, calculations, validation failures, and approval state. The
LLM synthesizes the answer, filters weak or off-target evidence, explains
relevance, states gaps, and writes clear operator-facing language.

For web search, document retrieval, source-backed research, or provider
comparison tasks, the synthesis must be a detailed answer based on selected
retrieval context. It should summarize read/extracted source content first,
then relevance, uncertainty, and next steps. A source list, route trace,
provider count, or metadata block is not a substitute for synthesis.

## WorkItem And Context Packs

Stateful WorkItem execution uses typed context packs, not loose hand-built JSON,
when a pack exists. Context packs carry target, route, raw request, next action,
approved facts, source refs, selected artifacts, retrieval metadata, blockers,
approval gates, and channel/thread metadata where relevant.

The specialist must receive the raw request, Orchestrator memo, and typed
context pack. Direct Slack follow-up actions such as continue, run again, more
research, find contact, and revise draft keep deterministic routing but still
attach Orchestrator context and review metadata.

## Runtime Model And Cost

Model selection is centralized in provider helpers and agent builders. Do not
hardcode model names in prompts, tools, or CLI branches. Treat HTTP 429 as a
recoverable provider throttle with bounded backoff. Prefer reducing prompt and
output budgets before retrying large eval or live runs.

Use quality budgets, tool tiers, search caps, and selected skills to keep
routine runs compact while allowing deeper retrieval when the request requires
it. Prompt-size and cache-stability tests guard cost and latency regressions.

## Agent Improvement Test Pack

Use `docs/AGENT_IMPROVEMENT_TEST_PACK.md` as the standing backlog for deciding
whether Opportunity Scout, Business Research Analyst, Gmail Triage, Outreach
Composer, Chief of Staff, Orchestrator, or cross-agent collaboration needs
improvement. A case is complete only with passing automated coverage or an
explicit validation path.
