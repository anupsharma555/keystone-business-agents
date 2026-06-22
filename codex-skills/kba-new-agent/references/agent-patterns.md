# Agent Patterns

Use this reference after `SKILL.md` when a task actually creates or reshapes a
new Keystone Business Agents agent in `<repo>`.

## Shared Contract Checklist

Every new agent needs a small, testable contract before implementation:

- Route name: lowercase snake case, stable, and operator-readable.
- Human name: concise display name for agent cards and handoffs.
- Primary caller: CLI, Slack, WorkItem, Chief of Staff, Orchestrator, schedule,
  or direct named-agent run.
- Input contract: existing context pack, `ManualRequestPlan`,
  `ChiefSpecialistToolInput`, or a new typed schema only when necessary.
- Output schema: Pydantic model with bounded fields that renderers and tests can
  consume without parsing markdown.
- Prompt files: shared profile/safety/tools plus one route-specific prompt with
  metadata.
- Runtime skills: shared reasoning contracts plus one route-specific
  `<route>_specialist_contracts` bundle.
- Tools: explicit wrappers only; agent modules compose tools but do not own
  provider SDKs or live network boundaries.
- Live flags: explicit for every live provider, write path, hosted search, SDK
  call, or external integration.
- Handoff: short `AgentSpec.handoff_description` and source-visible safety
  notes.
- Coverage: registry/builder tests, prompt metadata tests, skill matrix case,
  and the smallest route-specific fixture/eval.

## Workflow And AgentSpec Adaptation

Do not treat `AgentSpec` as boilerplate. Let the workflow determine each field:

- `input_schema`: choose the smallest typed input that preserves the caller's
  context. Use existing context packs for WorkItems, `ChiefSpecialistToolInput`
  for Chief-of-Staff nested calls, and new schemas only when existing contracts
  lose required state.
- `output_schema`: include fields renderers and downstream agents need, such as
  source refs, blockers, confidence, approval state, missing context,
  handoff-ready context, human work context, artifact plans, or draft-only copy.
- `prompt_files`: include shared profile/safety/tools and route-specific
  instructions. Add local/search/context prompt fragments only when the workflow
  uses those sources.
- `skills`: choose shared reasoning skills that match the workflow. Context
  agents usually need a smaller skill set than operating workflow specialists.
- `tools` and `optional_tools`: list only wrappers the builder can attach and
  the policy permits. Keep provider SDKs and network boundaries in tool modules.
- `live_flags_required`: declare every live SDK, search, Workspace, Gmail,
  Airtable, Zotero, Slack, publishing, or write boundary the agent can cross.
- `eval_datasets` and `validation_paths`: cover the first real workflow, not
  only importability.
- `handoff_description` and `safety_notes`: write operator-readable text that
  states the actual ownership boundary and stop conditions.

Keep LLM reasoning in the agent flow:

- Let the LLM interpret the raw request, map it onto bounded tools, synthesize
  source-backed findings, explain relevance, choose what evidence is weak, and
  write operator-facing recommendations.
- Let deterministic helpers constrain inputs, compute exact values, enforce
  permissions, block unsafe side effects, and validate structured outputs.
- Do not replace a new agent with a template renderer, canned preview, or
  phrase-specific branch when a general schema-plus-tool contract can handle
  the workflow.

## Workflow Specialists

Use a workflow specialist when the agent owns a business outcome: triage,
research, opportunity discovery, outreach drafting, report generation, approval
review, or another repeatable operating workflow.

Default surfaces:

- Input: prefer `ResearchContextPack`, `OpportunityContextPack`,
  `OutreachContextPack`, `GmailContextPack`, or a route-specific typed pack
  derived from WorkItem state.
- Output: durable business result schema with sources, blockers, confidence,
  next actions, approval state, and renderer-ready summaries.
- Skills: usually include most shared reasoning skills, plus a specialist
  contract.
- Tools: may include web search, local context, memory, approved internal
  reads, deterministic source/identity helpers, and draft-only or approval-queue
  helpers.
- Safety: no outbound send/post/publish by default; live writes require flags
  and approval references.
- Tests: fixture output, source attribution, unsupported-claim handling,
  approval gating, and WorkItem/context-pack handoff when applicable.

