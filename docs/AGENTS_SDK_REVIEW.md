# Agents SDK Review And Improvement Map

This note records the current Agents SDK review for Keystone Business Agents.
It is intentionally implementation-facing: use it when deciding whether a new
agent needs more local context, a schema, a tool wrapper, an MCP server, or a
different orchestration pattern.

## Sources Reviewed

Official OpenAI guidance:

- Agents SDK overview and build track:
  https://developers.openai.com/api/docs/guides/agents
- Agent definitions:
  https://developers.openai.com/api/docs/guides/agents/define-agents
- Tools and Agents SDK tool wiring:
  https://developers.openai.com/api/docs/guides/tools
- Orchestration and handoffs:
  https://developers.openai.com/api/docs/guides/agents/orchestration
- Guardrails and human review:
  https://developers.openai.com/api/docs/guides/agents/guardrails-approvals
- Results and state:
  https://developers.openai.com/api/docs/guides/agents/results
- MCP integration and observability:
  https://developers.openai.com/api/docs/guides/agents/integrations-observability#mcp
- Python SDK MCP reference:
  https://openai.github.io/openai-agents-python/mcp/

Representative public implementations:

- OpenAI customer-service agents demo:
  https://github.com/openai/openai-cs-agents-demo
- OpenAI cookbook multi-agent portfolio collaboration example:
  https://cookbook.openai.com/examples/agents_sdk/multi-agent-portfolio-collaboration/multi_agent_portfolio_collaboration
- OpenAI Agents Python SDK examples and docs:
  https://github.com/openai/openai-agents-python

## Current Fit

Keystone already matches the core SDK pattern in the places that matter most:

- `src/keystone_agents/agent_registry.py` is the canonical agent card source.
- Agent builders return SDK agents and attach instructions, tools, output
  schemas, guardrails, model settings, and handoff descriptions.
- Prompt content lives in markdown files with metadata headers.
- Pydantic schemas are the contract for agent outputs, context packs, approvals,
  storage records, and renderer inputs.
- Tool modules own provider boundaries and default to dry-run or fixture-safe
  behavior.
- `src/keystone_agents/agent_tool_policy.py` gives each agent a bounded tool
  surface.
- WorkItems and context packs preserve local state without replacing SDK agents.
- SDK sessions provide scoped conversation continuity for Chief of Staff and
  live WorkItem follow-ups.

The biggest remaining architectural risk is not missing SDK primitives. It is
allowing new context sources or provider wrappers to enter the system as
prompt-only conventions instead of explicit tool, schema, registry, safety, and
test surfaces.

## 2026-06-09 Current Repo Assessment

The current checkout is closer to a production Agents SDK integration than a
plain scripting project. The main improvement area is not SDK conformance; it is
runtime efficiency for common Slack, CLI, and WorkItem turns.

Strengths:

- The SDK boundary is centralized. `src/keystone_agents/sdk.py` owns SDK imports,
  model settings, live credential checks, sessions, tracing metadata, usage
  capture, prompt-cache defaults, and local fallback behavior.
- The registry is useful. `AgentSpec` keeps each registered agent's builder,
  output schema, prompts, tools, live flags, eval paths, handoff description, and
  safety notes in one inspectable card.
- Tool ownership is mostly clean. Provider boundaries live in
  `src/keystone_agents/tools/`, while agent modules compose tools instead of
  importing provider SDKs directly.
- Dry-run safety is a real architecture property. Live model, search, Gmail,
  Airtable, Google Workspace, Slack, browser, and sandbox paths are gated by
  flags, credentials, or typed approval boundaries.
- Cost/cache observability is already ahead of many standard examples. The
  runtime records SDK usage, cache hit rate, request-cache fingerprints, local
  cost estimates, budget decisions, and WorkItem cost events.
- The latest worktree moves reusable reasoning contracts out of the monolithic
  `skills.md` prompt and into declared repo-local skill bundles. The registry now
  exposes those skills explicitly on agent cards, and prompt/eval tests cover
  shared plus specialist skill contracts.
