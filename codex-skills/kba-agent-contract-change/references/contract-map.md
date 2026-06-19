# KBA Agent Contract Map

Load this reference when changing Keystone SDK agent contracts in
`<repo>`.

## Ownership Map

- Agent builders: `src/keystone_agents/agents/*.py`.
- SDK wrapper and instruction composition: `src/keystone_agents/sdk.py`.
- Registry and agent cards: `src/keystone_agents/agent_registry.py`.
- Tool surfaces and tool tiers: `src/keystone_agents/agent_tool_policy.py`.
- Tool wrappers: `src/keystone_agents/tools/`.
- Prompts: `src/keystone_agents/prompts/*.md`.
- Runtime skill contracts: `src/keystone_agents/skills/*/SKILL.md`.
- Skill selection: `src/keystone_agents/skill_sets.py`.
- Structured outputs: `src/keystone_agents/schemas/`.
- Context packs: `src/keystone_agents/schemas/context_pack.py` and
  route-specific builders/tests.
- WorkItems and lifecycle: `src/keystone_agents/work_items.py`,
  `src/keystone_agents/workflow_runner.py`.
- Orchestrator preflight: `src/keystone_agents/orchestrator/preflight_context.py`.

## Common Change Lanes

### New or Changed Agent

1. Add or update the Pydantic output schema.
2. Add or update the markdown prompt with metadata.
3. Expose or update `build_*_agent()` through `build_sdk_agent()`.
4. Attach only explicit tool wrappers and approved tool tiers.
5. Preserve deterministic fixture mode and live flags.
6. Update handoff contracts when another agent consumes the output.
7. Update `AgentSpec` with builder, schema, prompts, tools, live flags, skills,
   eval paths, handoff description, and safety notes.
8. Add focused tests or eval rows before broad docs cleanup.

### Runtime Skill Contract Change

1. Edit the relevant `src/keystone_agents/skills/<skill_id>/SKILL.md`.
2. Keep the skill as a reasoning/output contract, not a tool or permission grant.
3. Update `src/keystone_agents/skill_sets.py` only when selection should change.
4. Update selector, matrix, or local eval rows when trigger behavior changes.
5. Validate that unrelated specialist skills remain absent from compact runs.

### Tool or Provider Boundary Change

1. Put provider integration logic in `src/keystone_agents/tools/` or shared
   provider modules.
2. Keep agent modules as tool composers.
3. Add dry-run behavior and explicit live flags.
4. Add credential checks before live model or provider calls.
5. Add source attribution and safety gates where facts or external writes are involved.

### Chief of Staff Specialist Context Change

Use this lane when adding nested specialist capability, provider call metadata,
or richer operator intent preservation:

1. Inspect `src/keystone_agents/specialist_agent_tools.py`, the relevant
   specialist prompt, and the Chief of Staff builder.
2. Preserve explicit context lanes such as `decision_context`,
   `target_context`, `coordination_context`, and `provider_call_context`.
3. Pass exact identifiers, date windows, folder paths, object IDs, approval
   references, and provider hints as structured context instead of broad prompt
   prose.
4. Refresh prompt-fingerprint or prompt-contract tests when prompt guidance
   changes.
5. Keep nested specialist tools advisory. Do not let them bypass approval,
   source, or action gates.

### Prompt Surface or Architecture Review Change

Use this lane when updating architecture docs or instruction surfaces:

1. Keep `docs/AGENTS_SDK_REVIEW.md` as the deeper implementation-facing review.
2. Keep `docs/REPO_REVIEW.md` as the concise snapshot review.
3. Preserve the distinction between repo policy (`AGENTS.md`), runtime prompts,
   runtime skill bundles, tools, schemas, gates, and tests.
4. Avoid duplicating the same rule across every layer unless tests require it.

## Focused Validation

Run the narrowest relevant commands first:

```bash
.venv/bin/python -m pytest tests/test_agent_registry.py tests/test_agent_architecture.py
.venv/bin/python -m pytest tests/test_prompt_contracts.py tests/test_skill_contract_gates.py tests/test_skill_evals.py
.venv/bin/python scripts/run_skill_task_matrix.py --json
.venv/bin/python scripts/run_local_evals.py --dataset skill_task_matrix --json
```

For route-specific work, add the matching focused tests:

```bash
.venv/bin/python -m pytest tests/test_orchestrator.py tests/test_orchestrator_preflight_context.py
.venv/bin/python -m pytest tests/test_business_research_analyst.py tests/test_opportunity_scout.py
.venv/bin/python -m pytest tests/test_gmail_triage.py tests/test_outreach_composer.py
.venv/bin/python -m pytest tests/test_chief_of_staff.py tests/test_handoff_contracts.py
```

For broad completion:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

## Stop Conditions

Stop and ask or report a blocker when:

- The user asks for a live write, send, post, schedule, or external publish
  without explicit scoped approval.
- Required source identity, approval scope, recipient identity, or record
  identity is missing.
- A live provider path lacks credentials, live flags, dry-run fallback, or tests.
- A requested behavior would need phrase-specific deterministic routing instead
  of a general schema/tool/context contract.

## Prior Failure Anchors

- Downstream provider reads broadened into generic guesses when exact user
  intent was not preserved as structured `provider_call_context`.
- Review work drifted between `docs/AGENTS_SDK_REVIEW.md` and
  `docs/REPO_REVIEW.md`; inspect the file map before editing review docs.
- Live SDK smoke tests can hit the wrong project if they assume a generic
  `OPENAI_API_KEY`; use `KEYSTONE_OPENAI_API_KEY` for this repo and never print
  the value.
