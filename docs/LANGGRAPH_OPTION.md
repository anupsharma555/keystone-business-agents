# LangGraph Orchestration

## Position

OpenAI Agents SDK remains the core specialist-agent layer for Keystone. The workflow specialists stay as SDK agents:

- Gmail Triage
- Business Research Analyst
- RAG Retrieval Specialist (explicit vector-store requests only)
- Opportunity Scout
- Outreach Composer

Each specialist keeps its `build_*_agent()` function, markdown prompts, Pydantic structured output, explicit tool wrappers, dry-run fixture mode, and safety guardrails. LangGraph is implemented as an optional WorkItem graph runtime; it does not replace SDK agents, WorkItems, context packs, or Python gates.

Current implementation:

- `src/keystone_agents/langgraph_workflow.py` defines the optional graph runtime.
- `pyproject.toml` exposes LangGraph through the `orchestration` extra only.
- `keystone work-items advance --langgraph` still forces graph-native nodes for
  low-level diagnostics.
- Normal `@KNI`, CLI `work-items advance`, and Slack WorkItem manager-loop runs
  use backend selection:
  single-agent runs can stay on the simple runner, while resumes, multi-step
  handoffs, approval/draft checkpoints, Gmail-to-research/reply paths,
  context-agent-to-specialist paths, and Chief-of-Staff coordination asks use
  LangGraph automatically.
- `KEYSTONE_WORKITEM_LANGGRAPH` and `KNI_BUSINESS_AGENTS_LANGGRAPH` remain
  explicit environment overrides for diagnostics or emergency disablement; they
  are not the normal user-facing orchestration selector.
- If LangGraph is not installed, tests and local dry-run execution exercise the same graph-node contract through a dependency-free fallback.

Install the optional runtime only in environments that need graph execution:

```bash
.venv/bin/python -m pip install --no-build-isolation -e ".[orchestration]" -c constraints/ci.txt
```

## V2 persistence and recovery

Saved compiled-graph executions now use a SQLite checkpointer and a companion
execution journal. Each logical execution has its own `execution_id`, which is
the native LangGraph thread cursor. The older `checkpoint_key` remains a
caller-compatible reference and can reflect a WorkItem grouping identifier.
Use `execution_id` explicitly when resuming native execution. Unsaved runs and the dependency-free fallback do not create native
LangGraph checkpoints.

Use `scripts/kba_execution.py` with the exact business database to list, inspect,
and resume native executions. Approval interrupts are enabled by default for
saved graphs; `--approved` requires an already persisted approval and does not
grant it. Resume checks current WorkItem state and runtime compatibility. The
journal preserves model consumption and provider-operation evidence, while
provider gates still own permission. Unknown effects require reconciliation.

Direct execution remains supported and shares the operation/budget contract.
SDK conversation sessions, business WorkItems, and native graph checkpoints are
separate stores with separate purposes. Diagnostic fork export does not execute
historical provider actions. See [V2 implementation](KBA_V2_IMPLEMENTATION.md)
for current coverage, commands, and proof boundaries.

## Operational Use

LangGraph is used at the WorkItem orchestration boundary, not inside specialist
agents. The graph receives a typed `WorkflowRunRequest`, runs explicit nodes for
request normalization, Orchestrator preflight handoff, state follow-up,
WorkItem/context preparation, specialist execution, finalization, and approval
checkpointing, then returns the same `WorkflowRunResult` shape used by the CLI,
Slack handlers, tests, and renderers.
It is downstream of Orchestrator preflight: Keystone captures the raw request
and compact context, obtains the Orchestrator's typed semantic route/workflow
decision, and passes the validated internal decision plus the public-safe memo
into the execution boundary. LangGraph may checkpoint or resume the run, but it
does not replace Orchestrator interpretation, specialist SDK agents,
deterministic safety gates, or output review.

Graph execution does not imply that every tool is model-called. A graph stage
may acquire bounded provider context or execute an exact workflow-owned helper;
a specialist may instead choose among admitted tools inside its SDK loop. The
downstream model must still see any evidence that materially affects its
decision. Runtime summaries distinguish model-called tools, workflow-called
tools/helpers, and pre-acquired context rather than collapsing them into one
`tools_used` claim.

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
The KBA environment contract now marks the Slack parent
`KNI_BUSINESS_AGENTS_LANGGRAPH` flag as a child-env scrub key. That prevents a
stale sibling Slack `.env` value from forcing every KBA child run into graph
mode. Normal Slack runs should be backend-selected; forced comparison runs
should use the KBA-side `KEYSTONE_WORKITEM_LANGGRAPH=false` or `true` override
as explicit test harness state, never visible Slack prompt text.

Slack direct WorkItem actions keep their explicit action semantics when
LangGraph is enabled, but the request still carries Orchestrator preflight
context and records `orchestrator_action_review` metadata after the run. The
graph is the execution path for the opted-in WorkItem step; the Orchestrator
remains the model control plane and WorkItems remain canonical business state.
When a graph-selected manager-loop run receives a realtime feedback callback,
it emits `manager_loop_graph_started` and `manager_loop_completed` events with
node path, route/status, checkpoint state, checkpoint payload when approval is
required, stop reason, step summaries, and `send_enabled=false`. It does not
emit the legacy manager-loop
review/repair-stream events unless that review layer is deliberately added to
the graph path later.
The direct Chief-of-Staff script handoff path also calls
`advance_work_item_manager_loop_with_optional_langgraph`, so a Chief SDK result
that recommends a WorkItem-capable specialist uses the same backend selector as
normal `@KNI`, Slack, and WorkItem manager-loop entrypoints.

## Backend Selection Policy

LangGraph should be selected by backend policy when there is a meaningful
workflow boundary to preserve: existing WorkItem resume/continue runs,
multi-step specialist handoffs, Gmail-to-research-to-draft flows,
context-agent evidence staging before a specialist or artifact plan,
Chief-of-Staff-managed coordination across context agents and specialists, and
approval or draft checkpoints.
Natural Chief-of-Staff asks can be graph-worthy without naming specialist
agents when the request structure contains multiple execution stages, such as
research plus opportunity assessment, Gmail triage plus research, source
evidence plus artifact planning, or draft-only outreach plus an approval
checkpoint. That backend decision is still structural; it does not decide the
business answer.

Simple single-specialist lookups should stay on the simple runner. Deterministic
status/list/show commands, context-agent-only reads with no downstream
specialist or artifact plan, and safety disclaimers such as "do not draft
outreach" should not trigger LangGraph by themselves. Direct send/write/post
requests remain Python-gated; graph selection must never turn those requests
into live side effects.

Planning-only asks should also stay outside LangGraph, even when they mention
workflow, handoff order, blockers, draft-only outreach, or approval checkpoints.
For example, "Chief of Staff, plan the safest workflow and state the route"
should return a plan through the Orchestrator/Chief path; "run the workflow"
can enter the graph when the backend sees a real multi-step execution boundary.