- Quality budgets now exist for Business Research Analyst and Opportunity Scout,
  not only Chief of Staff. They map `fast`, `balanced`, and `deep` modes to
  model settings, max turns, retrieval result counts, hosted-search caps, page
  verification, repair, contact enrichment, and research reuse defaults.
- Tool tiers are now explicit. The tool policy layer classifies tools into
  `core_read`, `web_search`, `deep_retrieval`, `diagnostic`, `internal_write`,
  and `publish`, and search-heavy agent builders can attach narrower tool
  surfaces for cheaper or safer runs.
- Final user-facing synthesis is more source-aware. The synthesis input now
  includes ordered sources, extracted source summaries, provider-result samples,
  and source-context warnings so the final answer can distinguish extracted
  evidence from search-lane candidates.

Tradeoffs:

- The static prompt prefix is large. Each agent can receive the repo guide,
  shared prompt fragments, agent prompts, and selected skill bundles before the
  task-specific payload. Dynamic skill selection reduces some prompt bloat, but
  first-run cost and model attention cost remain high.
- Tool surfaces are better classified, but full agent builders can still expose
  broad schemas unless callers pass an appropriate tier or quality budget. The
  remaining risk is inconsistent use of the tiered builders across CLI, Slack,
  WorkItem, and direct SDK paths.
- The Orchestrator and some specialist paths contain substantial deterministic
  route and phrase handling. Deterministic gates are necessary for safety,
  approvals, record identity, and cost controls, but phrase-specific routing
  should stay secondary to typed schemas, context packs, and model interpretation.
- Local approval queues are mature, but SDK-native approval interruptions are not
  yet the primary lifecycle. If `needs_approval` tools become live, persist the
  SDK resumable state beside Keystone approval records instead of starting a new
  run after review.
- Richer source-aware final synthesis improves functionality, but it can also
  add another SDK call and more output tokens. It should remain budget-gated and
  skipped when a deterministic artifact is already the canonical user-facing
  answer.

Latest change assessment:

- **Implemented / materially improved:** research quality budgets for Business
  Research Analyst and Opportunity Scout; explicit tool-tier classification;
  selected repo-local skill bundles with metadata and eval coverage; source-aware
  final response synthesis; richer retrieval diagnostics with search queries,
  provider samples, and source-focus metadata; route-aware Slack cost profiles
  including formal opportunity deep mode.
- **Partially implemented:** compact runtime context. Skill selection reduces
  prompt load, and the default repo-level runtime policy now uses
  `prompts/repo_runtime_policy.md` instead of embedding the full `AGENTS.md`
  guide. Tool tiers are available, but every live path must consistently pass
  the right tier or budget-derived settings. Cost telemetry is broad, but
  post-change live no-side-effect experiments still need to prove actual
  platform cost and latency improvements.
- **Still open:** SDK-native approval interruption persistence; a bounded
  source-triage/reranker step that classifies retrieved candidates before final
  synthesis; MCP only where it improves cross-client scoping; durable latency
  dashboards for p50/p95 across retrieval, specialist SDK calls, review, and
  final synthesis.

Remaining highest-impact improvements:

1. Propagate quality budgets everywhere. Business Research Analyst and
   Opportunity Scout now have budgets, but CLI, Slack, WorkItem, direct SDK,
   manager-loop, and final-synthesis paths should all consume the same resolved
   budget fields instead of treating some fields as advisory.
2. Make tiered tool attachment the default execution path. Default to
   `core_read` or `web_search` for routine asks, escalate to `deep_retrieval`,
   `diagnostic`, or `internal_write` only when the request, route, live flags,
   and approval state justify the cost and risk.
3. Extend prompt-size budget coverage. The repo now has a compact runtime prompt
   profile for `AGENTS.md`, but composed agent prompts are still large because
   specialist prompts and selected skills dominate. Add budget checks for total
   composed instructions by agent, selected skill count, quality mode, and tool
   tier.
