# Agent Capability Boundaries

This note is the repo-local contract for ANU-193, ANU-194, and ANU-124. It
defines what Orchestrator and Chief of Staff may read, write, and modify as KBA
adds broader manager workflows.

An authenticated direct operator command is the human approval reference for
the exact supported operation and target it names. Provider sends, posts,
schedules, CRM/Airtable writes, Gmail drafts, Google Workspace mutations,
Zotero imports, and Slack broadcast changes still require the owning specialist
or approved action handler, explicit live flags, exact target scope, typed tool
support, provider read-back, and an audit reference derived from or supplied by
that command. Approval does not expand to inferred targets, bulk changes,
schema mutations, or unrelated side effects.

## Control-Plane Split

Orchestrator is the first model control plane for natural-language entrypoints.
It interprets the raw request, prepares route advice, carries constraints into
specialist briefs, and reviews outputs.

Chief of Staff is the operating manager for broad, ambiguous, project/ops,
Slack-thread, automation, and cross-agent goals. It can synthesize operating
state, ask specialists for advisory context, and recommend durable WorkItem
handoffs.

Python gates remain authoritative for approvals, source sufficiency, recipient
readiness, record identity, external-write scope, and side-effect blocking.
WorkItems, context packs, approval records, artifact refs, source refs, audit
events, and renderer metadata remain canonical business state.

## Ownership Reconciliation

Semantic planning proposes ownership; one shared Python resolver reconciles that
proposal for direct and WorkItem/graph execution.

For an unnamed `@KNI` or Orchestrator request, the semantic plan may choose the
owner or Chief-managed workflow. For an explicitly named agent, reassignment
requires two agreeing signals:

1. a capability-specific semantic intent; and
2. bounded positive evidence in the current operator instruction, such as an
   operation bound to the provider/object, a capability-specific requested
   artifact, or an explicit multi-owner sequence.

Provider names in supplied facts, quoted examples, negative constraints,
time-pressure context, and generic words such as `meeting`, `now`, `review`, or
`test` may shape context or output but cannot change ownership. If the planner
and bounded evidence disagree, the named owner remains selected and ordinary
execution continues; the disagreement must not become a clarification,
WorkItem, graph, approval, or missing-context blocker.

Typed controls, exact provider identity, safety gates, and approval checks remain
deterministic. This ownership rule does not prevent a real Gmail, Calendar,
Airtable, Workspace, Slack, browser-diagnostics, or multi-owner request from
delegating to its supported owner.

## Orchestrator R/W/M

### Read

Orchestrator may read:

- raw operator request text and explicit constraints
- compact Slack/thread context selected by the entrypoint
- WorkItem state, route history, prior run summaries, artifacts, sources, and
  approval state
- local memory and approved context packs
- retrieval hints, route diagnostics, decision trace entries, and specialist
  outputs
- graph/backend diagnostics only as WorkItem execution metadata

### Write

Orchestrator may write internal control-plane state:

- preflight route advice, missing-context blockers, safety notes, and retrieval
  hints
- compact specialist handoff briefs that preserve raw request wording,
  constraints, source requirements, stop conditions, and forbidden actions
- decision trace entries, review notes, repair instructions, and next-safe-action
  recommendations
- safe internal follow-up tasks or planning artifacts when the owning workflow
  surface supports them

These writes are planning and review metadata unless persisted by the WorkItem
or storage layer. They do not authorize provider mutation.

### Modify

Orchestrator may modify internal plans after user feedback, new evidence,
validation failure, stale context, or specialist failure:

- revise route advice and specialist briefs
- update output-review feedback and repair instructions
- recommend a different safe next specialist or a stop/clarification state
- append decision trace metadata explaining why the plan changed

It must not modify provider records, send/post/schedule externally, or convert a
draft/read-only request into a live action. It also must not treat an explicit
agent mention, prior approval-like language, or LangGraph backend selection as a
gate override.

## Chief Of Staff R/W/M

### Read

Chief of Staff may read:

- Slack/thread/runtime context that the entrypoint made available
- WorkItems, approvals, automations, recent agent runs, artifacts, source refs,
  and local audit records
- repo/backlog context, approved memory, and local context tools
- Airtable, Google Workspace, Gmail, Zotero, RSS, and preprints context through
  typed read/context tools
- nested specialist outputs returned through agents-as-tools envelopes
- graph/backend state only as WorkItem advancement diagnostics

### Write

Chief of Staff may write internal manager artifacts:

- manager workflow plans with objective, selected specialists, required context,
  allowed tool tiers, approval gates, and stop condition
- decision logs, work summaries, review packets, internal notes, and approval
  requests
- draft internal artifacts and write-plan proposals
- `durable_handoff.agent` for the specialist that should become canonical
  downstream WorkItem owner
- `context_handoffs` for read-only context agents that should stage evidence,
  schema, or artifact context around a durable specialist

These writes are internal state or review artifacts. They do not execute live
provider mutations by themselves.

### Modify

Chief of Staff may modify internal operating plans and recommendations:

- revise manager workflow plans, task queues, approval requests, and operating
  artifacts after feedback or new evidence
- adjust selected specialists, context requirements, and stop conditions
- update handoff recommendations when the safer owner changes
- request repair, clarification, or human review when a specialist result is
  weak, blocked, or unsafe