The typed compatibility/constraint envelope and backend-selection logic may
detect structural controls such as
target agent, target entity, requested count, live-search denial, read-only
scope, no-send/no-write constraints, and whether the request asks for a
multi-step handoff. It must not interpret LLM-owned business substance such as
company fit, evidence quality, opportunity strength, outreach angle, or whether
a draft should be persuasive. Test harness wording such as "comparison smoke",
"control run", "forced true", or "forced false" is orchestration metadata, not a
request for comparison-format output or multi-target research. A single named
company should keep `desired_count=1` and should not enter the multi-target
research branch merely because the surrounding test is a comparison.

For Slack-thread-local sample outreach, a draft that stays inside the current
thread for review is not the same as sending email, creating a Gmail draft, or
posting elsewhere. It may use source-provided WorkItem context through the
Outreach Composer LLM synthesis path when the external delivery flags remain
false. In no-SDK/offline paths, Keystone should not create deterministic
placeholder outreach copy; it should ask for model-backed synthesis or exact
operator-provided copy. External-use approval is still required before any
send, Gmail draft, post outside the thread, schedule, publication,
CRM/Airtable write, or other live side effect.

## Why SDK First

The SDK agents are the stable unit of behavior. They own prompts, output schemas, tool access, and guardrails. This keeps each agent testable without a larger workflow engine and prevents orchestration concerns from leaking into specialist prompts.

They also own the substantive domain decisions assigned by
`agent_decision_policy.py`. LangGraph owns durable ordering and checkpoints;
Python owns admission, validation, permissions, provider verification, and
state transitions. The graph must not infer a semantic decision from incidental
prose when a typed agent decision or handoff exists.

Keeping the SDK layer first also supports:

- simpler fixture-mode tests
- direct CLI execution of each specialist
- focused safety checks for outbound copy
- source-attributed company, broader research, and opportunity research
- orchestration that can swap workflow engines without rewriting specialists

## What LangGraph Improves

LangGraph improves orchestration rather than individual agent reasoning:

- It creates explicit node boundaries inside WorkItem advancement.
- It gives the project a durable checkpoint point before approval-gated next actions.
- Native saved executions can resume by execution ID; Slack transport continuation still needs its own correlated acceptance.
- It can add retry policies per node without changing specialist agents.
- It can support branching workflows for inbound Gmail, account research, opportunity scoring, and outreach.
- It preserves SQLite WorkItems as canonical audit state while allowing LangGraph checkpoints.

Do not use LangGraph merely to call one specialist agent or to hide business logic in an opaque runner node.

## Implemented Nodes

- `normalize_request`: Applies the same request normalization used by WorkItem flows.
- `orchestrator_preflight`: Carries the compact Orchestrator preflight boundary into graph state.
- `state_followup`: Answers state-only same-thread questions before specialist execution.
- `prepare_work_item`: Creates or loads the WorkItem, applies context, builds the context pack, and records start events.
- `stage_feed_context`: Stages read-only RSS or preprint context from local
  announcement-history records as selected WorkItem artifacts/sources before
  downstream Business Research or Opportunity Scout when the operator asks for
  historical signal context.
- `stage_zotero_context`: Stages a read-only Zotero context handoff as a selected
  WorkItem artifact/source before downstream Business Research when the operator
  asks for Zotero context to inform evidence, brief, or artifact planning.
- `stage_google_workspace_context`: Stages a read-only Google Workspace
  artifact/write plan as a selected WorkItem artifact/source and approval gate
  when the operator asks for internal Drive, Docs, Sheets, or sharing planning.
- Route-specific specialist nodes: `run_business_research`, `run_rag_retrieval`, `run_opportunity_scout`, `run_gmail_triage`, `run_outreach_composer`, `run_chief_of_staff`, and `run_unsupported_route`. `run_rag_retrieval` is admitted only by an explicit named-agent request or typed workflow route during the initial rollout.
- `finalize_step`: Attaches final context and persists the WorkItem. Normal one-step runs may synthesize the user-facing response here; manager-loop graph runs defer generic final synthesis so each live specialist edge can stay inside an explicit API-call budget.
- `manager_loop_continue`: Converts a successful non-approval next action into a `continue` request for the next distinct specialist when the operator asked for a multi-step workflow.
- `manager_loop_finalize`: Records the bounded graph-loop stop reason, step
  list, and deterministic graph completion review when no further safe graph
  edge should run. The review compares requested stages with completed graph
  nodes/artifacts, names missing or intentionally skipped stages, and carries
  renderer-ready explanation lines without adding another model call.
- `approval_checkpoint`: Records a graph-level checkpoint payload when the
  WorkItem result requires human approval. The persisted `langgraph_orchestration`
  event includes the bounded review payload: target, next action, blockers,
  approval gates, artifact refs, source refs, compact context-pack readiness, and
  explicit `send_enabled=false` / `external_writes_enabled=false` flags.
  `LangGraphWorkflowOutcome.checkpoint_payload` exposes the same bounded payload
  to direct graph callers without changing the canonical `WorkflowRunResult`
  shape.

The graph now supports bounded multi-step continuation through existing
WorkItem `next_action.agent` contracts. It stops at approval checkpoints,
terminal WorkItem statuses, repeated routes, missing next actions, max-step
limits, and requests that did not ask for multi-step execution.
The main approval-gated multi-step edges below are covered both at the direct
graph runtime layer and through `advance_work_item_manager_loop_with_optional_langgraph`,
so normal `@KNI`, Slack, and scheduled WorkItem paths can rely on backend
selection rather than user-visible LangGraph flags.
The backend-wrapper coverage for those primary edges also asserts WorkItem
artifact/source attribution and that no artifact enables send or external-write
metadata before the approval checkpoint.
The context-agent evidence/artifact-planning edges are also covered through the
same backend-selected wrapper for RSS-to-opportunity artifact planning and
preprints/Zotero-to-research Workspace artifact planning.
Chief-of-Staff-managed coordination is likewise backend-wrapper validated for
Chief -> RSS/Zotero context staging -> Business Research handoff and Chief ->
Opportunity -> Outreach approval checkpoint.
Opportunity Scout may use prior WorkItem sources and selected research/context
artifacts as source-provided context in offline graph paths. This is different
from falling back to generic fixture opportunities: if no live search is
approved and no source context exists, specific opportunity-fit asks should stop
and request evidence or live-search approval.

Chief of Staff can still use specialists as advisory tools inside its SDK call
when it needs read-plan context, route confidence, or a compact specialist
opinion before choosing the next owner. That is not a durable graph handoff.
When the ask requires canonical downstream work, the graph should still advance
through the selected specialist node so WorkItems, typed context packs, artifact
refs, events, checkpoints, renderer metadata, and no-send/no-write gates remain
the source of truth. Advisory tool output may inform Chief's recommendation but
should not silently replace `run_business_research`, `run_gmail_triage`,
`run_opportunity_scout`, `run_outreach_composer`, or context-agent staging nodes.
Chief's preferred durable handoff contract is the structured
`durable_handoff.agent` field on `ChiefOfStaffResult`. Legacy
`Chief of Staff -> Agent` wording remains a compatibility fallback for older
tests and deterministic dry-run plans, but live Chief reasoning should not need
the operator to name exact downstream agents in the visible prompt.
Chief's preferred context-staging contract is the structured
`context_handoffs` list on `ChiefOfStaffResult`. Use it for read-only context
agents such as RSS, preprints, Zotero, Airtable Context, and Google Workspace
Context that should stage evidence or schema/artifact context before or after a
durable specialist. These entries are not durable downstream owners and do not
authorize reads, writes, sends, posts, drafts, schedules, or publication.
Provider-side writes follow the same ownership rule: Chief and Orchestrator may
coordinate context, review plans, approvals, and resumes, but Airtable,
Google Workspace, Gmail draft, Zotero importer, Slack post, or other mutation
execution belongs to the owning specialist or explicitly approved action
handler. Nested `agents_as_tools` calls remain advisory/read-plan only.