4. Turn the existing cost queue into a recurring benchmark. For representative
   Slack and CLI scenarios, compare first-run versus follow-up cache hit rate,
   uncached input, output tokens, hosted-search calls, retrieval query count,
   latency, local estimate, and operator-supplied platform actual cost.
5. Add context-source catalog tests before adding new integrations. Every new
   source should declare an owner module, allowed agent list, live flags,
   approval notes, source-attribution behavior, and at least one validation path.
6. Add bounded source triage for search-heavy agents. Retrieval should
   collect candidate snippets and extracted page context, then a specialist or
   reranker agent should classify candidate sources as retained, review-only, or
   rejected against the raw operator request before final synthesis. Python
   should still enforce hard source URL, timing, eligibility, contradiction,
   approval, and side-effect gates.
7. Preserve MCP as an optional boundary. Use direct function tools for
   Keystone-owned providers unless MCP improves cross-client reuse, permission
   scoping, or auditability enough to justify a tested server boundary.

Implementation order:

1. Wire budget-derived model settings, max turns, retrieval caps, hosted-search
   caps, and tool tiers through all Business Research Analyst and Opportunity
   Scout entrypoints.
2. Add read-only/default tool tiers for Gmail Triage, Outreach Composer,
   Orchestrator, and Chief of Staff where the current path still builds the full
   tool surface for routine asks.
3. Composed prompt-size budgets with prompt-fingerprint and prompt-contract
   tests.
4. Repeated-run live no-side-effect cost experiment using the existing comparison
   scripts.
5. Source-triage/reranker spike for Opportunity Scout and Chief of Staff search
   briefs, reusing provider diagnostics and selected-page extraction as bounded
   input.
6. SDK approval-interruption persistence spike only after a write-capable tool is
   ready to use SDK-native `needs_approval`.

## Instruction Surface Review

This repo now has three separate instruction surfaces:

- `AGENTS.md`: durable repo-level architecture, safety, tool, and development
  policy.
- `src/keystone_agents/prompts/*.md`: runtime prompt fragments for shared
  context, agent identity, tool policy, output style, and specialist roles.
- `src/keystone_agents/skills/*/SKILL.md`: progressively disclosed reasoning and
  output contracts selected per agent/request.

That split is directionally correct. It mirrors standard agent implementation
practice: long-lived developer policy belongs outside code, reusable reasoning
belongs in named skill bundles, and runtime agents receive only the prompts and
tools needed for the current task. The current structure can be improved in a
few targeted ways.

Recommended changes:

1. **Keep the runtime guide profile for `AGENTS.md` compact.** The root guide is
   intentionally comprehensive, while `compose_instructions()` now uses
   `prompts/repo_runtime_policy.md` by default and preserves a full-guide escape
   hatch through `KEYSTONE_AGENTS_GUIDE_PROFILE=full`. Keep `AGENTS.md` as the
   human/developer source of truth. Tests should continue proving the compact
   profile covers SDK-agent architecture, dry-run defaults, no-send/no-PHI
   policy, source attribution, live flags, approval gates, and tool-boundary
   rules.
2. **Make instruction-surface ownership explicit.** Add a small table, either in
   `AGENTS.md` or `docs/SKILLS_ARCHITECTURE.md`, that says which rules belong in
   repo policy, shared prompts, specialist prompts, skills, tools, schemas,
   gates, or tests. This prevents the same rule from being copied into every
   layer and makes future edits easier to place.
3. **Move skill-selection hints into skill metadata.** Today the selector owns
   many trigger phrases in Python while the `SKILL.md` front matter owns purpose,
   applicability, evals, and safety notes. Add optional metadata such as
   `trigger_phrases`, `default_for_routes`, `requires`, `conflicts_with`, and
   `compact_summary`, then generate or validate selector behavior from those
   fields. This keeps skill docs, selection, and tests from drifting.
