# Agents SDK Conformance Notes

This repository follows the OpenAI Agents SDK structure by keeping each SDK
concept in an explicit local boundary.

## SDK Concept Map

| Agents SDK concept | Keystone location |
| --- | --- |
| Agent definitions | `src/keystone_agents/agents/` |
| Shared SDK wrapper | `src/keystone_agents/sdk.py` |
| Live SDK integration bridge | `src/keystone_agents/run.py`, `docs/LIVE_SDK_INTEGRATION.md` |
| Instructions and prompt fragments | `src/keystone_agents/prompts/` |
| Shared pre-run context | `AGENTS.md`, `src/keystone_agents/prompts/memory_policy.md` |
| Function tools and integration boundaries | `src/keystone_agents/tools/` |
| Structured output types | `src/keystone_agents/schemas/` |
| Guardrails and approval checks | `src/keystone_agents/guardrails.py`, `src/keystone_agents/schemas/approval.py` |
| Handoffs and orchestration | `src/keystone_agents/agents/orchestrator.py` |
| Results, local state, and audit storage | `src/keystone_agents/run.py`, `src/keystone_agents/storage/` |
| Optional LangGraph WorkItem orchestration | `src/keystone_agents/langgraph_workflow.py`, `docs/LANGGRAPH_OPTION.md` |
| Evals and regression tests | `tests/`, `tests/evals/`, `evals/` |
| Future improvement test pack | `docs/AGENT_IMPROVEMENT_TEST_PACK.md` |
| MCP servers | Not added yet; use only when a live provider needs an MCP boundary |
| Optional sandbox agent scaffolding and execution wrapper | `src/keystone_agents/sandboxing.py`, `docs/SANDBOX_AGENTS.md` |

## Structural Rules

- Agent modules expose `build_*_agent()` functions that return SDK agents.
- Agent builders load instructions from markdown files instead of embedding long
  prompts in Python.
- Specialist agents include `handoff_description` so orchestrators have SDK-native
  delegation hints.
- Tool modules own external integration boundaries. Agent modules compose tools.
- Tools default to dry-run or local fixture behavior. Live paths require explicit
  flags and credentials.
- Live LLM synthesis uses the retrieve, normalize, SDK synthesize, validate, then
  save/audit sequence documented in `docs/LIVE_SDK_INTEGRATION.md`.
- Structured outputs are Pydantic models under `schemas/`.
- Guardrails run at the SDK agent level and, for direct local tool calls, through
  guarded function wrappers.
- Storage and approval state are local-first and do not create send, schedule, or
  publish side effects.
- LangGraph is an optional orchestration wrapper around WorkItems. It must call
  existing typed runners and preserve SDK specialists as the behavior boundary.
- Sandbox execution is explicit. `run_sandbox_workspace_review(...)` defaults to
  setup preview, and real execution requires `execute=True` with either
  credential-gated `live=True` or an injected fake/local runner for tests.

## 2026 SDK Review Follow-Up

The current SDK and public-implementation review lives in
`docs/AGENTS_SDK_REVIEW.md`. Use it before adding new context sources, provider
adapters, manager-style agent-as-tool calls, or MCP integrations.

The short decision is:

- direct SDK function tools remain the default for Keystone-owned providers;
- WorkItem context packs remain the default for durable local workflow state;
- agents-as-tools are preferred when a manager agent must retain the final
  response while asking a specialist for a bounded subtask;
- MCP is optional and should be added only when it gives a real external tool or
  data boundary, cross-client reuse, or stronger permission scoping than local
  function tools.

Do not add an MCP server merely because a provider exists. Add one only with a
reviewed schema, dry-run test server or fixture, explicit live flags, credential
checks, source attribution rules, and approval gates for any write-capable tool.

## Naming Notes

`skills.md` and `tools.md` are instruction fragments, not separate SDK primitives.
Keystone skills are prompt-only reasoning and output patterns unless
an existing explicit runtime capability surface is used. Do not add hidden skill
routing, dynamic tool attachment, or implicit live integrations behind a skill
name.

The SDK-native surface remains agent `instructions`, attached `tools`,
`handoffs`, `output_type`, guardrails, WorkItem gates, and tracing-ready
metadata. If a future change needs runtime behavior, represent it through those
surfaces and update the `AgentSpec` registry, tests, and documentation.
