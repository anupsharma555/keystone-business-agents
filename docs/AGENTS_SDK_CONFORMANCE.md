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
| Repo-local reasoning skills | `src/keystone_agents/skills/*/SKILL.md` |
| Shared pre-run context | `AGENTS.md`, `src/keystone_agents/prompts/memory_policy.md` |
| Function tools and integration boundaries | `src/keystone_agents/tools/` |
| Structured output types | `src/keystone_agents/schemas/` |
| Guardrails and approval checks | `src/keystone_agents/guardrails.py`, `src/keystone_agents/schemas/approval.py` |
| Semantic execution authority | `src/keystone_agents/authority/semantic.py`; public facade: `src/keystone_agents/semantic_execution.py` |
| Bounded compatibility planning | `src/keystone_agents/planning/compatibility.py`; public facade: `src/keystone_agents/manual_request.py` |
| Capability admission | `src/keystone_agents/capabilities/profile.py`; public facade: `src/keystone_agents/capability_profile.py` |
| Agent-owned decision policy and evidence binding | `src/keystone_agents/agent_decision_policy.py`, `src/keystone_agents/agent_decision_contracts.py`, `src/keystone_agents/runtime/decision_validation.py` |
| Tool-use postconditions and origin accounting | `src/keystone_agents/runtime/tool_execution.py` |
| Cross-provider context selection and handoff | `src/keystone_agents/runtime/provider_context.py` |
| Mutation receipts, idempotency, and provider recovery | `src/keystone_agents/receipts/`; public facades: `src/keystone_agents/provider_recovery.py`, `src/keystone_agents/tool_receipt_journal.py` |
| Request-scoped runtime composition | `src/keystone_agents/runtime/request.py` |
| Handoffs and orchestration | `src/keystone_agents/agents/orchestrator.py` |
| Public executable-stage boundary | `src/keystone_agents/orchestration/stages.py`; execution kernel: `src/keystone_agents/workflow_runner.py` |
| Results, local state, and audit storage | `src/keystone_agents/run.py`, `src/keystone_agents/storage/` |
| Execution attempts, telemetry, and trace-safe decision evidence | `src/keystone_agents/runtime/execution_attempt.py`, `src/keystone_agents/execution_telemetry.py`, `src/keystone_agents/runtime/decision_trace_harness.py`, `src/keystone_agents/trace_processor.py` |
| Public-result assembly and rendering | `src/keystone_agents/presentation/`; public facades: `src/keystone_agents/reporting.py`, `src/keystone_agents/terminal_result_consistency.py` |
| CLI entrypoint | Public facade: `src/keystone_agents/cli.py`; implementation: `src/keystone_agents/entrypoints/cli_impl.py` |
| Optional LangGraph WorkItem orchestration | `src/keystone_agents/langgraph_workflow.py`, `docs/LANGGRAPH_OPTION.md` |
| Evals and regression tests | `tests/`, `evals/` |
| Future improvement test pack | `docs/AGENT_IMPROVEMENT_TEST_PACK.md` |
| MCP servers | Not added yet; use only when a live provider needs an MCP boundary |
| Optional sandbox agent scaffolding and execution wrapper | `src/keystone_agents/sandboxing.py`, `docs/SANDBOX_AGENTS.md` |

## Structural Rules

- Agent modules expose `build_*_agent()` functions that return SDK agents.
- Agent builders load instructions from markdown files and declared repo-local
  skill bundles instead of embedding long prompts in Python.
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
- Supported top-level compatibility modules remain stable import surfaces, but
  new semantic and business logic belongs in the canonical implementation
  packages listed above.
- Orchestrator preflight is the first model control-plane step for
  natural-language Slack, CLI, WorkItem, scheduled-automation, and explicit
  named-agent paths. Its validated internal semantic decision and the raw
  request must reach the specialist; the public preflight may remain compact.
- Model-owned selections must be bound to evidence that was actually visible
  in the same model loop. Python may normalize before the turn and validate or
  request bounded repair afterward, but it may not add missing substantive
  evidence or silently substitute a different selection.
- Tool attachment is capability, not proof of execution. Runtime summaries keep
  model requests, attached tools, actual SDK model tool calls and outputs,
  workflow-called tools, deterministic helpers, pre-acquired context, provider
  attempts, successes, and receipts as separate fields. Compatibility
  `tool_mode=model_called` decision events do not establish a tool call alone.
- A missing required tool result or invalid decision fails closed or, where the
  shared wrapper is wired, enters bounded recovery. Completed provider
  mutations are never repeated merely to repair output or decision shape.
- Runtime integration is not uniform across the registry. Current direct CLI
  paths own Airtable, Workspace, and Zotero execution outside
  `WorkflowRunner`, including bounded missing-tool correction and semantic
  repair; RSS and Preprints use the signal runtime; nested Chief context
  specialists use validated child wrappers over agent tools rather than the
  direct wrapper. Registry or trace-harness presence alone does not prove one
  identical validation/repair envelope.
- Live Business Research and Opportunity WorkItems use agent-owned SDK
  retrieval, selection, ranking, and handoff over model-visible stable provider
  candidate IDs. Their one semantic repair replays the same evidence tool-free;
  Python validates identity, bounds, and formal gates rather than selecting a
  replacement.
- LangGraph is an optional graph runtime around WorkItems. It must call existing
  typed workflow utilities, carry Orchestrator preflight context, and preserve
  SDK specialists as the behavior boundary.
- Sandbox execution is explicit. `run_sandbox_workspace_review(...)` defaults to
  setup preview, and real execution requires `execute=True` with either
  credential-gated `live=True` or an injected fake/local runner for tests.

## Architecture Fitness Check

`tests/test_architecture.py` keeps SDK execution on a small, reviewed allowlist.
New modules may not call `Runner.run`, `Runner.run_sync`, `run_sdk_sync`, or
`run_typed_sdk_sync` directly. The central SDK wrapper, typed live-synthesis
bridge, legacy CLI adapter, and separately gated sandbox boundary are the current
accepted exceptions. Add an allowlist entry only when a new execution boundary
is intentional, documented, cost/trace aware, and covered by focused tests; do
not update the allowlist merely to silence a failing architecture check.

Run the check locally without credentials or network access:

```bash
.venv/bin/python -m pytest tests/test_architecture.py -q
```

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

`skills.md` is now a compatibility instruction fragment for legacy or auxiliary
agents that have not yet migrated to repo-local skill bundles. Registered
Keystone business agents declare reusable capabilities in
`src/keystone_agents/skills/<skill_id>/SKILL.md`; see
`docs/SKILLS_ARCHITECTURE.md`.

Keystone skills are bounded reasoning and output contracts, not separate SDK primitives.
They are not tool calls, approval gates, hidden routers, dynamic tool attachment
paths, or live integrations. Agent builders and the `AgentSpec` registry declare
which skills are part of an agent's static instruction surface. Tool access,
approval checks, live flags, source sufficiency, and schema validation remain in
explicit SDK tools, guardrails, Python gates, and Pydantic schemas.

The SDK-native surface remains agent `instructions`, attached `tools`,
`handoffs`, `output_type`, guardrails, WorkItem gates, and tracing-ready
metadata. If a future change needs runtime behavior, represent it through those
surfaces and update the `AgentSpec` registry, tests, and documentation.