4. **Normalize every skill to the full section template.** Most skills include
   the same twelve sections. Keep that as the contract and add missing
   `Applicable Agents` / `Typical Inputs` sections where absent, including
   route-specific skills. This improves scanability and lets tests enforce the
   same structure uniformly.
5. **Keep `prompts/skills.md` as legacy compatibility only, then retire it when
   tests permit.** Registered agents no longer need the monolithic skills prompt
   in composed instructions. Either move it to a clearly named compatibility
   document or keep a test that proves no registered agent includes it. Avoid
   adding new behavior there; new reusable behavior should go into a specific
   `SKILL.md`.
6. **Split large skills with references only when there is evidence.** Current
   skills are mostly 100-150 lines, which is still manageable but expensive when
   several are selected together. Keep the main `SKILL.md` focused on required
   behavior, boundaries, rubric, and output contract. Move examples, borderline
   cases, and long source recipes into optional `references/` files only when
   eval failures show the extra context is useful.
7. **Expand prompt-size budgets in instruction tests.** Static-prefix
   fingerprint tests already protect cache stability, and the compact runtime
   profile now has a size guard against the full `AGENTS.md` guide. Add budget
   checks for selected skill count and composed instruction character/token size
   by agent and quality mode. This turns prompt bloat into a visible regression
   instead of a gradual latency/cost drift.
8. **Trace selected instruction surfaces in run metadata.** SDK request-cache
   metadata now records the compact/full repo instruction profile. Extend the
   audit payload to include selected skill IDs, prompt versions, skill versions,
   and tool tier in one place. That makes response-quality, cost, and latency
   regressions diagnosable without storing full prompts.

## Diverse Ask Handling Review

This review is architecture-only. It does not judge current behavior from live
agent tests; it maps likely strengths and failure modes from the current
planner, retrieval policy, context packs, skill selection, WorkItem runner, and
final synthesis contracts.

The system is structurally capable of handling a wide range of asks because it
has an Orchestrator-first path, named specialists, typed context packs,
dry-run-safe tools, source-aware retrieval quality checks, action-boundary
skills, and final response synthesis that can focus on the latest user request.
The main precision risk is that diverse natural-language asks are still
compressed early into a small manual plan and route shape. That plan captures
agent, intent, target, count, constraints, required entities, and side-effect
policy, but it does not yet preserve enough dimensions of the ask: evidence
depth, source type, strictness of filters, prior-context dependence, requested
output form, permission state, and stop condition.

Scenario assessment:

