# KBA WorkItem And Orchestrator Flow

Load this reference when operating or debugging `@KNI`, WorkItems, context
packs, approvals, Orchestrator routing, or Slack action continuations in
`<repo>`.

## Canonical State Model

Canonical business state lives in:

- WorkItems and artifacts.
- Local SQLite storage and audit rows.
- Context packs.
- Approval records and approval queue items.
- Source refs, blockers, and timeline events.
- Rendered Slack/CLI/report outputs.

Local Agents SDK sessions are optional conversation continuity. Do not describe
them as the canonical business state unless the repo architecture changes.

## Entrypoint Map

- Codex `@KNI` shorthand:
  `.venv/bin/python scripts/ask_agent.py @KNI <agent words> "<request>"`.
- Generic stateful ask path: `scripts/ask_agent.py` and
  `src/keystone_agents/cli.py`.
- Orchestrator preflight: `src/keystone_agents/agents/orchestrator.py` and
  `src/keystone_agents/orchestrator/preflight_context.py`.
- WorkItem advancement: `src/keystone_agents/workflow_runner.py`.
- Optional LangGraph orchestration: `docs/LANGGRAPH_OPTION.md`,
  `src/keystone_agents/langgraph_workflow.py`,
  `src/keystone_agents/langgraph_plan.py`.
- WorkItem schemas/state: `src/keystone_agents/work_items.py`,
  `src/keystone_agents/schemas/work_item.py`.
- Context packs: `src/keystone_agents/schemas/context_pack.py` and route-specific
  pack builders.
- Slack actions: `src/keystone_agents/slack_actions.py`,
  `src/keystone_agents/slack_action_contract.py`,
  `scripts/handle_slack_agent_action.py`.
- Approval scripts: `scripts/list_approvals.py`, `scripts/update_approval.py`,
  `scripts/handle_slack_approval_action.py`.

## Safe Commands

Dry-run natural-language ask:

```bash
.venv/bin/python scripts/ask_agent.py --input "research Curebase" --agent orchestrator --json --no-live-sdk
```

Explicit named-agent shorthand:

```bash
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health"
```

List pending approvals:

```bash
.venv/bin/python scripts/list_approvals.py
```

Run focused local evals around workflow behavior:

```bash
.venv/bin/python scripts/run_local_evals.py --dataset orchestrator_routing --json
.venv/bin/python scripts/run_local_evals.py --dataset skill_task_matrix --json
```

## Debugging Checklist

1. Capture raw request, selected Slack/thread context, and entrypoint flags.
2. Inspect Orchestrator preflight route, blockers, safety notes, and memo.
3. Inspect WorkItem route, next action, artifact refs, approval state, and
   timeline events.
4. Inspect selected context pack and deterministic readiness gate result.
5. If LangGraph is enabled, inspect graph backend selection and node events
   while keeping WorkItems as the state contract.
6. Inspect specialist output schema and Orchestrator/deterministic review.
7. Inspect final renderer output for visible sources, no-send/no-post language,
   and absence of raw trace noise.

## Prior Failure Anchors

- Stale or unrelated prior context can override a current Slack request if raw
  operator wording and Orchestrator review are not preserved.
- Direct send/post/share wording must route to approval gates and remain
  draft-only or blocked without scoped approval.
- State-only Slack follow-ups can still incur SDK cost through preflight or
  final synthesis; inspect `workflow_sdk_usage` rather than only `agent_runs`.
- Context-pack loss causes specialists to receive loose summaries; prefer typed
  `ResearchContextPack`, `OpportunityContextPack`, `OutreachContextPack`, and
  `GmailContextPack`.
- LangGraph is a backend orchestration option, not a separate permission model;
  route, approval, source, and write gates must match the normal WorkItem path.

## Focused Tests

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/test_cli_work_items.py
.venv/bin/python -m pytest tests/test_manual_request_plan.py tests/test_context_packs.py
.venv/bin/python -m pytest tests/test_workflow_runner.py tests/test_work_item_schemas.py tests/test_work_item_storage.py
.venv/bin/python -m pytest tests/test_orchestrator.py tests/test_orchestrator_preflight_context.py
.venv/bin/python -m pytest tests/test_slack_agent_actions.py tests/test_slack_action_contract.py
```

## Stop Conditions

Stop and ask or report a blocker when:

- The WorkItem ID, Slack thread, selected context, record identity, or approval
  scope needed to continue is missing.
- The user asks to send, post, schedule, publish, or write externally without
  explicit scoped approval.
- The fix would add a one-off phrase shortcut instead of improving the shared
  route/context/gate contract.