## Minimal LLM Reasoning Touchpoints

Graph execution does not add default manager-review model calls. The default
graph completion review is deterministic and records `review_mode`,
`llm_review_used`, `cost_guard`, and `deterministic_gates_authoritative` in
review evidence. Any live validation of model review belongs to the repo's
cost-aware live SDK boundary and must be budgeted separately from normal graph
execution.

Model reasoning may be plugged in only at contained manager-review points:

- Relevance/usefulness review at `manager_loop_finalize`, after deterministic
  stage, source/artifact, status, approval, and side-effect checks.
- Repair routing at `manager_loop_continue`, where review feedback becomes
  observed gaps, target output type, source issue, repair route, and next safe
  action for the existing `manager_loop_repair` context.
- Source sufficiency judgment at `finalize_step`, only when deterministic source
  presence/count/provider checks are ambiguous rather than hard-failed.
- Final answer satisfaction at `manager_loop_finalize`, to decide whether the
  final answer satisfies the latest user ask or needs a targeted
  rewrite/deepen/stop recommendation.

These nodes may use fake/local LLM review in offline tests or explicit live
review under a cost budget. They must not change the hard authority of Python
gates: missing source refs, approval boundaries, no-send/no-write defaults,
recipient readiness, side-effect blockers, repair-attempt caps, and checkpoint
state remain deterministic.

The graph nodes that must stay deterministic are request normalization, state
follow-up routing, WorkItem/context preparation, read-only context staging,
approval checkpoints, backend selection, and all send/write/publish/schedule
gates. Specialist nodes may call their owning SDK agents when the existing
live/fixture flags allow it, but that is specialist execution, not an implicit
manager-review pass.

## Implemented Edges

- `run_business_research -> run_opportunity_scout -> run_outreach_composer -> approval_checkpoint` when a source-backed company profile should become an opportunity review and then draft-only outreach.
- `run_gmail_triage -> run_business_research -> run_outreach_composer -> approval_checkpoint` when an inbound thread needs company/source context before a reply draft.
- `stage_zotero_context -> run_business_research -> manager_loop_finalize`
  when a read-only Zotero context-agent handoff should inform research or
  internal evidence-packet planning without creating a first-class context-agent
  WorkItem route.
- `stage_feed_context -> run_opportunity_scout -> manager_loop_finalize` when
  read-only RSS announcement history should become source-provided opportunity
  signal context without live search, writes, posts, sends, or publication.
- `stage_feed_context -> run_business_research -> manager_loop_finalize` when
  read-only preprint history should become preliminary evidence context for
  internal research or evidence-packet planning without external claims or
  publication.
- `stage_feed_context -> stage_zotero_context -> run_business_research -> manager_loop_finalize`
  when historical/preprint signals and read-only Zotero collection context both
  need to be staged before Business Research prepares an internal evidence
  packet.
- `stage_google_workspace_context -> approval_checkpoint` when a Google
  Workspace Context handoff should become a read-only internal artifact plan
  requiring human review before any Drive, Docs, Sheets, sharing, or file
  mutation.
- `stage_feed_context -> stage_zotero_context -> run_business_research -> stage_google_workspace_context -> approval_checkpoint`
  when historical/preprint signals, read-only Zotero context, and Business
  Research evidence should feed an internal Workspace artifact plan before any
  Drive, Docs, Sheets, sharing, or file mutation.
- `stage_feed_context -> run_business_research -> stage_google_workspace_context -> approval_checkpoint`
  when a natural request asks for preprints/RSS evidence to support an internal
  artifact, packet, or brief plan. The graph first stages the read-only context,
  then runs the specialist evidence pass, then creates a read-only Workspace
  artifact plan for approval review without requiring the operator to name
  Google Workspace, Drive, Docs, or Sheets explicitly.
- `stage_feed_context -> run_opportunity_scout -> stage_google_workspace_context -> approval_checkpoint`
  when read-only RSS opportunity signals should become an opportunity packet
  artifact plan for internal Drive, Docs, or Sheets placement before any write,
  share, send, post, schedule, or publication.
- `run_chief_of_staff -> run_business_research -> manager_loop_finalize` when
  the Chief of Staff output recommends Business Research as the durable next
  owner through `durable_handoff.agent=business_research_analyst`.
- `run_chief_of_staff -> run_business_research -> run_opportunity_scout -> manager_loop_finalize`
  when Chief of Staff first selects Business Research for source-backed company
  evidence, the original request also asks for advisory/research opportunity
  assessment, and an explicit no-draft/no-send constraint should stop the graph
  before Outreach Composer.
- `run_chief_of_staff -> stage_feed_context -> stage_zotero_context -> run_business_research -> manager_loop_finalize`
  when Chief of Staff owns the coordination decision, selects read-only context
  agents such as RSS and Zotero, and then hands the bounded context pack to
  Business Research without live search, writes, posts, sends, or publication.
- `run_chief_of_staff -> run_business_research -> stage_google_workspace_context -> approval_checkpoint`
  when Chief of Staff selects Business Research as the main owner and a
  read-only Google Workspace Context artifact plan should be staged only after
  the evidence brief exists, with Drive, Docs, Sheets, sharing, and file
  mutation blocked until scoped human approval.
- `run_chief_of_staff -> run_business_research -> stage_airtable_context -> approval_checkpoint`
  when Chief of Staff selects Business Research as the main owner and a
  read-only Airtable Context write plan should be staged only after the evidence
  brief exists, with creates, updates, attachment uploads, deletes, and record
  mutation blocked until scoped human approval.
- `run_chief_of_staff -> stage_airtable_context -> run_business_research -> manager_loop_finalize`
  when Chief of Staff selects Airtable Context as a read-only schema/record
  planning lane before a main Business Research handoff, with Airtable live
  reads, writes, attachment uploads, sends, posts, and record mutation disabled.
- `run_chief_of_staff -> stage_google_workspace_context -> run_business_research -> manager_loop_finalize`
  when Chief of Staff selects Google Workspace Context as a read-only
  artifact/source planning lane before a main Business Research handoff, with
  Drive, Docs, Sheets, sharing, sends, posts, and file mutation disabled.
- `run_chief_of_staff -> stage_airtable_context -> stage_google_workspace_context -> run_business_research -> manager_loop_finalize`
  when Chief of Staff selects multiple read-only business-context lanes before
  Business Research, with each context artifact kept read-only and no Workspace
  or Airtable approval plan staged unless explicitly requested.