| Ask family | Example query shape | Likely strength | Precision risk |
| --- | --- | --- | --- |
| Narrow source follow-up | `summarize source 2 from the prior thread in 5 bullets` | Final synthesis has ordered-source rules and can prefer the latest request over broad artifact context. | If the referenced source is missing or not extracted, the agent may broaden into a general topic answer unless the plan carries a hard source-focus stop condition. |
| Broad discovery | `find behavioral health AI companies relevant to Keystone` | Opportunity Scout, source lanes, provider diagnostics, and quality budgets can support flexible discovery without forcing a fixed taxonomy. | Broad retrieval can drift toward adjacent companies or generic provider snippets unless retained/review/rejected source triage is enforced before synthesis. |
| Formal opportunities | `find active grants, RFPs, pilots, or roles posted this week` | Retrieval quality checks already recognize grants, roles, trials, recency, and primary-source signals. | The system needs hard gates for active status, eligibility, geography, deadline, and official-source evidence so news pages or stale summaries are not treated as active opportunities. |
| Company/account research | `research Lindus Health for partnership relevance` | Business Research Analyst has source attribution, website extraction, identity resolution, and unsupported-claim handling. | Similar names, thin company pages, and marketing copy require explicit uncertainty fields; the agent should not silently infer traction, customers, funding, or leadership. |
| Comparison/ranking | `compare Lindus Health and Holmusk as partnership targets` | Structured artifacts and source mapping make comparison feasible. | Heterogeneous evidence can be blended into a false ranking unless the comparison schema requires common fields, missing-data markers, and source URLs per row. |
| Literature or local-source review | `summarize this Zotero article collection for KNI relevance` | The research route can handle source research and local context without live web search. | Company-oriented fields can distort article or collection asks; source-review output should preserve article, claim, method, limitation, and relevance shape. |
| Gmail and thread triage | `what needs follow-up this week from email?` | Gmail-specific schemas, draft-only rules, risk flags, and approval boundaries are present. | If selected thread/message context is incomplete, the agent must ask for context or return a scoped query plan instead of inferring recipients, deadlines, or thread facts. |
| Outreach drafting | `write a warm founder email from this research brief` | Outreach Composer has approved-context gates, no-send policy, style adaptation, and draft-only contracts. | Mixed discovery-plus-drafting asks should not draft from unapproved or weak source context; route should stop after research or produce a clearly blocked draft plan. |
| Internal operations | `review the business-agent architecture and next steps` | Chief of Staff and Orchestrator paths can handle operational diagnosis, WorkItems, Slack context, and repo artifacts. | Route heuristics can overmatch `research`, `review`, or `summarize` terms; operational asks need preserved target domain so they do not become company/opportunity research. |
| Exact-match or no-padding search | `find exact active remote U.S. roles; if none, say none` | Retrieval autonomy hints and source triage skills can support hard filters. | Desired count and opportunity discovery defaults may pressure the agent to fill slots with adjacent results unless `zero_results_allowed` and hard-filter failures are explicit. |
| Mixed multi-step workflow | `find companies, research the best one, then draft outreach; do not send` | Orchestrator, WorkItems, context packs, and handoff packaging are suited to staged execution. | A single route can collapse too much work into one specialist. The plan needs an explicit staged workflow with stop points, approvals, and per-stage evidence requirements. |
| Cost-sensitive ask | `quickly tell me whether this is worth deeper research` | Quality budgets and tool tiers support fast/balanced/deep modes. | Budget fields must be propagated consistently so quick asks do not trigger deep retrieval, page extraction, review, and final synthesis by default. |

Recommended architecture changes:

1. **Add ask-shape dimensions without adding a broad intent taxonomy.** Extend
   the manual/orchestrator plan or specialist brief with compact fields such as
   `ask_breadth`, `evidence_depth`, `source_type_preference`,
   `strict_filter_mode`, `output_form`, `prior_context_dependency`,
   `permission_state`, `cost_mode`, and `stop_condition`. These are orthogonal
   dimensions, not new route enums.
2. **Make stop conditions explicit.** Carry fields such as
   `zero_results_allowed`, `do_not_broaden`, `must_extract_referenced_source`,
   `official_source_required`, and `approval_required_before_next_stage` into
   context packs and final synthesis inputs. This is the simplest way to keep
   flexible agents precise on exact-match and narrow follow-up asks.
3. **Turn source triage into a typed contract.** The repo already has
   source-triage skills and context-pack fields. Promote the triage result to a
   structured object with `retained`, `review_only`, `rejected`, `deepen`, and
   `why_rejected` entries, then require final synthesis to support claims only
   from retained or extracted deepen sources.
4. **Add scenario-matrix review rows before new live tests.** Create a small
   review artifact that maps scenario family, route, required context, allowed
   tool tier, expected failure mode, stop condition, and output shape. This can
   reuse `docs/AGENT_IMPROVEMENT_TEST_PACK.md` but should stay architecture
   oriented until the team chooses which cases deserve automated evals.
5. **Preserve requested output shape as a first-class field.** Final synthesis
   already contains instructions for tables, bullets, source summaries, and
   narrow follow-ups. Moving output shape into the plan/context pack would make
   specialists and renderers less dependent on prompt memory.
6. **Separate route selection from workflow staging.** A query can be
   Opportunity Scout for stage one, Business Research for stage two, and
   Outreach Composer for stage three. The Orchestrator should represent that as
   a staged plan with gates, not a single overloaded route.
