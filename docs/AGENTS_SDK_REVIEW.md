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
  has an optional LangGraph wrapper for WorkItems; specialists should remain SDK
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
