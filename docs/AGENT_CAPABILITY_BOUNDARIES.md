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
It interprets the raw request, owns the semantic route and ordered-workflow
choice, carries the validated internal decision and constraints into specialist
briefs, and reviews outputs. The compact public preflight does not need to
expose the private decision payload.

Chief of Staff is the operating manager for broad, ambiguous, project/ops,
Slack-thread, automation, and cross-agent goals. It can synthesize operating
state, ask specialists for advisory context, and recommend durable WorkItem
handoffs.

Python gates remain authoritative for approvals, source sufficiency, recipient
readiness, record identity, external-write scope, and side-effect blocking.
WorkItems, context packs, approval records, artifact refs, source refs, audit
events, and renderer metadata remain canonical business state.

## Agent-Owned Decisions And Deterministic Validation

`agent_decision_policy.py` defines the semantic decisions owned by every
registered agent, the deterministic validators that constrain those decisions,
and forbidden shortcuts. Orchestrator owns route and workflow meaning; Chief
owns manager context/delegation decisions; each specialist owns its domain
selection, judgment, and synthesis. Python owns capability admission,
permissions, ceilings, exact identity checks, arithmetic, side-effect gates,
provider verification, and public-state consistency.

Every substantive model-owned selection must be grounded in evidence the model
actually saw. Supplied-context paths serialize the raw request, bounded
candidate identities, relevant provider evidence, required cross-provider
context, and limitations before the model call. Tool-loop paths return provider
results into the same model loop before selection. A declared pre-model
candidate that is absent from the actual input fails before a model request.

After the model turn, route-specific decision contracts verify the selected and
excluded identities, decision reasoning, candidate coverage, provider receipt
binding, and required handoff. Validation may reject the result or request
bounded repair, but it must not insert missing substantive evidence, choose a
different candidate, or convert invalid output into an apparently grounded
success.

`ManualRequestPlan` remains a supported typed constraint and compatibility
envelope used across direct and WorkItem paths. Its standalone model producer is
optional. Planner output may narrow safe scope or preserve structural request
shape, but it is not a second semantic authority and cannot override an
agent-owned decision or deterministic gate. Exact per-agent default call shapes
remain documented only where current call-site and trace evidence confirms them.

For an explicitly named agent, the mention is routing advice rather than
authority to bypass Orchestrator interpretation. Provider names in supplied
facts, quoted examples, negative constraints, time-pressure context, and generic
words such as `meeting`, `now`, `review`, or `test` may shape context or output
but cannot grant provider scope or side-effect authority.

## Tool Execution And Bounded Recovery

Tool attachment indicates what a model could call, not what executed. The
runtime records separate origins for:

- model requests, which do not by themselves establish any tool execution;
- tools admitted and attached to the SDK agent;
- SDK tools actually called by the model and their returned outputs;
- tools called by a deterministic workflow stage;
- provider context acquired before the model turn;
- deterministic helpers that normalize, validate, calculate, or enforce gates.

Provider attempts, successful provider requests, and durable provider receipts
are also separate counters. A tool-execution postcondition can require,
optionally allow, or forbid calls for one stage. Missing required tool evidence
may enter a corrective model turn; invalid typed decisions may enter semantic
repair when the active call site uses the shared wrapper. Those paths reuse verified
evidence where safe, preserve prior receipts, disable already-completed mutation
tools, and fail closed when the correction cannot be grounded. Exact retry
count and ordering are runtime properties and should be claimed only from the
current wrapper and tests.

When configured, the request-local model ledger is the hard ceiling around
these paths. SDK hooks consume one allowance before every parent, nested, or
sandbox model turn; sequential child processes receive a reserved remainder and
reconcile actual usage back to the parent. An attempted N+1 turn is rejected
before provider dispatch. Terminal telemetry preserves the budget correlation,
consumed and remaining allowance, and exhaustion stage.

The request estimator describes semantic execution envelopes, not every
transport attempt. Its route-sensitive stage rows separate the initial agent
loop from conditional required-tool correction and decision/validator repair.
Rate-limit transport retries and structured-output retries stay outside those
semantic rows, while every model request they actually initiate remains subject
to the hard ledger and attempt telemetry.

Current integration is described by call site rather than as one registry-wide
lane:

- `WorkflowRunner` dispatches its implemented workflow routes, but not the
  direct live Airtable, Google Workspace, or Zotero routes in `cli_impl.py`;
- RSS and Preprints use the signal runtime;
- nested Chief context specialists use validated child wrappers over agent
  tools. They bind selections to actual provider candidates, preserve one
  evidence-only repair without rereads, fail closed without execution context,
  return blocked envelopes instead of unvalidated prose, and reject mutation
  tools. Validated child decision records attach durably through SDK tool custom
  data and enter the unified privacy-safe trace, not the model-visible or public
  result envelope. A live parent-supplied `run_config` is labeled `live_sdk`;
  nested live reads enforce `live=true` on isolated tool copies;
- direct Airtable, Workspace, and Zotero receive one bounded missing-tool
  corrective turn before semantic-decision validation/repair, with
  provider-authoritative candidate binding, cumulative telemetry, exact
  lifecycle-tool contracts, and mutation-safe evidence handling. Completed read
  evidence may be replayed with completed tools disabled; mutations are never
  repeated, and unavailable replay or a failed corrected postcondition fails
  closed. They remain outside first-class
  `WorkflowRunner` specialist dispatch;
- RSS and Preprints now replay the first verified candidate universe tool-free
  for semantic repair; the history provider read is not repeated, and both
  attempts retain evidence, usage, and telemetry.