- `run_chief_of_staff -> stage_airtable_context -> run_business_research -> stage_airtable_context -> approval_checkpoint`
  when Chief of Staff requests Airtable schema/record context before research
  and separately requests a post-research Airtable write plan for human review.
- `run_chief_of_staff -> stage_google_workspace_context -> run_business_research -> stage_google_workspace_context -> approval_checkpoint`
  when Chief of Staff requests Workspace artifact/source context before
  research and separately requests a post-research Workspace artifact plan for
  human review.
- `run_chief_of_staff -> stage_airtable_context -> run_opportunity_scout -> manager_loop_finalize`
  when Chief of Staff selects Airtable Context as a read-only schema/record
  planning lane before Opportunity Scout evaluates an internal opportunity
  direction, with Airtable live reads, writes, attachment uploads, sends, posts,
  and record mutation disabled.
- `run_chief_of_staff -> stage_google_workspace_context -> run_opportunity_scout -> manager_loop_finalize`
  when Chief of Staff selects Google Workspace Context as a read-only
  artifact/source planning lane before Opportunity Scout evaluates an internal
  opportunity direction, with Drive, Docs, Sheets, sharing, sends, posts, and
  file mutation disabled.
- `run_chief_of_staff -> stage_airtable_context -> run_gmail_triage -> manager_loop_finalize`
  when Chief of Staff selects Airtable Context as a read-only schema/record
  planning lane before Gmail Triage reviews sanitized inbound email context,
  with Airtable live reads, writes, attachment uploads, sends, posts, Gmail
  drafts, and record mutation disabled.
- `run_chief_of_staff -> stage_google_workspace_context -> run_gmail_triage -> manager_loop_finalize`
  when Chief of Staff selects Google Workspace Context as a read-only
  artifact/source planning lane before Gmail Triage reviews sanitized inbound
  email context, with Drive, Docs, Sheets, sharing, sends, posts, Gmail drafts,
  and file mutation disabled.
- `run_chief_of_staff -> run_opportunity_scout -> run_outreach_composer -> approval_checkpoint`
  when the Chief of Staff output recommends Opportunity Scout, the original
  request asks for draft-only outreach if useful, and Outreach Composer must
  stop at the source-approval gate.

## Edge Coverage Matrix

Use this matrix to decide whether the next node-to-node path is already covered,
needs more proof, or should stay outside the graph. "Backend-selected" means the
path is reachable through `advance_work_item_manager_loop_with_optional_langgraph`
without asking the user to pass a LangGraph flag.

| Path family | Current status | Proof boundary | Notes |
| --- | --- | --- | --- |
| Business Research -> Opportunity Scout -> Outreach Composer -> approval checkpoint | Implemented and tested | Direct graph and backend-selected tests | Use when source-backed company research should become opportunity review and then draft-only outreach. |
| Gmail Triage -> Business Research -> Outreach Composer -> approval checkpoint | Implemented and tested | Direct graph and backend-selected tests | Use when inbound Gmail/thread context needs company research before reply or outreach drafting. |
| Natural Chief multi-stage ask -> backend-selected graph | Implemented and tested | Backend selector tests plus direct graph tests | Chief stays the front door; the backend selects graph from structural multi-stage execution markers, not planner semantic promotion or user flags. Chief's durable next owner is carried through `ChiefOfStaffResult.durable_handoff.agent`, and read-only context staging is carried through `ChiefOfStaffResult.context_handoffs`, with legacy prose handoff markers kept only as compatibility fallback. |
| RSS/preprints -> Opportunity Scout -> Workspace artifact plan -> approval checkpoint | Implemented and tested | Backend-selected tests | Keeps feeds as read-only signal context before opportunity or artifact planning. |
| Preprints/RSS -> Business Research -> Workspace artifact plan -> approval checkpoint | Implemented and tested | Direct graph and backend-selected tests | Natural internal artifact/packet/brief planning asks can stage a read-only Workspace artifact plan after context-backed research, even when the operator does not explicitly name Google Workspace. |
| Preprints/RSS + Zotero -> Business Research -> Workspace artifact plan -> approval checkpoint | Implemented and tested | Backend-selected tests | Keeps multiple context agents as evidence-planning inputs before a research packet or artifact plan. |
| Zotero -> Business Research -> manager finalize | Implemented and tested | Direct graph tests | Read-only Zotero handoff; no first-class Zotero WorkItem route yet. |
| Chief -> Business Research -> manager finalize | Implemented and tested | Direct graph and backend-selected tests | Chief selects Business Research as durable next owner. |
| Chief -> Business Research -> Opportunity Scout -> manager finalize | Implemented and tested | Direct graph tests | Stops before Outreach when the operator explicitly says no draft/send/write. |
| Chief -> selected RSS/Zotero context -> Business Research -> manager finalize | Implemented and tested | Backend-selected tests | Chief coordinates context agents before main research. |
| Chief -> Business Research -> Workspace/Airtable plan -> approval checkpoint | Implemented and tested | Direct graph and backend-selected tests | Post-research artifact/write planning remains approval-gated. |
| Chief -> Workspace/Airtable context -> Business Research -> manager finalize | Implemented and tested | Direct graph tests | Context-first lane before durable research handoff. |
| Chief -> Workspace/Airtable context -> Opportunity Scout -> manager finalize | Implemented and tested | Direct graph tests | Context-first lane before opportunity assessment. |
| Chief -> Workspace/Airtable context -> Gmail Triage -> manager finalize | Implemented and tested | Direct graph tests | Read-only business context before sanitized inbound email review. |
| Chief -> Workspace/Airtable context -> Gmail Triage -> Business Research -> manager finalize | Implemented and tested | Direct graph and backend-selected tests | Read-only business context can precede Gmail triage, and the email-selected organization can still become durable Business Research state. |
| Chief -> Opportunity Scout -> Outreach Composer -> approval checkpoint | Implemented and tested | Direct graph and backend-selected tests | Use when Chief selects opportunity first and the original ask requests draft-only outreach if useful. |
| Graph-produced source context -> Opportunity Scout offline assessment | Implemented and tested | Direct graph and quality-comparison tests | Prior WorkItem sources and selected research/context artifacts can satisfy the offline source requirement; generic fixture opportunities remain blocked for specific asks without evidence. |
| Chief advisory specialist tools inside the Chief SDK call | Intentionally not a durable graph edge | Regression test asserts graph handoff still runs afterward | Useful for route confidence/read-plan context only; must not silently replace downstream WorkItem nodes, structured `context_handoffs`, or the structured `durable_handoff.agent` WorkItem handoff. |
| Chief -> Gmail Triage -> Business Research -> manager finalize | Implemented and tested | Direct graph and backend-selected tests | Chief can select Gmail Triage first, then durable research can proceed when the original ask requests company research after email review. |
| Chief -> selected context agents -> selected main specialist -> review/finalize beyond current rows | Future candidate | Partial patterns exist | Extend case by case when operational payoff is clear. |
| Approval checkpoint -> stored approval decision -> safe graph resume | Implemented and tested | Direct graph and backend-selected tests | Approval decisions update WorkItems/SQLite and approved draft artifacts; a later graph `continue` reports saved state without redrafting, sending, posting, or writing externally. |
| Approval checkpoint -> approved external action execution | Future edge | Not implemented | Existing WorkItems/SQLite approval records remain canonical; external sends/posts/writes still require a separately scoped live integration path. |
| Storage -> operator graph report | Implemented and tested | Reporting helper tests from stored WorkItem/events | Renders node path, specialist steps, completion review, checkpoint state, artifacts, sources, blockers, approval decisions, and no-send/no-write flags from persisted state. |
| Single-agent lookup, context-only read, or deterministic readiness check | Intentionally not graphed | Backend selector tests | Keep as tool/helper/specialist call unless it creates a real multi-step state transition. |
| Chief/Orchestrator planning-only route explanation | Intentionally not graphed | Backend selector and wrapper tests | Mentioning workflow, handoff, blockers, draft-only outreach, or approval checkpoints is not enough; graph starts only when the backend sees execution/resume/multi-step state transition intent. |

