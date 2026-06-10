# Add A New Agent

Use this checklist when adding a new Keystone SDK agent. Keep the first change small and
fixture-safe.

## Checklist

1. Add or extend a Pydantic output schema under `src/keystone_agents/schemas/`.
2. Add a markdown prompt under `src/keystone_agents/prompts/` with metadata for name, version,
   purpose, safety notes, and eval datasets.
3. Add a `build_*_agent()` function that returns an SDK `Agent` through `build_sdk_agent()`.
4. Load instructions through `compose_instructions()` and include shared safety, tools,
   declared repo-local skills, and Keystone profile context.
5. Add deterministic fixture mode before live execution.
6. Use explicit tool wrappers. Do not place live integration logic directly in the agent module.
7. Require explicit live flags for every live integration path.
8. Add a handoff contract if another agent consumes this output.
9. Add at least one eval dataset or a documented validation path.
10. Register the agent in `src/keystone_agents/agent_registry.py`.
11. Add architecture coverage that proves the registry entry has a builder, schema, prompts,
    safety notes, and eval or validation coverage.
12. Update docs only after the fixture path and tests pass.

## Registry Fields

Each `AgentSpec` should include:

- route name and human agent name
- builder import path
- output schema import path
- prompt files
- declared skills
- supported tool names
- required live flags
- eval datasets and validation paths
- handoff description
- safety notes

The orchestrator derives specialist handoff metadata from the registry, so keep registry
descriptions short, stable, and operator-readable.

## Validation

Run the narrow tests first, then the suite:

```bash
.venv/bin/python -m pytest tests/test_agent_registry.py tests/test_agent_architecture.py
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```

Do not mark a new agent complete until dry-run behavior, prompt metadata, safety gates, and eval
or validation coverage are all present.

## Skill Bundles

Use `src/keystone_agents/skills/<skill_id>/SKILL.md` for reusable reasoning and
output contracts shared across agents. Skills may describe required behavior,
flexible behavior, boundaries, output contracts, failure modes, and evals. They
must not attach tools, grant permissions, select hidden routes, or replace
Python approval/source/action gates.
