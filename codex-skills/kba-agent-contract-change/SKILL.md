---
name: kba-agent-contract-change
description: Repo-local workflow for <repo>. Use only in this repo when changing Keystone SDK agents, prompts, Pydantic schemas, tool wrappers, context packs, handoff contracts, runtime skill bundles, AgentSpec registry metadata, model/tool policy, or tests that protect those contracts.
---

# KBA Agent Contract Change

## Scope

Use this skill only when the current repository is
`<repo>`. If the working directory is
not this repo, stop and do not apply these instructions.

Treat this repo as an OpenAI Agents SDK project. Preserve the boundary between:
agents, markdown prompts, Pydantic schemas, explicit tool wrappers, context
packs, deterministic gates, runtime skill bundles under `src/keystone_agents/skills/`,
and Codex-facing skills under `codex-skills/`.

Use `$kba-new-agent` first when the task creates a new agent family. This skill
then owns the concrete contract implementation and regression surfaces.

## First Reads

Read the smallest set that matches the task:

- Agent or registry change: `docs/ADD_AGENT.md`, `src/keystone_agents/agent_registry.py`.
- Runtime skill contract change: `docs/SKILLS_ARCHITECTURE.md`, `src/keystone_agents/skill_sets.py`.
- Architecture-sensitive change: `docs/AGENTS_SDK_REVIEW.md`, then the touched builder, prompt, schema, and tests.
- Detailed file and validation map: `references/contract-map.md`.

## Request Shapes

Use this workflow for requests like:

- "Add a new agent", "change an agent prompt", "update the AgentSpec", or
  "fix a handoff/context-pack bug".
- "Expose this provider/context source to Chief of Staff" or "preserve this
  exact Gmail/Airtable/Zotero/Google Workspace call hint".
- "Change runtime skill selection", "update a Keystone `SKILL.md` contract", or
  "fix prompt contract tests".
- "Wire a live integration flag", "change model/provider defaults", or "add a
  tool wrapper".

## Workflow

1. Start with the requested behavior and identify the owning surface. Do not
   implement prompt-only behavior when a schema, tool wrapper, registry entry,
   context pack, or deterministic gate owns the contract.
2. Inspect the current implementation before editing. Prefer `rg` over broad
   browsing and confirm existing tests for the affected route or agent.
3. Make the smallest maintainable change across all required surfaces:
   schema, prompt, builder, tool policy, live flags, registry, eval data, and
   tests.
4. Preserve dry-run fixture behavior. Do not add live integration behavior
   unless the user explicitly requests it and the repo has explicit live flags,
   credential checks, approval boundaries, and tests.
5. Update `AgentSpec` when prompts, schemas, tools, live flags, skills, eval
   coverage, or handoff descriptions change.
6. Validate narrow tests first. Run broader tests only after the narrow contract
   is clean.

## Decision Rules

- Use explicit tool wrappers under `src/keystone_agents/tools/`; do not import
  provider SDKs directly inside agent modules.
- Keep runtime skills as reasoning and output contracts only. They must not
  attach tools, grant permissions, select hidden routes, or replace Python
  gates.
- Keep Orchestrator-first execution intact for natural-language entrypoints
  unless the task is a narrower deterministic health/status command.
- Ask for clarification when source identity, approval scope, write scope, or
  live-provider intent is missing.
- Do not mark work complete until dry-run behavior, prompt metadata, safety
  gates, and eval or validation coverage are present.
- For ad hoc live SDK smoke tests, use the repo-local `KEYSTONE_OPENAI_API_KEY`
  and map it to `OPENAI_API_KEY` only inside the child process that needs the
  generic variable. Never print secrets.

## Verification

Use focused validation from `references/contract-map.md`. At minimum, run the
tests that cover the modified contract surface. Prefer this sequence for broad
agent contract changes:

```bash
.venv/bin/python -m pytest tests/test_agent_registry.py tests/test_agent_architecture.py
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```