It must not modify external systems without a separately approved provider
mutation path. It must keep top-level specialist write capability available
behind that specialist's live flags and approval gates, while nested specialist
tools remain advisory/read-plan by default.

## Agents-As-Tools Boundary

Chief may use agents-as-tools inside its SDK call for route confidence, compact
domain judgment, source-backed context, blocker discovery, draft-quality review,
or provider-specific read/write-plan advice.

Nested specialist calls must:

- receive structured context lanes such as `decision_context`,
  `target_context`, `coordination_context`, and `provider_call_context`
- preserve source IDs, blockers, approval needs, side-effect boundaries, and
  human-work context in nested-result envelopes
- return advisory context to Chief rather than becoming the final user-facing
  answer or canonical WorkItem owner
- avoid live writes, sends, posts, schedules, deletes, schema mutations, and
  attachment uploads unless a future issue adds an explicit approved nested
  write contract

If downstream work should become canonical business state, Chief must emit a
durable handoff instead of relying only on a nested tool result.

## Durable Graph Handoff Boundary

Durable graph handoffs are WorkItem state transitions. They are not the same as
Chief asking a specialist tool for advice.

Use:

- `ChiefOfStaffResult.durable_handoff.agent` when Business Research, Opportunity
  Scout, Gmail Triage, or Outreach Composer should become the next canonical
  WorkItem specialist
- `ChiefOfStaffResult.context_handoffs` when Airtable Context, Google Workspace
  Context, Zotero Context, RSS Context, or Preprints Context should stage
  read-only evidence or schema/artifact context before or after the durable
  specialist
- WorkItem events, artifacts, source refs, context packs, and approval
  checkpoints as the durable proof surface

Legacy prose such as `Chief of Staff -> Agent` remains compatibility fallback
only. New live reasoning should prefer structured handoff fields.

## Context-Agent R/W/M Boundary

Airtable, Google Workspace, Zotero, RSS, and Preprints context-agent boundaries
are defined in `docs/CONTEXT_AGENT_CONTRACTS.md` and
`src/keystone_agents/context_agent_contracts.py`. Those contracts are
authoritative for ANU-198 through ANU-202 context-pack evidence, graph-candidate
edges, approval requirements, blocked mutations, and validation gates.

The short rule is: context agents stage bounded evidence and write plans by
default. Airtable and Google Workspace may execute direct internal writes only
as selected top-level agents with exact target identity, approval reference, and
live flags. Ordinary Zotero library mutation stays behind the guarded importer
path; the sole native exception is a versioned disposable note containing
`KBA_TEST_NOTE`, with approval, a dedicated live flag, read-back verification,
and cleanup.
RSS and Preprints remain read-only until a separate feed/library management
contract exists.

## Backend Graph Selector Boundary

The backend graph selector owns simple-runner versus LangGraph execution
selection. It does not own business routing, provider permissions, approval
state, or specialist output authority.

Graph selection should be structural:

- resume/continue WorkItem execution
- multi-step specialist handoffs
- context-agent staging before a specialist or artifact plan
- Gmail-to-research-to-draft flows
- Chief-managed coordination across context agents and specialists
- approval, draft, or artifact checkpoints

Graph selection should not be triggered by user-visible test labels, prompt
mentions of LangGraph, planning-only route explanations, safety disclaimers, or
a simple single-specialist lookup. Direct send/write/post/schedule wording must
remain Python-gated whether the simple runner or LangGraph executes the step.

## Unresolved Architecture Decisions

- Whether Chief needs a separate first-class `ManagerWorkflowPlan` schema beyond
  the current `durable_handoff`, `context_handoffs`, WorkItem events, and
  manager-loop metadata.
- Whether any nested agents-as-tools path should ever execute a live write, or
  whether all provider mutations should remain top-level specialist/action
  handler work.
- Which internal manager artifacts should be persisted as durable WorkItem
  artifacts versus transient decision-trace metadata.
- How much Orchestrator repair should remain deterministic versus use a budgeted
  LLM review pass for route correction after specialist failure.
- Which milestone will first validate plan-based WorkItem continuation beyond
  phrase markers across a full no-live manager workflow.

## Test Acceptance Criteria

ANU-193 is accepted when tests or evals prove:

- route correction updates internal plans/decision trace without changing
  external side-effect authority
- no-send/no-write constraints survive Orchestrator preflight, handoff, review,
  and repair
- specialist ownership and Python gates remain authoritative

ANU-194 is accepted when tests or evals prove:

- Chief manager asks produce internal plans or handoffs rather than external
  mutations
- agents-as-tools outputs remain advisory/read-plan and preserve blockers,
  sources, approval needs, and provider-call context
- internal write/modify paths are separated from Slack/Gmail/Calendar/Airtable/
  Workspace/Zotero mutations

ANU-124 is accepted when tests or evals prove:

- broad, ambiguous, project/ops, Slack-thread, automation, and cross-agent asks
  default to Chief when no narrower deterministic command applies
- Chief emits a typed workflow plan or structured durable/context handoff before
  execution
- continuation consumes plan and WorkItem state instead of relying primarily on
  phrase markers
- trace/cost evidence records selected route, rejected routes, child steps,
  tool calls, approval checkpoints, repair count, usage, cache state, and no raw
  private content