## Future Edges

- Additional `run_chief_of_staff -> selected_context_agents -> selected_main_specialist -> review`
  variants beyond the RSS/Zotero/Airtable/Workspace-to-Business-Research and
  Airtable/Workspace-to-Opportunity-Scout/Gmail-Triage paths.
- `approval_checkpoint -> approved external action execution` after a scoped
  human decision and separately reviewed live integration path.

Do not graph single-agent lookups, context-agent-only reads, deterministic
readiness checks, direct send/post/write actions, or one-off natural-language
branches. Keep those as tools, gates, or specialist calls.

Live validation for a new edge should follow the cost-aware ladder: static and
fixture tests first, then mocked SDK tests, then at most three serial live SDK
calls with live search and writes disabled unless explicitly approved. Slack
messages that explicitly describe a bounded read-only LangGraph smoke should
use the `slack_smoke_limited` profile: no hosted web search, no manager-loop
repair expansion, no contact enrichment, and Chief of Staff capped to three SDK
turns on the core read tier. Inspect WorkItem `workflow_sdk_usage` events after
each smoke before approving another live run.

The July 4, 2026 open/default Slack smoke for the NeuroFlow Chief-of-Staff
research -> opportunity -> thread-local outreach workflow completed through the
backend-selected graph path and produced an LLM-constrained Slack-thread-local
draft without creating an approval queue item, Gmail draft, send, post outside
the thread, or external write. It also exposed two planner boundaries that now
have regression coverage: "comparison smoke" must not be treated as a
comparison-format/multi-target request, and source ids returned by the live
Outreach Composer draft must be normalized to the approved WorkItem/source
context before strict source validation. Do not run forced-off/forced-on live
comparison variants without a fresh explicit live-test budget.

A later July 4, 2026 direct live comparison used the same NeuroFlow prompt with
manual `chief_of_staff` routing and three backend modes: open/default,
`KNI_BUSINESS_AGENTS_LANGGRAPH=false`, and `KNI_BUSINESS_AGENTS_LANGGRAPH=true`.
It was useful as a prompt/orchestration diagnostic but not as a completed
multi-agent comparison. All three variants stopped at Chief of Staff with only a
`chief_of_staff_plan`; neither the graph variants nor the forced-off variant
continued to Business Research, Opportunity Scout, or Outreach Composer. The
open/default and forced-true variants recorded graph node paths ending at
`run_chief_of_staff -> finalize_step -> approval_checkpoint`; forced-false
stayed on the legacy manager loop and reported missing downstream research and
outreach stages. The observed live SDK usage was six model requests total,
because the forced-false path also invoked the user-response synthesizer, so do
not run additional live variants without a renewed budget. The practical lesson:
this direct prompt is too conservative for comparing graph-edge quality. A
better next comparison prompt or harness should explicitly preserve the same
backend-selected multi-step handoff surface being evaluated, while still keeping
the user-facing prompt natural and side-effect-free.

Graph explainability should come from graph completion metadata by default:
node path, executed routes, stop reason, checkpoint payload, blockers, artifacts,
missing downstream stages, and explicit `send_enabled=false` /
`external_writes_enabled=false`. Do not copy the legacy manager-loop behavior of
adding an uncontrolled live user-response synthesis call to make the answer more
verbose. Smoke-limited comparison runs should keep that final synthesis disabled
unless a fresh live-test budget explicitly allows it.
Current manager-loop graph runs attach `graph_completion_review` to saved
`langgraph_orchestration` metadata, saved `langgraph_manager_loop_completed`
metadata when the loop finalizes without an approval checkpoint, and feedback
callback payloads. Renderers should use that object to explain completed stages,
skipped/no-draft stages, approval checkpoints, and side-effect boundaries.

## Quality Validation

LangGraph is successful only if the user-facing work improves or stays equally
safe while preserving the same output contracts. Do not treat "graph ran" as a
quality result. For comparison runs, score the visible answer and persisted
WorkItem state against eval-derived criteria:

- route fidelity: the run chose the correct specialist stages and did not skip
  a requested research, opportunity, triage, context, or draft-only handoff
- evidence fidelity: source-backed claims are visible, unsupported claims and
  stale/weak evidence are flagged, and source limitations survive handoffs
- decision quality: opportunity scores, why-now reasoning, duplicate/identity
  checks, and recommended next actions match the relevant eval expectations
- operator value: the Slack/CLI answer explains what was learned, what is still
  missing, and the next safe action without dumping raw route metadata
- safety fidelity: no send, post, schedule, Gmail draft, Airtable/CRM write, or
  external artifact is created without explicit scoped approval
- efficiency: graph mode should not add avoidable SDK calls just to synthesize a
  more verbose final answer

Use existing eval prompts as comparison anchors. Good candidates include
`evals/static/opportunity_scout_cases.json::behavioral_health_topic`,
`evals/local/opportunity_scoring.jsonl::score_behavioral_health_high`, and
`evals/local/skill_task_matrix.jsonl::orchestrator_slack_route_and_package`.
Use `langgraph_quality_markers()`, `compare_langgraph_quality()`, and
`render_langgraph_quality_comparison()` from
`src/keystone_agents/langgraph_quality.py` to turn graph-off and
backend-selected runs into deterministic comparison markers before judging live
prose. The next Slack comparison should start offline with the same prompt and
fixture context in graph-off and backend-selected modes, then run at most one
open Slack/API variant after the offline result shows the graph can complete
the intended stages, preserve route/safety fidelity, and add durable context,
stages, or explainability.

The repo-local offline command is:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py --require-ready
```

The selected edge program can be inspected without running any WorkItem
comparison:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --edge-inventory --json --require-ready
```

That inventory is a structural readiness contract. It names the selected
durable edges, their docs/tests surfaces, required invariants, and the approved
live-smoke boundary: offline first, serial live runs, an operator-approved live
API test budget of five comparison attempts, no live search by default, and no
sends or external writes. It does not claim the graph produces better
user-facing prose; use the live-output review packet below for that.

