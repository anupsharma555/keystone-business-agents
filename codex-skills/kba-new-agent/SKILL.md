---
name: kba-new-agent
description: Repo-local workflow for <repo>. Use only in this repo when creating, shaping, or integrating a novel Keystone Business Agents agent, including workflow specialists, read-only or advisory context agents, Chief-of-Staff callable specialists, workflow-specific AgentSpec entries, runtime skill bundles, context packs, handoff contracts, tool policies, eval matrix coverage, and validation tests while preserving LLM reasoning inside the agent path.
---

# KBA New Agent

## Scope

Use this skill only for `<repo>`. If the working directory is
not this repo, stop and do not apply these instructions.

This is a repo-local Codex skill for creating new Keystone Business Agents
agents. Pair it with `codex-skills/kba-agent-contract-change/SKILL.md`
whenever code, prompts, schemas, tools, context packs, registry metadata, or
tests will change.

## First Reads

Read the smallest useful set before editing:

- Always: `docs/ADD_AGENT.md`, `src/keystone_agents/agent_registry.py`,
  `src/keystone_agents/skill_sets.py`.
- Tool access or side effects: `src/keystone_agents/agent_tool_policy.py` and
  the relevant files under `src/keystone_agents/tools/`.
- Chief-of-Staff callable agent: `src/keystone_agents/specialist_agent_tools.py`
  and `src/keystone_agents/schemas/chief_of_staff.py`.
- WorkItem or route execution: `src/keystone_agents/work_items.py`,
  `src/keystone_agents/workflow_runner.py`, and
  `codex-skills/kba-workitem-orchestrator-ops/SKILL.md`.
- Search or extraction agent: `codex-skills/kba-search-provider-eval/SKILL.md`.
- Live SDK/model smoke work: `codex-skills/kba-live-sdk-smoke-and-cost/SKILL.md`.
- Detailed agent pattern checklist: `references/agent-patterns.md`.

## Request Shapes

Start by naming the proposed agent family and the owning boundary:

- `workflow specialist`: owns a business workflow such as research, opportunity
  scouting, triage, drafting, review, or artifact creation.
- `context specialist`: reads or organizes one source system for Chief of Staff
  or another specialist, and returns bounded handoff context with blockers,
  source refs, and human work context.
- `control-plane agent`: plans, routes, reviews, or coordinates other agents.
  Treat this as high-blast-radius; prefer backlog/design first unless the user
  explicitly asks to implement.

If the family, side-effect permissions, source system, or intended caller is
ambiguous, ask the minimum clarification before implementing.

## Adaptation Rules

Design the agent around the workflow it must actually perform. Before touching
files, identify the expected operator surface, source systems, context depth,
handoff consumers, renderer needs, live-provider budget, side-effect boundary,
and failure/blocker modes.

Shape `AgentSpec` to that workflow instead of copying a generic registry entry.
The spec should accurately declare the builder, input schema, output schema,
prompt files, runtime skills, required and optional tools, live flags, eval
datasets, validation paths, handoff description, and safety notes that the
agent truly needs.

Preserve agent reasoning with the LLM. Use deterministic Python for exact
gates, validation, identity resolution, arithmetic, source sufficiency, write
blocking, and fixture behavior, but keep natural-language interpretation, tool
selection over bounded tools, source synthesis, recommendations, and
operator-facing explanation in the agent's LLM path when live or SDK synthesis
is requested.

## Workflow

1. Define the contract before code.
   Capture route name, human name, primary caller, input contract, structured
   output schema, source/tool boundaries, live flags, approval gates, and the
   first fixture-safe validation path.

2. Reuse existing surfaces.
   Prefer existing schemas, context packs, shared skills, tool wrappers, and
   registry patterns. Add a new abstraction only when the new agent has a
   durable contract that existing agents cannot cleanly represent.

3. Implement the smallest complete slice.
   A new agent is complete only when it has a Pydantic output model, markdown
   prompt with metadata, `build_*_agent()` SDK builder, explicit tool list,
   dry-run fixture behavior where applicable, runtime skill bundle, `AgentSpec`,
   tool policy coverage where relevant, and focused tests or eval coverage.

4. Preserve Orchestrator-first execution.
   Natural-language entrypoints must keep raw operator wording, Orchestrator
   preflight, Python safety gates, typed context packs, specialist execution,
   and review before rendering. Explicit agent mentions are routing advice, not
   permission to bypass gates.

5. Keep context agents context-first.
   A context specialist should inspect schemas or bounded source context first,
   return organized handoff context, and block on ambiguous identity or approval
   scope. Nested context specialists are advisory; Chief of Staff or the direct
   selected agent owns any approved write path.

6. Avoid phrase-specific lanes.
   Do not create deterministic branches for one natural-language wording. If a
   task recurs, expose bounded schema/context and typed tools, then let the
   agent map the request. Add deterministic helpers only for routing hints,
   validation, arithmetic, identity, approval, source sufficiency, or write
   blocking.

7. Validate by contract surface.
   Run narrow tests for the touched schema, builder, prompt metadata, registry,
   tool policy, context pack, eval matrix, and safety gate before broad tests.

## Required Surfaces

For every new agent, check these surfaces and edit only the ones that apply:

- `src/keystone_agents/schemas/`: Pydantic output and any input/context schema.
- `src/keystone_agents/prompts/`: markdown prompt with metadata.
- `src/keystone_agents/agents/`: `build_*_agent()` using
  `compose_instructions()` and `build_sdk_agent()`.
- `src/keystone_agents/tools/`: explicit wrappers for provider or local data
  boundaries.
- `src/keystone_agents/skills/<skill_id>/SKILL.md`: runtime reasoning/output
  contract, not a permission grant.
- `src/keystone_agents/skill_sets.py`: shared and agent-specific skills.
- `src/keystone_agents/agent_registry.py`: `AgentSpec` with builder, schema,
  prompts, skills, tools, live flags, evals, validation paths, handoff text,
  and safety notes customized to the agent's workflow.
- `src/keystone_agents/agent_tool_policy.py`: allowed tool tiers and source
  layer policy when new tools or side-effect classes are introduced.
- `evals/local/skill_task_matrix.jsonl`: at least one representative matrix
  case for the new route when skill selection applies.
- `tests/`: focused coverage for registry, builder, schema, tool policy,
  context packs, fixtures, and safety gates.

## Stop Rules

Stop and ask or document a validation plan when:

- The request implies sending, posting, publishing, scheduling, deleting,
  schema-changing, or bulk-writing without explicit approval scope.
- Source identity, record identity, collection/folder/table identity, or write
  target is ambiguous.
- A live provider is requested without explicit live flags, credential checks,
  budget controls, and dry-run tests.
- The proposed agent would duplicate an existing specialist, context agent, or
  deterministic helper without a clear contract gap.

## Verification

For this skill itself, run:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py codex-skills/kba-new-agent
```

For future agent implementations, prefer:

```bash
.venv/bin/python -m pytest tests/test_agent_registry.py tests/test_agent_architecture.py
.venv/bin/python -m pytest tests/test_skill_evals.py
.venv/bin/python -m ruff check .
```

Add narrower tests before those broad checks whenever the new agent touches a
specific tool, context pack, live flag, safety gate, or renderer.