7. **Use quality budgets as ask policies.** Map fast, balanced, and deep modes
   to concrete behavior per scenario family: retrieval count, provider lanes,
   extraction requirement, review requirement, final synthesis requirement, max
   turns, and allowed tool tier. Then audit that CLI, Slack, WorkItem, direct
   SDK, and manager-loop paths all consume the same resolved budget.
8. **Add hard gates for formal opportunity claims.** Active opportunity answers
   should require official or primary URLs, deadline/status evidence, geography
   or eligibility evidence when requested, and explicit separation of active,
   adjacent, and rejected candidates.
9. **Keep deterministic helpers focused on verification, not phrase routing.**
   Regex and deterministic routing are valuable for safety, side-effect
   blocking, counts, dates, and hard filters. New diverse-query behavior should
   prefer schema/context fields and bounded tools over adding more
   phrase-specific branches.
10. **Return blockers as useful answers.** For missing context, no exact
   results, weak sources, unavailable referenced links, or unapproved writes, the
   precise response should be a clear partial/blocker answer with the next safe
   action, not a broadened substitute answer.

Practical next step: add the ask-shape fields and typed source-triage object
first. Those two changes would improve diverse-query adaptability without
increasing live tool cost, and they would give later evals a clearer contract to
measure.

## Context Sources To Make Explicit

When adding or improving an agent, decide which of these context surfaces it
needs and update the registry, tests, and prompts together.

| Context surface | Current owner | Recommended contract |
| --- | --- | --- |
| Operator/project policy | `AGENTS.md`, shared prompts | Prompt context only; do not turn into tools. |
| WorkItem state | `schemas/work_item.py`, `work_items.py`, `workflow_runner.py` | Typed context packs; do not pass loose summaries when a pack exists. |
| Slack selected thread context | `slack_actions.py`, WorkItem metadata | Bounded JSON context file plus scoped SDK session. |
| Agent memory | `memory.py`, `tools/memory_tool.py` | Retrieval tool plus approval state and prompt-safety flags. |
| Local docs/Zotero/cache | `tools/local_context_tool.py`, Zotero helpers | Read-only tools with source refs and path bounds. |
| Web search and page extraction | `SearchProvider`, `website_extraction_tool.py`, `html_review_tool.py` | Live-gated retrieval metadata plus source-backed claims. |
| Airtable finance/tax base | `tools/internal_data_tools.py`, `schemas/airtable.py` | Schema-first read tools; writes require typed fields, live flags, and approval reference. |
| Gmail | `tools/gmail_tool.py` | Read, label, and draft-only tools; never send. |
| Google Workspace | `tools/internal_data_tools.py` | Scoped KNIOps tools; live writes require approval reference. |
| Slack posting | `tools/slack_tool.py`, `operations_publisher_tool.py` | Internal review or dry-run post plans unless channel policy and approval allow live post. |
| OpenAI file search | `file_search.py`, `file_search_corpus.py` | Explicit vector store configuration; approved corpus only. |
| Sandbox workspace review | `sandboxing.py`, `docs/SANDBOX_AGENTS.md` | Optional draft-artifact workspace review, never a live side-effect surface. |

## What To Add Next

High-value additions:

1. **Context source catalog tests.** Add a small registry-style test that every
   new context surface has an owner module, allowed agent list, live flags,
   approval notes, and at least one validation path. This prevents hidden
   context injection as the agent set grows.
2. **Agent-as-tool wrappers for bounded synthesis.** The SDK docs distinguish
   handoffs from agents-as-tools. Keystone already has handoff metadata, but
   manager-style flows such as Orchestrator review or Chief of Staff synthesis
   could expose selected specialists as tools when the manager must own the
   final answer.
3. **SDK interruption persistence evaluation.** Keystone has its own approval
   queue and dry-run write gates. If live SDK `needs_approval` tools are adopted
   later, persist SDK interruption state beside existing approval records rather
   than starting a new run after review.
4. **Tool-search readiness, not immediate adoption.** Keystone has several large
   tool surfaces, especially Chief of Staff. Tool search or namespaces may be
   useful after the installed SDK baseline and target models support it in the
   project environment. Until then, keep explicit tool lists and tool policies.