The live API test budget is separate from internal SDK request telemetry. A
single WorkItem run may emit multiple `workflow_sdk_usage` events, and those
events are used as an over-orchestration review signal rather than as the
session-level live-test budget. For bounded smoke, the current per-run review
threshold is `internal_sdk_request_review_threshold_per_run = 4`. A run above
that threshold is review-blocked until inspected. Raising the threshold toward
`8` is only a future option for complex Chief-led or multi-specialist routes
after trace evidence shows the additional calls improve relevance, detail,
planning quality, or execution quality.

Before any live Slack/API comparison, run the aggregate selected-scenario gate:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --all-scenarios --require-ready
```

This runs every offline comparison scenario referenced by the selected edge
inventory and fails if any selected scenario is missing, not ready for bounded
live smoke, or carries regression markers. It is the quickest way to confirm
the implemented edge set is still coherent before spending any live SDK budget.

It disables live SDK/search calls, runs graph-off and backend-selected variants,
and prints the quality comparison summary. The default
`research-opportunity` scenario exercises Business Research -> Opportunity
Scout. The `outreach-checkpoint` scenario exercises Business Research ->
Opportunity Scout -> Outreach Composer -> approval checkpoint and verifies that
the blocked no-send/no-write outcome is preserved while graph mode adds
checkpoint/stage explainability. The `rss-opportunity` scenario seeds read-only
RSS context and verifies that the graph can intentionally move from a graph-off
Chief planning fallback to the expected Opportunity Scout route with context
evidence staged:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --scenario outreach-checkpoint --require-ready

.venv/bin/python scripts/compare_langgraph_quality.py \
  --scenario rss-opportunity --require-ready
```

The `gmail-research-outreach` scenario exercises the cross-agent collaboration
test-pack shape: Gmail Triage -> Business Research -> Outreach Composer ->
approval checkpoint with a sanitized inbound inquiry. It should remain blocked
until claims or context are approved for drafting:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --scenario gmail-research-outreach --require-ready
```

For context-agent evidence paths, use both `rss-opportunity` and
`preprints-zotero-research`. The first proves RSS announcement context can hand
off into Opportunity Scout. The second proves preprints history and Zotero
context can both stage read-only evidence before Business Research:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --scenario preprints-zotero-research --require-ready
```

The `chief-context-opportunity` scenario is a structural Chief-of-Staff
coordination proof. It starts at Chief, uses explicit Chief -> Opportunity Scout
handoff notation for deterministic dry-run delegation, stages RSS and Zotero
context in the backend-selected graph, and verifies that Opportunity Scout can
complete with source context while the graph-off control stops for missing
evidence:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --scenario chief-context-opportunity --require-ready
```

The `gmail-research-thread-draft` scenario exercises the same Gmail -> Research
-> Outreach handoff, but asks for Slack-thread-local sample outreach for review.
This is the better output-quality smoke candidate because it should produce a
reviewable draft without sending, creating a Gmail draft, posting outside the
thread, or writing external systems:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --scenario gmail-research-thread-draft \
  --include-output-rubric \
  --include-live-smoke-plan \
  --require-ready
```

Use `--json` for structured markers and `--database-dir` when the temporary
SQLite files should be retained for inspection.

Use `--include-output-rubric` before a live Slack/API comparison. The offline
quality gate is necessary but not sufficient: the final decision must compare
the visible graph-off and graph outputs for usefulness, detail, relevance,
evidence quality, safety/permission clarity, and API-call efficiency. The
script includes a live-output review packet with the control and graph output
surfaces, but its decision remains `unreviewed` until the actual Slack/API
responses are inspected. A graph run should count as better only if it improves
usefulness, relevance, evidence quality, and safety/permission clarity without
adding unnecessary model calls or verbose but low-value prose.
The script also emits a deterministic `live_output_review_decision` object. It
stays `unreviewed` until each criterion is scored as `graph`, `control`, or
`tie`; it returns `graph_better` only when the graph wins every minimum
criterion and does not lose efficiency.
After live Slack/API output is captured, save the packet JSON, fill in
`control_observation`, `graph_observation`, and `winner` for each review
question, then finalize it without spending another API call:

```bash
.venv/bin/python scripts/compare_langgraph_quality.py \
  --scenario gmail-research-thread-draft \
  --write-review-packet artifacts/langgraph-live-review-packet.json \
  --require-ready

.venv/bin/python scripts/compare_langgraph_quality.py \
  --write-evidence-template artifacts/langgraph-open-default-evidence.json \
  --evidence-mode open_default_backend_selected

# If the Slack run has already produced a WorkItem id, prefill DB-backed fields
# from local SQLite before adding the Slack permalink and visible output:
.venv/bin/python scripts/compare_langgraph_quality.py \
  --evidence-from-work-item wi_example \
  --evidence-mode open_default_backend_selected \
  --write-evidence-template artifacts/langgraph-open-default-evidence.json

# Or merge those DB-backed fields directly into the packet, then manually add
# the Slack permalink, operator display fields, visible output, and side-effect
# review evidence before final scoring:
.venv/bin/python scripts/compare_langgraph_quality.py \
  --review-packet artifacts/langgraph-live-review-packet.json \
  --merge-mode-evidence open_default_backend_selected \
  --evidence-from-work-item wi_example \
  --write-review-packet artifacts/langgraph-live-review-packet.json

# After the open/default Slack output is captured, save a compact evidence JSON
# with Slack permalink/timestamps, route/status, WorkItem id, operator_status,
# slack_display_title, visible output, source_urls, side_effects,
# side_effects_reviewed=true, workflow_sdk_usage_events for every SDK usage
# event from the run, and workflow_sdk_usage_event as the latest event for
# backward compatibility. For forced comparison modes, include
# --actual-environment KEYSTONE_WORKITEM_LANGGRAPH=false or true with the
# captured runner value.
# For forced_langgraph_true, also include the langgraph_orchestration event from
# the same WorkItem run. Merge evidence into the packet without rerunning a model:
.venv/bin/python scripts/compare_langgraph_quality.py \
  --review-packet artifacts/langgraph-live-review-packet.json \
  --merge-mode-evidence open_default_backend_selected \
  --evidence-json artifacts/langgraph-open-default-evidence.json \
  --write-review-packet artifacts/langgraph-live-review-packet.json

# Only after the open/default checkpoint reports ready should forced comparison
# modes be run and copied into the packet. The checkpoint also verifies that the
# open/default run left enough live API test attempts for both forced modes and
# that its per-run internal SDK request count did not exceed the bounded-smoke
# review threshold.
# After all Slack outputs, WorkItem ids, route/status, operator display fields,
# and usage events are present:
.venv/bin/python scripts/compare_langgraph_quality.py \
  --review-packet artifacts/langgraph-live-review-packet.json \
  --require-graph-better

# After each evidence merge, print the saved packet's next live step before
# spending another call:
.venv/bin/python scripts/compare_langgraph_quality.py \
  --review-packet artifacts/langgraph-live-review-packet.json \
  --include-live-smoke-plan

# After all three mode evidence slots are complete and the Slack outputs have
# been inspected, score each criterion deterministically without hand-editing
# the review packet:
.venv/bin/python scripts/compare_langgraph_quality.py \
  --review-packet artifacts/langgraph-live-review-packet.json \
  --score-review usefulness=graph \
  --review-observation "usefulness.control=control was less actionable" \
  --review-observation "usefulness.graph=graph produced a clearer decision" \
  --score-review detail=graph \
  --review-observation "detail.control=control had fewer stage details" \
  --review-observation "detail.graph=graph preserved useful handoff detail" \
  --score-review relevance=graph \
  --review-observation "relevance.control=control was less specific" \
  --review-observation "relevance.graph=graph stayed on the requested thread" \
  --score-review evidence_quality=graph \
  --review-observation "evidence_quality.control=control preserved fewer evidence gaps" \
  --review-observation "evidence_quality.graph=graph preserved evidence gaps" \
  --score-review safety_and_permissions=graph \
  --review-observation "safety_and_permissions.control=control was safe" \
  --review-observation "safety_and_permissions.graph=graph was safe and clearer" \
  --score-review efficiency=tie \
  --review-observation "efficiency.control=control used comparable calls" \
  --review-observation "efficiency.graph=graph used comparable calls" \
  --write-review-packet artifacts/langgraph-live-review-packet.json \
  --require-graph-better
```

