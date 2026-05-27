# LangGraph Orchestration

## Position

OpenAI Agents SDK remains the core specialist-agent layer for Keystone. The four specialists stay as SDK agents:

- Gmail Triage
- Business Research Analyst
- Opportunity Scout
- Outreach Composer

Each specialist keeps its `build_*_agent()` function, markdown prompts, Pydantic structured output, explicit tool wrappers, dry-run fixture mode, and safety guardrails. LangGraph is implemented as an optional WorkItem orchestration wrapper; it does not replace SDK agents.

Current implementation:

- `src/keystone_agents/langgraph_workflow.py` defines the optional graph wrapper.
- `pyproject.toml` exposes LangGraph through the `orchestration` extra only.
- `keystone work-items advance --langgraph` runs WorkItem advancement through the wrapper.
- `KEYSTONE_WORKITEM_LANGGRAPH=true` enables the wrapper for repo-local
  `work-items advance` calls without requiring `--langgraph`.
- `KNI_BUSINESS_AGENTS_LANGGRAPH=true` enables the same wrapper for WorkItem
  advancement reached through the local `keystone-slack` bridge.
- If LangGraph is not installed, tests and local dry-run execution can exercise the same node contract through a dependency-free fallback.

Install the optional runtime only in environments that need graph execution:

```bash
.venv/bin/python -m pip install -e ".[orchestration]"
```

## Operational Use

LangGraph is used at the WorkItem orchestration boundary, not inside specialist
agents. The graph receives a typed `WorkflowRunRequest`, calls the existing
WorkItem runner, records node-path and checkpoint metadata, and returns the same
`WorkflowRunResult` shape used by the CLI, Slack handlers, tests, and renderers.
It is downstream of Orchestrator preflight: Keystone captures the raw request
and compact context, obtains Orchestrator route advice/review context, then
passes that memo into the typed `WorkflowRunRequest`. LangGraph may checkpoint
or resume the run, but it does not replace Orchestrator planning, specialist
SDK agents, deterministic safety gates, or output review.

CLI usage:

```bash
.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "continue" \
  --database-url "$DATABASE_URL" \
  --langgraph \
  --json
```

Slack usage:

1. Install the `orchestration` extra in the KBA virtual environment referenced
   by `KNI_BUSINESS_AGENTS_PYTHON`.
2. Set `KNI_BUSINESS_AGENTS_LANGGRAPH=true` in the `keystone-slack` environment.
3. Continue using existing Slack actions such as `continue`, more research,
   find contact, and revise draft.

The `keystone-slack` bridge already merges its environment into subprocesses
before invoking this repo, so no bridge code change is required for the flag to
reach KBA. Existing live flags still control model execution, search, Slack
posting, and Gmail drafts independently.

Slack direct WorkItem actions keep their explicit action semantics when
LangGraph is enabled, but the request still carries Orchestrator preflight
context and records `orchestrator_action_review` metadata after the run. The
graph is a resumable execution wrapper; the Orchestrator remains the model
control plane.

## Why SDK First

The SDK agents are the stable unit of behavior. They own prompts, output schemas, tool access, and guardrails. This keeps each agent testable without a larger workflow engine and prevents orchestration concerns from leaking into specialist prompts.

Keeping the SDK layer first also supports:

- simpler fixture-mode tests
- direct CLI execution of each specialist
- focused safety checks for outbound copy
- source-attributed company, broader research, and opportunity research
- orchestration that can swap workflow engines without rewriting specialists

## What LangGraph Improves

LangGraph improves orchestration rather than individual agent reasoning:

- It creates explicit node boundaries around WorkItem advancement.
- It gives the project a durable checkpoint point before approval-gated next actions.
- It lets future Slack or scheduled runs resume from a graph thread id instead of rerouting.
- It can add retry policies per node without changing specialist agents.
- It can support branching workflows for inbound Gmail, account research, opportunity scoring, and outreach.
- It preserves SQLite WorkItems as canonical audit state while allowing LangGraph checkpoints.

Do not use LangGraph merely to call one specialist agent or to hide business logic in graph nodes.

## Implemented Nodes

- `advance_work_item`: Calls the existing deterministic `advance_work_item()` runner with a typed `WorkflowRunRequest`.
- `approval_checkpoint`: Records a graph-level checkpoint payload when the WorkItem result requires human approval.

The implementation intentionally starts with one WorkItem advancement per graph run. Multi-specialist loops should be added only after this wrapper is stable in real use.

## Future Nodes

- `classify_input`: Decide whether the input is inbound email, company/account context, broader research request, opportunity scouting request, or approved outreach context.
- `gmail_triage`: Run the SDK Gmail Triage agent or fixture equivalent.
- `account_research`: Compatibility node for the SDK Business Research Analyst or fixture equivalent.
- `opportunity_scoring`: Run the SDK Opportunity Scout agent or fixture equivalent.
- `outreach_drafting`: Run the SDK Outreach Composer agent only after approved context exists.
- `approval_checkpoint`: Interrupt for human review before outbound copy is generated or used.
- `storage`: Persist inputs, structured outputs, approvals, and audit records.
- `report`: Render markdown and JSON reports from structured outputs.

## Future Edges

- `classify_input -> gmail_triage` for inbound email.
- `classify_input -> account_research` for known companies or accounts.
- `classify_input -> opportunity_scoring` for scout requests.
- `gmail_triage -> approval_checkpoint` when a reply draft or risky content is present.
- `gmail_triage -> account_research` when the email identifies a relevant company.
- `account_research -> opportunity_scoring` when source-backed fit signals exist.
- `opportunity_scoring -> account_research` when a high-priority opportunity needs company enrichment.
- `opportunity_scoring -> approval_checkpoint` before any outreach drafting.
- `approval_checkpoint -> outreach_drafting` only after explicit approval.
- `outreach_drafting -> approval_checkpoint` before any outbound use of draft copy.
- `approval_checkpoint -> storage` after each human decision.
- `storage -> report` for final run output.

## Interruptions And Approval Checkpoints

Interruptions should be explicit and persisted. The current wrapper records an `approval_checkpoint` node when the WorkItem result requires approval. Future live graph interrupt/resume handling should pause when:

- outbound email, LinkedIn, Slack, or CRM copy may be produced
- a draft exists and requires review
- PHI or patient-specific content is detected
- legal, financial, security, or contractual content is present
- source attribution is insufficient for a company or opportunity claim
- a live integration flag is requested

Approval checkpoints must record the reviewer, decision, timestamp, scope, and approved next action. Approval to draft does not imply approval to send. No external email may be sent automatically.

## Current Guardrails

- Do not add LangGraph to core dependencies; keep it optional.
- Do not rewrite SDK specialists as graph-native nodes.
- Do not remove or bypass `build_*_agent()` functions.
- Do not create live integration paths as part of orchestration.
- Do not implement automatic email sending, CRM writes, or scheduled follow-ups.
- Do not put prompts or scoring logic into graph edge definitions.
- Do not persist PHI, secrets, raw credentials, or sensitive draft material in logs.

## Initial Migration Shape

The first implementation is a thin orchestrator around existing WorkItem advancement. Each graph node accepts typed serializable state, calls one existing utility, stores structured output, and returns the next state. Reports remain generated from structured outputs, not graph internals.