5. **Trace review fields in agent run logs.** The SDK guidance emphasizes traces
   for tool calls, handoffs, MCP calls, errors, and timing. Keystone already logs
   safe run metadata; a future improvement should capture non-sensitive trace ids
   and handoff/tool-call summaries when available.

Lower-value or premature additions:

- A new framework layer around LangGraph, CrewAI, or LangChain. The repo already
  has optional LangGraph WorkItem graph execution; specialists should remain SDK
  agents.
- Broad dynamic tool loading before there is a measurable tool-schema token or
  routing problem.
- Live provider MCP wrappers for APIs that already have reviewed direct tools,
  unless the MCP boundary gives better permission scoping or cross-client reuse.

## MCP Decision

Keystone does not need a mandatory MCP server now.

Use ordinary SDK function tools when:

- the provider boundary is already owned by Keystone code;
- tests need dry-run fixtures and direct monkeypatching;
- credentials and approvals are Keystone-specific;
- tool outputs must be shaped into Keystone schemas before the model sees them.

Use hosted MCP when:

- a public remote MCP server is the actual product interface;
- the server fits OpenAI's hosted trust model;
- exposing the MCP tool through the model surface is safer or simpler than a
  custom adapter.

Use local or private MCP when:

- another runtime already exposes a stable MCP server;
- Keystone should consume that server without importing provider SDKs;
- permission scoping, audit logs, and approval boundaries can be enforced at the
  MCP server boundary;
- tests can run with a fake or dry-run MCP server.

Candidate future MCP boundaries:

- **Read-only local repository context.** A filesystem or Git MCP server could
  standardize scoped repo inspection, but Keystone already has local context
  tools and sandbox previews. Add MCP only if the same context must be shared
  with non-Keystone clients.
- **External knowledge stores.** A private MCP server can be useful if Airtable,
  Google Drive, Zotero, or a future CRM should expose a common search/fetch
  contract to multiple agents and clients. It must preserve Keystone's
  approval/write gates.
- **Codex or sandbox review.** A Codex MCP server can be useful for long-running
  code/document review, but should stay outside the business-agent live side
  effect path.

Do not add MCP for:

- Gmail sending, Slack posting, Airtable writes, calendar writes, or CRM writes
  until the approval lifecycle is implemented and tested at the MCP boundary.
- Placeholder providers such as Apify or Browserless. Those need reviewed direct
  adapters first.

## Public Implementation Lessons

The customer-service demo reinforces a triage-plus-specialists pattern: keep
agents focused, route to specialists, and make guardrails visible in the runtime.
For Keystone, that supports the current Orchestrator plus specialist registry,
but it also argues against deterministic route branches becoming the only way to
interpret natural-language asks.

The portfolio collaboration example uses a hub-and-spoke manager with agents as
tools and mixes custom tools, hosted tools, and MCP. For Keystone, this suggests
using agents-as-tools for bounded internal synthesis when the manager should own
the final answer, while reserving handoffs for direct specialist ownership.

The Python SDK examples and docs emphasize tool guardrails, sessions, MCP
transports, and approval interruption state. Keystone already has local tool
guardrails and SDK sessions; the missing piece to evaluate later is whether SDK
native approval interruptions should be persisted into the existing approval
queue.

## Review Checklist For Future Agent Changes

Before adding a new agent capability, answer:

1. Does this belong in an existing specialist, a new specialist, or a manager
   calling a specialist as a tool?
2. What is the structured input/output schema?
3. Which context surfaces does the agent need, and are they explicit tools or
   typed context packs?
4. Which live flags and credentials are required?
5. Which tool policy entries and safety notes change?
6. Which writes, posts, sends, or external-use actions require human approval?
7. Which eval or pytest path proves the new behavior?
8. Would MCP materially improve provider isolation, cross-client reuse, or
   permission scoping? If not, use direct tools.