The decision policy and trace harness define contracts and test surfaces; they
do not by themselves prove that every registered agent has the same production
dispatcher or recovery coverage.

The generic provider-free `DirectAgentResponse` lane is restricted to
positively evidenced supplied-text or attachment synthesis. It cannot intercept
provider actions, candidate selection, continuations, delegation, or specialist
schemas. Its one-field answer is synthesis-only and is not evidence that a
named specialist compared candidates.

### Current Route-Level Classification

The current offline call-site and fake-model audit classifies all 11 registered
agents as bounded overall. Stronger labels apply only to the exact path named
below; no-live fixture/fallback paths remain deterministic facades.

| Agent | Strongest current path | Important weaker or alternate path |
| --- | --- | --- |
| Gmail Triage | true-specialist direct-live query/read/compare/repair | WorkItem fixture/ranking and generic supplied-response |
| Business Research | true-specialist direct-live and live WorkItem retrieval/selection/ranking with stable provider candidate IDs and one no-reread repair | provider-free supplied-context synthesis and no-live fallback remain narrower paths |
| Opportunity Scout | true-specialist direct-live and live WorkItem retrieval/selection/ranking/handoff with stable provider candidate IDs and one no-reread repair | source-provided and no-live fallback paths remain narrower |
| Outreach Composer | legitimate tool-free judgment over canonical approved WorkItem context | generic `DirectAgentResponse` is synthesis-only |
| Airtable Context | bounded direct-live model tool loop with missing-tool correction, provider-bound semantic repair, and mutation-safe evidence | deterministic marked lifecycle; no first-class WorkItem dispatch |
| Google Workspace Context | bounded direct-live model tool loop with missing-tool correction, provider-bound semantic repair, and mutation-safe evidence | deterministic marked lifecycle; no first-class WorkItem dispatch |
| Zotero Context | bounded direct-live model tool loop with missing-tool correction, provider-bound semantic repair, and mutation-safe evidence | no first-class WorkItem dispatch; nested Chief validation uses a separate child wrapper |
| RSS Context | bounded direct/live signal runtime with raw-history candidate binding | no-live facade; semantic repair replays the first candidate universe tool-free without another provider read |
| Preprints Context | bounded direct/live signal runtime with raw-history candidate binding | no-live facade; semantic repair replays the first candidate universe tool-free without another provider read |
| Orchestrator | bounded live route/workflow judgment; direct SDK may use scoped tools | exact WorkItem inspection can be overwritten by Python; SDK handoffs are disabled |
| Chief of Staff | bounded tool-free judgment over verified context or scoped tool calls; nested context decisions are provider-bound and validated | precompiled admission, fallback/Calendar/Gmail fast paths, and workflow fallback still weaken uniform ownership proof |

Provider selection and budgets, normalization, permissions, exact identity,
arithmetic, receipts, and rendering remain Python-owned. Candidate relevance,
selection, and delegation are model-owned only when the active path preserves
observable model-visible evidence and a validated decision record.

The shared runner now retains completed tool evidence across one correction,
aggregates usage and search telemetry across attempts, disables completed reads
and mutations before retry, and blocks automatic retry when mutation completion
is failed or unknown. Calendar lookup and verified Gmail continuation use one
evidence-preserving, tool-free semantic repair with no repeated provider read
and fail closed after exhaustion. Calendar lookup decision telemetry propagates
through the CLI payload and correlated trace evidence.

The same runner enforces request-local total and per-tool admission before
calling a function tool. Business Research and Opportunity Scout convert their
quality-budget `max_tool_calls` values into real total ceilings; RSS and
Preprints permit one history read. Gmail keeps its more precise domain contract:
up to four distinct candidate-context reads, with cached repeats and rejected
identities tracked separately. A blocked call is returned to the model as
structured evidence and does not reach the provider; a blocked mutation is
non-recoverable and cannot be retried or widened.

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

## Cross-Provider Context Boundary

Cross-provider context is read-only evidence, not inherited action authority.
The source provider read, manager selection, downstream handoff, and downstream
specialist decision must remain separately inspectable.

For the implemented Calendar-to-Gmail coordination contract:

1. Chief or Orchestrator selects one Calendar event from the exact bounded
   provider result set and explains excluded alternatives;
2. the validator binds that selection to the Calendar receipt and requires an
   explicit typed handoff to Gmail Triage;
3. only the bounded selected event is serialized into the Gmail handoff;
4. Gmail Triage separately chooses and reads the message/thread evidence and
   owns reply relevance and wording;
5. Calendar context cannot authorize a Gmail label, draft, send, or other
   mutation, and Gmail approval cannot enlarge Calendar authority.

If any required provider context or handoff evidence is missing, the join
blocks rather than substituting a first result or reconstructing the missing
decision in Python.

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
- Which validator failures should remain terminal versus enter the shared
  bounded model correction/repair path as coverage expands.
- Which milestone will first validate plan-based WorkItem continuation beyond
  phrase markers across a full no-live manager workflow.

## Test Acceptance Criteria

ANU-193 is accepted when tests or evals prove:

- route/workflow choices and specialist selections are model-owned, typed, and
  bound to the model-visible candidate universe
- invalid selections trigger fail-closed validation or bounded repair without
  Python substituting a semantic answer
- no-send/no-write constraints survive Orchestrator preflight, handoff, review,
  and repair
- tool traces distinguish attached, model-called, workflow-called,
  pre-acquired, helper, provider-attempt, provider-success, and receipt evidence
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
  tool-call origins, provider attempts/successes/receipts, approval checkpoints,
  repair state, usage, cache state, external-write state, and no raw private
  content