Use `--json` with `--review-packet` when the finalized decision should be stored
as structured evidence. The exported packet includes evidence slots for the
open/default, forced-off, and forced-on modes; fill in Slack timestamps or
permalinks, WorkItem ids, route/status, `operator_status`,
`slack_display_title`, visible output, `source_urls`, `side_effects`,
`side_effects_reviewed=true`, and `workflow_sdk_usage` details from the
inspected run before scoring the criteria. For forced comparison modes, also
capture `actual_environment.KEYSTONE_WORKITEM_LANGGRAPH` with
`--actual-environment KEYSTONE_WORKITEM_LANGGRAPH=false` for forced-off and
`--actual-environment KEYSTONE_WORKITEM_LANGGRAPH=true` for forced-on. The
option is repeatable for additional captured environment keys.
For the forced-on mode, also fill `langgraph_orchestration_event` from the same
WorkItem's event stream. The forced-off control mode must not include
`langgraph_orchestration_event`; if it does, the packet is not a valid
graph-off comparison. The forced-on event must use schema
`keystone.langgraph.orchestration.v1`.
Use `--review-packet ... --include-live-smoke-plan` after each evidence merge to
print the saved packet's next recommended live step. Once open/default evidence
passes, the next step should be `forced_langgraph_false_control`; after the
control evidence passes, the next step should be `forced_langgraph_true`; after
all three evidence slots pass, the next step should be review scoring rather
than another live run.
Valid `workflow_sdk_usage` evidence must use schema
`keystone.workflow_sdk_usage.v1`, include `usage.requests`, include a nonempty
`cost` object with an estimate, amount, or cost-source field, and include
`request_cache` evidence such as `static_prefix_sha256`,
`dynamic_prompt_sha256`, or `prompt_cache_key_hash`. When a WorkItem emits more
than one SDK usage event, copy every event into `workflow_sdk_usage_events`; the
legacy `workflow_sdk_usage_event` field should contain the latest event only.
Use repeated `--score-review CRITERION=WINNER` flags to update the human
output-quality review after inspecting the Slack outputs. Valid winners are
`graph`, `control`, `tie`, and `unreviewed`; scoring still cannot bypass missing
run evidence, side-effect review, usage/cost evidence, or the all-scenarios
offline gate.
Each scored criterion also needs reviewer observations for both outputs. Use
`--review-observation CRITERION.control=TEXT` and
`--review-observation CRITERION.graph=TEXT` so the final packet records why the
winner was chosen.
The finalizer treats missing run evidence as a blocker: `--require-graph-better`
will not pass just because the review questions were marked in the graph's
favor. It also requires the packet-level all-scenarios offline gate to be true,
which is populated by the normal scenario command after
`--all-scenarios --require-ready` succeeds. At minimum, the forced-off and
forced-on comparison slots need Slack permalinks, WorkItem ids, route/status,
visible output, source URLs, `side_effects` flags proving no
send/draft/post/schedule/write occurred, `side_effects_reviewed=true`,
`actual_environment.KEYSTONE_WORKITEM_LANGGRAPH`, and `workflow_sdk_usage`
evidence with usage, cost, and request-cache proof before a positive decision
can be accepted. The forced-on comparison slot additionally needs
`langgraph_orchestration_event`; without it, a positive review would only prove
"a run completed," not that the run actually exercised the LangGraph path.
The finalizer also inspects `usage.requests` for every populated mode evidence
slot, including the open/default run. It prefers `workflow_sdk_usage_events`
when present and falls back to the legacy single `workflow_sdk_usage_event`.
`graph_better` is blocked when any single run exceeds the current per-run
internal SDK request threshold. This is separate from the operator-approved live
API test count, which controls how many live comparison attempts may be run in
the session.
The `--evidence-from-work-item` helper is intentionally partial. It can prefill
route/status, source URLs, SDK usage events, and the latest LangGraph event from
SQLite, but it leaves `slack_permalink`, `operator_status`,
`slack_display_title`, `visible_output`, and `side_effects_reviewed` for manual
Slack inspection. It can either write an evidence file or merge those DB-backed
fields directly into a review packet with `--review-packet --merge-mode-evidence`.
Evidence template writing and merge both validate graph-mode consistency for
JSON evidence files and DB-backed WorkItem extraction: a forced-off control slot
must not receive `langgraph_orchestration_event`, and any provided graph event
must use schema `keystone.langgraph.orchestration.v1`. Legacy graph events that
predate the schema field are accepted only when they include
`runtime=langgraph` and a nonempty `node_path`. DB-backed forced-on evidence
must include a `langgraph_orchestration_event` from the same WorkItem; otherwise
the evidence does not prove that the forced graph path ran.
Open/default backend-selected evidence may include graph evidence when the
backend selected LangGraph, but a malformed graph event still blocks the
open/default checkpoint.
When merging into a packet that already has `side_effects_reviewed=true`, the
partial DB extraction must not downgrade that manual review flag.
Do not treat DB extraction alone as proof that user-visible Slack output was
useful or that side effects were reviewed.

That output-quality review is the real value test. Route fidelity, graph events,
and approval gates only show that the architecture is safe enough to test. They
do not prove the graph is useful. The graph should be treated as a downstream
improvement only when the visible Slack/API answer gives a clearer operator
decision, stays specific to the requested company or thread, preserves
source-backed evidence and gaps across handoffs, includes the requested
draft/work product when safety gates allow it, and avoids using route/debug
metadata as the main substance. If the graph answer is merely longer, more
instrumented, or equally generic, the comparison should be marked as not proven
even if the graph path completed.

