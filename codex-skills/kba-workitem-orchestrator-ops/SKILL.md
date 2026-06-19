---
name: kba-workitem-orchestrator-ops
description: Repo-local workflow for <repo>. Use only in this repo when operating or debugging @KNI, scripts/ask_agent.py, Orchestrator-first routing, WorkItems, context packs, approval gates, workflow_runner behavior, Slack action continuations, local storage/audit rows, or stateful manual-request flows.
---

# KBA WorkItem Orchestrator Ops

## Scope

Use this skill only when the current repository is
`<repo>`. If the working directory is
not this repo, stop and do not apply these instructions.

Treat WorkItems, local storage, context packs, approval records, audit rows, and
artifacts as canonical business state. Treat local Agents SDK sessions as
optional conversation continuity, not the canonical operating state.

## First Reads

Read the smallest set that matches the task:

- Natural-language or `@KNI` entrypoint: `AGENTS.md`, `README.md`,
  `scripts/ask_agent.py`, `src/keystone_agents/cli.py`.
- Orchestrator preflight or routing: `src/keystone_agents/agents/orchestrator.py`,
  `src/keystone_agents/orchestrator/preflight_context.py`,
  `src/keystone_agents/manual_request.py`.
- WorkItem lifecycle: `src/keystone_agents/work_items.py`,
  `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/schemas/work_item.py`.
- Context packs and gates: `src/keystone_agents/schemas/context_pack.py`,
  `tests/test_context_packs.py`, `tests/test_workflow_runner.py`.
- Slack actions/continuations: `src/keystone_agents/slack_actions.py`,
  `src/keystone_agents/slack_action_contract.py`,
  `scripts/handle_slack_agent_action.py`.
- Command map and failure anchors: `references/workitem-flow.md`.

## Request Shapes

Use this workflow for requests like:

- "Debug this @KNI response", "why did the wrong agent answer?", or "continue
  this WorkItem".
- "Inspect approval state", "why is outreach blocked?", or "what gate stopped
  this run?"
- "Fix Slack action continue/run again/more research/find contact/revise draft".
- "Explain current state", "what is canonical state?", or "inspect WorkItem
  context packs and audit rows".

## Workflow

1. Preserve the original operator wording. Do not rewrite away route, safety,
   approval, time, target, or context constraints before Orchestrator preflight.
2. Identify the entrypoint: dry-run CLI, `@KNI` explicit named-agent CLI,
   WorkItem manager loop, Slack action, scheduled automation, or direct script.
3. Inspect Orchestrator preflight, deterministic gates, selected route, context
   pack type, WorkItem timeline, approval state, and final renderer output.
4. Keep Python gates authoritative for approval, recipient readiness, source
   sufficiency, Gmail thread readiness, external-use approval, and side effects.
5. Keep direct send/post/share wording draft-only or blocked unless a scoped
   approval and live integration path explicitly permits the exact action.
6. When fixing behavior, update tests for the route, context pack, Slack action,
   or WorkItem lifecycle that actually owns the bug.

## Decision Rules

- Explicit agent mentions are routing advice, not permission to bypass
  Orchestrator preflight, specialist ownership, gates, or review.
- Do not add broad intent taxonomies or phrase-specific shortcuts for normal
  natural-language requests. Prefer context packs, schemas, tools, and gates.
- Do not confuse SDK session continuity with business-state persistence.
- Do not treat a Slack thread summary, prompt text, or model rationale as
  approval to send, post, schedule, publish, or write externally.
- Keep Slack selected-message/thread context bounded, deduped, read-only, and
  attached through structured context.

## Verification

Use focused validation from `references/workitem-flow.md`. For broad
WorkItem/Orchestrator changes, prefer:

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/test_orchestrator_preflight_context.py tests/test_workflow_runner.py
.venv/bin/python -m pytest tests/test_slack_agent_actions.py tests/test_slack_action_contract.py tests/test_context_packs.py
```