Implementation notes:

- Keep natural-language interpretation in the agent, but keep safety, approval,
  exact write scope, arithmetic, and record identity in Python.
- Add or update context pack builders when the specialist consumes WorkItem
  state. Do not hand-roll loose JSON if a typed context pack exists.
- Add Orchestrator routing hints only as schema-light advice; do not add broad
  route taxonomies for every phrasing.
- If another agent consumes the output, add handoff fields and tests for the
  exact fields it needs.

## Context Specialists

Use a context specialist when the agent primarily reads, resolves, ranks, or
packages source-system context for Chief of Staff or a workflow specialist.
Examples include Airtable, Google Workspace, Zotero, RSS history, preprints, or
future CRM/document/repository context lanes.

Default surfaces:

- Input: usually `ChiefSpecialistToolInput` for Chief-of-Staff calls, unless a
  direct selected-agent workflow needs a narrower schema.
- Output: operational context schema with considered sources, identity
  resolution, source refs, handoff-ready context, missing context, blockers,
  human work context, and an optional write/artifact plan.
- Skills: use focused shared skills; do not automatically include the full
  workflow-specialist bundle unless the context source needs it.
- Tools: start with schema/list/read tools. Add write tools only when direct
  selected-agent writes are explicitly supported and guarded.
- Nested behavior: when called as a Chief specialist tool, return advisory
  context and Chief-owned write guidance; do not perform nested live writes.
- Tests: read-only fixture behavior, ambiguous identity blockers, write-plan
  defaults such as `live_write_allowed_for_specialist = False`, and Chief
  nested-output extraction.

Implementation notes:

- Inspect schema or source inventory before reading records or files.
- Return bounded context, not raw dumps.
- Preserve source-system identity fields such as base/table/field, Drive
  file/folder/doc/sheet/tab, Zotero library/collection/item, feed URL, message
  or WorkItem ids.
- State whether evidence is current verification, local historical context, or
  review-only context.
- Ask for clarification rather than previewing an arbitrary first record or
  folder when the target is ambiguous.

## Control-Plane Agents

Use a control-plane agent only when the repo needs a new planning, routing,
review, or coordination layer. Existing Orchestrator and Chief of Staff coverage
should usually be extended before adding another control plane.

Extra requirements:

- Backlog/design note before implementation unless the user explicitly asks for
  code.
- Schema-light planning. Reuse `ManualRequestPlan`, `OrchestratorResult`,
  WorkItems, context packs, audit events, and `decision_trace` where possible.
- Strong tests for route advice, blocker precision, approval preservation, and
  no bypass of Python gates.
- Clear proof that existing deterministic helpers or current control-plane
  agents cannot own the behavior cleanly.

## File Pattern Map

Use current adjacent files as patterns:

- Workflow specialist builder: `src/keystone_agents/agents/business_research_analyst.py`
  or `src/keystone_agents/agents/outreach_composer.py`.
- Context specialist builder: `src/keystone_agents/agents/airtable_context.py`
  or `src/keystone_agents/agents/zotero_context.py`.
- Operational context schemas: `src/keystone_agents/schemas/operational_context.py`.
- Chief nested input/output envelopes:
  `src/keystone_agents/schemas/chief_of_staff.py`.
- Registry examples: `SPECIALIST_AGENT_SPECS` in
  `src/keystone_agents/agent_registry.py`.
- Skill selection: `src/keystone_agents/skill_sets.py`.
- Matrix coverage: `evals/local/skill_task_matrix.jsonl`.

## Minimal Done Definition

Do not mark a new agent complete until:

- The builder returns an SDK `Agent` with declared schema, tools, guardrails,
  prompt instructions, and handoff text.
- The registry can resolve the builder and output schema.
- Prompt and runtime skill metadata load correctly.
- `AgentSpec` metadata matches the implemented builder, schema, prompts, tools,
  live flags, evals, validation paths, handoff description, and safety notes.
- Dry-run or fixture behavior exists for development and tests.
- At least one eval or validation path proves the route can be exercised.
- Source attribution, approval gates, and no-send/no-publish behavior are
  enforced where relevant.