Use `--include-live-smoke-plan` to print the approved live-test boundary before
spending API calls. For the next comparison pass, the plan should allow only one
open/default backend-selected run before review, with `live_sdk=true`,
`live_search=false`, `cost_profile=slack_smoke_limited`,
`hosted_web_search_max_calls=0`, manager-loop repair disabled, contact
enrichment disabled, and all send/write/draft/post-outside-thread flags false.
The plan is live-ready only when the selected-scenario aggregate gate is present
and true; helper runs that skip that gate must report `do_not_run_live` even if
their single scenario comparison is structurally ready.
The same plan enforces the operator-approved live API test budget:
`max_live_sdk_calls` is kept as the legacy field name, but it represents the
number of live comparison attempts allowed by the operator, not the maximum
internal SDK requests inside one WorkItem run. It must be between 1 and 5, and
any larger budget keeps the recommended next run at `do_not_run_live`.
After that run, inspect the full Slack/API output, WorkItem route/status,
`langgraph_orchestration`, and `workflow_sdk_usage` with usage, cost, and
request-cache proof before deciding whether a forced-off control run and then a
forced-on graph run are worth spending from the remaining live API test budget.

The open/default run has its own checkpoint. After the first Slack run, fill the
`open_default_backend_selected` evidence slot in the review packet with the
Slack permalink, WorkItem id, route/status, visible output, source URLs,
`side_effects`, `side_effects_reviewed=true`, and `workflow_sdk_usage` with
usage, cost, and request-cache proof. Also copy `operator_status` and
`slack_display_title` from the Slack/KBA result payload when available. The
checkpoint must report
`ready_for_forced_comparison=true` before running either forced comparison mode.
It also reports internal SDK request count, the per-run internal request
threshold, live API test attempts used/remaining, and the minimum two-attempt
reserve needed for the forced graph-off and forced graph-on modes. If the
open/default run leaves fewer than two live API test attempts inside the
approved five-attempt budget, stop instead of running forced comparisons. If
the open/default run exceeds the current per-run internal request threshold,
stop and inspect for over-orchestration before spending another live attempt.
If the open/default status is failed/blocked, route differs from the expected
offline graph route, SDK usage is missing, or the visible output implies a send,
Gmail draft, external post, schedule, publish, or write, stop and inspect/fix
that run instead of spending on forced comparisons.

For Slack inspection, distinguish canonical state from operator-facing status.
The WorkItem may still carry `status=blocked` when a gate is waiting for
operator input, but Slack should render `operator_status=needs_input` and
`slack_display_title=Business Agents Need Input` when those fields are present.
The visible body should ask for the missing focus, target, source context, or
approval scope in natural language. A Slack run that only says "blocked" without
the next useful input is not a successful output-quality comparison, even if the
underlying gate behaved correctly.

Keep live comparison mode outside the visible Slack prompt. The Slack prompt
should remain a natural operator ask, for example the `gmail-research-thread-draft`
scenario text. Do not put harness labels such as "open/default", "forced true",
"forced false", "LangGraph", live SDK approval, live-search denial, or
no-side-effect inventories into the user-facing request; apply those modes and
constraints through backend configuration or the test harness. The live-smoke
prompt template strips known harness labels, live SDK approval text,
live-search denial text, duplicate `@KNI`, and long no-side-effect inventories
before producing Slack wording. If the rendered Slack prompt still contains
harness labels or control text after sanitization, the live-smoke plan blocks
with a prompt-cleanliness blocker, `--require-ready` exits nonzero, and the plan
recommends `do_not_run_live`. The rendered live-smoke plan prints the prompt
cleanliness blockers separately from the generic blocker list so the operator
can fix the visible Slack wording before spending an API call. For the
three-mode comparison, leave
`KEYSTONE_WORKITEM_LANGGRAPH` unset for the open/default run, set it to `false`
for the graph-off control, and set it to `true` for the forced-on graph run. If
prompt wording changes the route, stop and treat that as a failed comparison
setup rather than a meaningful output-quality result.

The July 4, 2026 15:50 EDT open/default Slack smoke used generic "prepare
draft-only outreach" wording for the Gmail -> Research -> Outreach path. It
correctly selected the graph and ran
`gmail_triage -> business_research -> outreach_composer -> approval_checkpoint`
under `slack_smoke_limited` with live search off and hosted web search capped at
0, but the visible output was not useful enough for comparison: Outreach
Composer blocked with `outreach_requires_approved_context` instead of producing
a reviewable thread-local draft. Local evidence also showed no
`workflow_sdk_usage` event because no live SDK synthesis actually ran on that
blocked fixture path. Treat that run as a useful diagnostic, not a successful
output-quality comparison. The next live smoke should use the
`gmail-research-thread-draft` wording and must still stop after the open/default
run until the Slack output and usage events are inspected.

The overly restrictive part of that failure was the Outreach Composer gate, not
the no-send policy. Slack-origin requests that ask for draft-only outreach for
review and explicitly block sends, Gmail drafts, external posts, schedules, or
writes may now create a thread-local review draft without a selected
external-use-approved company profile. External sends, Gmail drafts, posts
outside the current thread, CRM/Airtable/Drive/Sheets writes, schedules, and
publication still require their normal approvals.

A later open/default attempt with an explicit thread-local draft request hit an
OpenAI provider 500 before completion. It also showed that adding test-harness
labels to the Slack text can distort routing before the comparison reaches the
intended Gmail -> Research -> Outreach flow. Treat that as a setup diagnostic:
the next attempt should use the natural prompt template emitted by
`scripts/compare_langgraph_quality.py --scenario gmail-research-thread-draft --include-live-smoke-plan`.

A July 4 follow-up confirmed a second setup problem: direct `gmail_triage`
Slack asks with inline email plus downstream research/outreach were entering the
legacy named Gmail child process before the WorkItem manager loop. That path is
not the graph workflow under evaluation. KBA now promotes parseable inline
Gmail requests that ask for downstream research or outreach into the WorkItem
manager loop, while simple single-agent Gmail triage can still use the direct
Gmail path.

Current offline readiness before the next live probe:

- Backend selection now explicitly covers the natural
  `gmail-research-thread-draft` prompt family, with no user-visible LangGraph
  flag required.
- `tests/test_langgraph_workflow.py` passes locally and covers simple-off-graph,
  planning-only-off-graph, graph-worthy Gmail/research/thread-draft, context
  staging, and Chief-of-Staff coordination decisions.
- `tests/test_langgraph_quality.py`, `tests/test_slack_action_contract.py`, and
  `tests/test_slack_agent_actions.py` pass together with the Slack operator
  display fields in the review packet.
- `scripts/compare_langgraph_quality.py --all-scenarios --require-ready`
  reports `Ready: yes`, seven selected scenarios, and no blockers.
- The comparison command still reports `Ready for live smoke: yes` and
  `Recommended next run: open_default_backend_selected_only`.
- Do not run forced graph-off or graph-on comparisons until the open/default
  Slack run has a permalink, WorkItem id, route/status, `operator_status`,
  `slack_display_title`, visible output, source URLs, `side_effects`,
  `side_effects_reviewed=true`, and `workflow_sdk_usage`
  usage/cost/request-cache proof captured in the review packet and
  `langgraph_open_smoke_checkpoint()` reports ready.

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

## Migration Shape

The current implementation replaces the previous side-wrapper shape with
graph-native nodes for WorkItem preparation, route-specific specialist
execution, finalization, bounded manager-loop continuation, and approval
checkpointing. Each graph node accepts typed serializable state, calls existing
workflow utilities, stores structured output, and returns the next state.
Reports remain generated from structured outputs, not graph internals.
