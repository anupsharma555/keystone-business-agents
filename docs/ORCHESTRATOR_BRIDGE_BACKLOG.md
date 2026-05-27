# Orchestrator Bridge Backlog

Current review backlog for the Orchestrator-first agent architecture, Slack bridge handoff, WorkItem manager loop, and local child-process/runtime behavior.

Timestamp policy updated: `2026-05-25 12:47 EDT`.

Timestamps use local machine time in `YYYY-MM-DD HH:MM TZ` format. New findings
must include a `Found` timestamp and should keep `Fixed: pending` until a Codex
session verifies the repair. The session that verifies a fix must replace
`Fixed: pending` with its fix timestamp and evidence. Legacy rows that were
fixed before this policy may keep their retrospective status text until touched
again.

## Review Queue

- Queued: 2026-05-25 13:29 EDT
- Area: `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Next review: without live runs, walk the expected execution path for the
  current implementation across Gmail Triage, Business Research, Opportunity
  Scout, Outreach Composer, Orchestrator, and cross-agent collaboration specs.
  Focus on whether diverse natural-language asks preserve the right specialist
  route, context packs, safety gates, downstream artifacts, and Slack bridge
  lifecycle status before live testing.

## High-Value Next Review Priorities

- Updated: 2026-05-25 18:05 EDT
- Priority 1: keep proving real CLI/Slack compact-preflight behavior, not only
  hand-built WorkItem fixtures. This has repeatedly exposed false-positive
  regression coverage around Orchestrator plan summaries.
- Priority 2: verify side-effect wording lands on the owning specialist's safety
  gate. `send`, `save`, `post`, `label`, and `draft` requests should produce
  precise no-send/no-write/approval blockers rather than generic unsupported
  clarification.
- Priority 3: focus Slack bridge checks on lifecycle truthfulness and process
  cleanup: modal/source-thread status should match child WorkItem status, stderr
  feedback must keep draining if callbacks fail, and timed-out child process
  groups must be killed.
- Priority 4: before live runs, test that every agent-specific request has the
  information and tool surface needed for a precise response: selected Slack or
  Gmail context, source bundles, approved outreach facts, live-search flags, and
  write approvals.

## Open Findings

### P1 - Other specialists need reasoning-based Orchestrator success criteria

- Found: 2026-05-25 19:04 EDT
- Fixed: 2026-05-25 19:06 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`, all specialist WorkItem
  routes.
- Issue: Business Research now receives transient Orchestrator guidance for
  Slack context and current-year depth, but the other specialists still rely too
  much on generic review language. The goal should not be a hardcoded script for
  each agent; it should be a transient reasoning contract between Orchestrator
  and the specialist.
- Expected fix: Expand the per-run Orchestrator memo with reasoning-based
  instructions that make the specialist derive task-specific success criteria:
  target/entity, exact question, needed context/tools, source freshness/depth,
  output shape, safety gates, downstream readiness, and blockers. Keep
  deterministic gates only for conditions that should not be waved away, such as
  missing selected context, missing approved facts, shallow current-year
  evidence, or unsafe side effects.
- Fix: The per-run Orchestrator memo now sends all specialists an advisory,
  reasoning-based success frame. It asks the specialist to derive criteria from
  the raw request, selected route, attached context, safety policy, and available
  tools, then check target/entity, exact question, evidence depth, output shape,
  blockers, citations, no-send/no-write gates, and downstream readiness. Context
  and temporal nudges remain lightweight; hard enforcement stays in deterministic
  guards.
- Verification: Added focused coverage that the memo asks the specialist to
  derive task-specific criteria, consider available tools, return precise
  blockers when needed, and reason about freshness for current-year requests.

### P1 - Orchestrator-specialist review should support a repair turn

- Found: 2026-05-25 19:04 EDT
- Fixed: 2026-05-25 19:14 EDT
- Status: fixed
- Area: manager loop review and specialist retry/repair flow.
- Issue: The manager loop can block or warn after Orchestrator review, but a
  blocked review currently behaves mostly as a stop state with a next action. It
  does not yet provide a clean bounded repair turn where the specialist receives
  Orchestrator feedback and produces a revised output in the same run.
- Expected fix: Add a bounded repair pass for selected hard-review failures:
  attach the review gaps to the transient Orchestrator memo, rerun the same
  specialist once when side-effect-safe, and only then block if the output still
  misses required context/depth/source gates. Keep no-send/no-write gates
  authoritative.
- Fix: Hard Orchestrator review failures now enter a bounded repair exchange
  before blocking when the specialist step is otherwise side-effect-safe. The
  latest review gaps are persisted in WorkItem metadata, injected into the next
  specialist Orchestrator memo, and the same specialist route is retried once.
  If the repaired pass satisfies review/depth gates, the WorkItem continues; if
  not, the original `manager_loop_review_failed` blocker is applied.
- Verification: `tests/test_workflow_runner.py` covers successful repair after
  a shallow 2026 research pass, final blocking after an unrepaired failure,
  Orchestrator feedback injection into the specialist memo, and no-send/no-write
  gate preservation through existing manager-loop tests.

### P2 - Temporal-depth and tool-budget policy should be explicit across agents

- Found: 2026-05-25 19:04 EDT
- Fixed: pending
- Status: open
- Area: retrieval policy, Orchestrator memo, specialist runtime budget.
- Issue: Requests with `current`, `latest`, `2026`, `recent`, or similar wording
  imply deeper reasoning and fresher source requirements, but the depth policy is
  not yet uniformly represented across all agents.
- Expected fix: Make temporal intent a shared transient policy: request recent
  sources, independent validation when available, source extraction/read-through
  before synthesis, and explicit “not enough evidence yet” output when the tool
  budget is exhausted.

### P2 - Unknown temporary model aliases should fail early or have pricing metadata

- Found: 2026-05-25 19:27 EDT
- Fixed: pending
- Status: open
- Area: runtime model policy, manual planner cost guardrails.
- Issue: Temporarily setting agents to `gpt-5.5` caused the live manual planner
  to fall back because local pricing metadata had no matching model entry. That
  did not appear to be the direct timeout crash, but it weakened early planning
  and target extraction for the Slack test.
- Expected fix: Either add reviewed pricing metadata before temporary model
  tests, or make unknown live model aliases fail before planner execution with a
  precise configuration error and rollback instruction.

### P1 - Prior-post Slack context can be dropped for named Business Research requests

- Found: 2026-05-25 18:51 EDT
- Fixed: 2026-05-25 19:02 EDT
- Status: fixed
- Area: sibling `keystone-slack` Slack app-mention/history context path,
  `src/keystone_agents/workflow_runner.py`, manager-loop routing/review gates.
- Issue: A Slack request named `business research analyst` and referred to the
  prior post (`company being discussed: OpenEvidence`), but the stored KBA run
  had `external_context: {}`, target `the company being discussed in the
  selected Slack thread`, and final route `opportunity_scout`. Business Research
  searched for the placeholder target, retrieved Slack.com sources, and the
  manager loop then advanced to generic Opportunity Scout results.
- Evidence: Slack run `sbar_fd3fb101ddaa4a9bb844000da623f217`, WorkItem
  `wi_523553d13ec040febb811cd3f0cce24f`. The WorkItem first attached a
  Business Research company profile for the placeholder target using Slack.com
  sources, then attached opportunity artifacts for unrelated targets including
  Stellantis/Wayve, Cummins, Deutsche Telekom, Isomorphic Labs, and Oasys.
- Impact: This violates the selected-context architecture test: the named
  specialist did not receive the operator-supplied company, unresolved target
  readiness was marked ready, and failed Business Research review did not stop
  downstream Opportunity Scout advancement.
- Expected fix: When Slack text says `selected Slack thread`, `prior post`,
  `above`, or `company being discussed`, attach bounded same-thread/prior-message
  context or return a precise `selected_slack_context_required` blocker before
  live search. KBA should treat placeholder targets like `the company being
  discussed...` as not research-ready unless Slack/context-pack evidence resolves
  the entity, and manager loop should not advance to Opportunity Scout after a
  failed/unresolved Business Research pass.
- Fix: Sibling Slack app-mention history detection now treats selected/prior
  Slack context references as current-channel history requests and passes a
  `keystone.slack.history_context.v1` context file into the KBA WorkItem run.
  KBA ingests that context, resolves explicit prior-post labels such as
  `company being discussed: OpenEvidence`, blocks unresolved placeholder targets
  before live search, and injects a transient per-task specialist checklist into
  the Orchestrator memo.
- Depth follow-up: Manager-loop review now hard-blocks current-year Business
  Research profiles that rely only on company-controlled sources, asking for a
  deeper 2026/current activity pass with independent sources before the artifact
  is treated as decision-ready.
- Verification: KBA focused tests passed:
  `tests/test_slack_agent_actions.py::test_cli_ask_history_context_resolves_prior_post_company`,
  `tests/test_workflow_runner.py::test_manager_loop_blocks_current_research_with_only_company_controlled_sources`,
  plus adjacent WorkItem/Slack context regression tests. Sibling `keystone-slack`
  focused unittest coverage passed for prior-post history attachment and context
  file handoff. `git diff --check` passed in both repos.

## Completed Findings (Consolidated)

Completed items are kept compact here for scanability. Each row preserves the
latest useful fix timestamp and the strongest current evidence; the detailed
historical entries below remain as the audit trail until they are archived.

| Status | Finding | Fixed | Current evidence |
| --- | --- | --- | --- |
| Fixed | Slack WorkItem action buttons now use the integrated Orchestrator-manager loop instead of a one-step specialist runner. | 2026-05-25 19:55 EDT | `continue`, `more research`, `run again`, and revision-style WorkItem actions route through the same bounded manager-loop path as initial Slack asks, including optional LangGraph wrapping. Backend telemetry now has named metric `keystone.manager_loop.efficiency`/`v1`, records latency bucket, repair rate, completion signal, and persists compact `manager_loop_efficiency` memory for trend comparison across runs. Focused Slack, LangGraph, manager-loop, and memory tests pass. |
| Fixed | Current-year Business Research now deepens on the first pass and fixture records no longer count as independent validation. | 2026-05-25 19:43 EDT | Prompts like `what OpenEvidence is doing in 2026` add current-activity queries up front; manager-loop depth checks ignore `fixture` records when deciding whether independent sources exist. Focused workflow tests pass. |
| Fixed | Live search provider/query timeouts no longer hard-fail Slack WorkItems when retrieval can safely degrade. | 2026-05-25 19:27 EDT | Hybrid retrieval now records recoverable provider failures in backend telemetry and returns zero/partial results instead of raising; Slack-safe diagnostics only surface a generic outage note when all attempted providers fail with no results. Focused retrieval-policy and diagnostics tests pass. |
| Fixed | Planning-first Orchestrator asks now return a real workflow plan on compact preflight paths. | 2026-05-25 18:05 EDT | Real preflight probe returns route `orchestrator`, artifact `orchestrator_plan_summary`, and blocker `manager_loop_outreach_not_drafted`; focused WorkItem planning tests pass. |
| Fixed | Direct outreach `send` wording reaches Outreach Composer's draft-only/no-send gate instead of generic clarification. | 2026-05-25 14:45 EDT | `Send the strongest version to the CEO.` routes to `outreach_composer` and blocks on missing approved context/send approval; manual-plan and WorkItem tests pass. |
| Fixed | Direct Slack WorkItem actions now carry Orchestrator review evidence after specialist execution. | 2026-05-25 14:45 EDT | Slack action runs record `orchestrator_action_review` metadata while preserving deterministic action routes; Slack action tests pass. |
| Fixed | Slack `continue WorkItem` actions attach Orchestrator preflight context before advancement. | 2026-05-25 14:43 EDT | Continue path loads the existing WorkItem route, attaches compact preflight payload, and preserves the deterministic route; focused Slack continue test passes. |
| Fixed | Slack revision and more-research actions attach compact Orchestrator preflight context. | 2026-05-25 14:39 EDT | `_advance_work_item_for_intent()` passes the resolved specialist as advisory requested agent and records preflight payload; Slack revise/more-research tests pass. |
| Fixed | Broad Gmail follow-up asks land on Gmail readiness gates instead of unsupported clarification. | 2026-05-25 14:35 EDT | Follow-up/reply prompts route to `gmail_triage` and block on thread context readiness; matrix and focused workflow tests pass. |
| Fixed | Source-bundle-only Business Research requests no longer fabricate profiles without supplied evidence. | 2026-05-25 14:43 EDT | Source-bundle prompts block on `source_bundle_required` when no artifact/context is supplied; focused workflow tests pass. |
| Fixed | Opportunity Scout partnership asks route correctly and avoid generic weak matches when evidence is insufficient. | 2026-05-25 14:35 EDT | Partnership prompts route to `opportunity_scout`; no-result/narrow-match cases block rather than padding with weak generic opportunities. |
| Fixed | Agent-attached `search_web` expectations are aligned with the shared provider policy. | 2026-05-25 14:25 EDT | Test-pack wording now treats hosted Agents web search as a capped live research lane through shared retrieval policy, not a guaranteed per-agent attached tool in dry run. |
| Fixed | Chief of Staff operational asks route to the control-plane lane instead of clarification or research. | 2026-05-25 14:21 EDT | Manual planner recognizes operational workflow/status/plan requests as Orchestrator/Chief-of-Staff owned; focused workflow tests pass. |
| Fixed | CRM/write side-effect boundaries survive WorkItem execution and final output. | 2026-05-25 14:23 EDT | CRM save/write asks preserve `manager_loop_crm_write_blocked` and no-write approval blockers in final WorkItem output. |
| Fixed | Manager-loop review failures are advisory for ordinary dry-run research/scout runs unless they identify a hard blocker. | 2026-05-25 14:12 EDT | Review failures no longer block normal dry-run research/scout fixture paths; focused manager-loop tests pass. |
| Fixed | CLI WorkItem advance now goes through Orchestrator preflight and manager-loop review. | 2026-05-25 14:19 EDT | CLI advancement attaches compact preflight context and records review metadata instead of bypassing the control plane. |
| Fixed | Direct live child scripts have timeout, process-group isolation, and structured/redacted failure output. | 2026-05-25 12:36 EDT | Child process tests pass; process scan found no stuck KBA child runners. |
| Fixed | Slack bridge child process and feedback failures are drained, redacted, and rendered without leaking raw callback exceptions. | 2026-05-25 13:04 EDT | Keystone Slack focused tests pass for streaming feedback, stderr draining, timeout lifecycle, and Slack-visible failure redaction. |
| Fixed | Slack modal/private-metadata handoff now uses compact context pointers instead of oversized JSON payloads. | 2026-05-25 12:17 EDT | KBA and sibling `keystone-slack` modal builders use hard-size metadata fallbacks and absolute context file paths. |
| Fixed | Apify profile-search-only health/config is no longer treated as actor-missing or unconfigured. | 2026-05-25 12:50 EDT | Sibling health checks recognize profile-search-only mode and no longer lower confidence solely for missing company/people actor IDs. |

## Detailed Finding History

Historical notes below may include reopen/re-fix trails and verbose evidence.
Prefer the consolidated table above for current completed status.

### P1 - Direct outreach send asks can miss the Outreach no-send gate

- Found: 2026-05-25 14:45 EDT
- Fixed: 2026-05-25 14:45 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: A direct outbound outreach request that says `send` but clearly refers
  to an outreach draft can route to generic clarification before Outreach
  Composer sees it. The final WorkItem then reports
  `route_not_supported_in_workitem_phase` instead of the more precise
  draft-only/no-send approval gate.
- Evidence: A dry matrix probe for `Send the strongest version to the CEO.`
  produced `manual_request_plan.target_agent="clarification"`, final route
  `clarification`, status `blocked`, blocker
  `route_not_supported_in_workitem_phase`, and no Outreach Composer
  approved-context, recipient, or no-send blocker.
- Impact: The architecture goal is for agents to interpret diverse natural
  language and then hit the correct specialist safety gate. Generic
  unsupported-route output gives the operator less useful guidance than
  `outreach_requires_approved_context`, missing recipient/context, and no-send
  approval blockers.
- Expected fix: Route direct email/LinkedIn/outreach send/publish requests that
  name a recipient or draft version to Outreach Composer while preserving
  `blocked_send`/draft-only policy. The WorkItem result should block external
  send/publish, request approved facts and recipient context when missing, and
  return safe draft-only next steps rather than unsupported clarification.
- Fix: Send-side-effect detection now covers direct draft/version/recipient
  wording such as `Send the strongest version to the CEO`, and manual planning
  routes those requests to Outreach Composer with `blocked_send` and
  draft/read-only policy intact.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_direct_outreach_send_to_outreach_gate
  tests/test_workflow_runner.py::test_agent_specific_variant_requests_reach_owning_workitem_gate`.

### P1 - Direct Slack WorkItem actions lack Orchestrator output review events

- Found: 2026-05-25 14:44 EDT
- Fixed: 2026-05-25 14:45 EDT
- Status: fixed
- Area: `src/keystone_agents/slack_interactions.py`,
  `tests/test_slack_tool.py`
- Issue: Slack button-triggered WorkItem actions intentionally use
  `advance_work_item_with_optional_langgraph()` rather than the full manager
  loop so they can preserve LangGraph checkpoints and deterministic action
  routing. After preflight was attached, those direct action runs still did not
  record an Orchestrator review/eval event for the specialist output.
- Impact: The Orchestrator could read the request, but direct Slack action runs
  did not leave the same review feedback trail expected by the consolidated
  planner/control-plane goal. This made operator feedback weaker for
  deterministic Slack actions such as more research, revise draft, run again,
  and continue WorkItem.
- Expected fix: Keep the optional LangGraph/direct runner, then attach a
  lightweight deterministic Orchestrator review event to the resulting WorkItem
  using existing `review_specialist_output()` output-review logic. Preserve the
  deterministic route and no-send/no-write safety boundaries.
- Fix: Direct Slack action results now record an `orchestrator_action_review`
  event and append compact review metadata under `target.metadata`.
- Verification: `.venv/bin/python -m pytest
  tests/test_slack_tool.py::test_kba_more_research_uses_langgraph_when_enabled
  tests/test_slack_tool.py::test_kba_continue_work_item_uses_langgraph_thread_when_enabled`.

### P1 - Slack continue WorkItem actions bypass Orchestrator preflight context

- Found: 2026-05-25 14:43 EDT
- Fixed: 2026-05-25 14:43 EDT
- Status: fixed
- Area: `src/keystone_agents/slack_interactions.py`,
  `tests/test_slack_tool.py`
- Issue: The Chief of Staff Slack `continue WorkItem` action calls
  `advance_work_item_with_optional_langgraph()` with only `request_text`,
  `work_item_id`, and persistence settings. Unlike the modal run-agent path and
  Slack revision/research actions, it does not attach a compact Orchestrator
  preflight memo.
- Impact: Continuing an existing WorkItem from Slack preserves the WorkItem's
  deterministic route, but the Orchestrator does not explicitly read and record
  the continue request before the specialist step. That leaves a gap in the
  consolidated planner/control-plane trace and weakens downstream specialist
  context and review.
- Expected fix: Load the target WorkItem, run an Orchestrator preflight for the
  `continue` action with the existing route as advisory requested agent when
  available, attach the compact preflight payload to the `WorkflowRunRequest`,
  and verify the resulting `advance_started` event records the preflight.
- Fix: The Slack continue path now loads the existing WorkItem route, runs
  `run_orchestrator_preflight("continue", requested_agent=<existing route>)`,
  and attaches `compact_orchestrator_preflight_payload()` to the WorkItem
  advancement. The existing deterministic continue route remains authoritative.
- Verification: `.venv/bin/python -m pytest
  tests/test_slack_tool.py::test_kba_continue_work_item_uses_langgraph_thread_when_enabled`.

### P1 - Slack WorkItem revision actions bypass Orchestrator preflight context

- Found: 2026-05-25 14:39 EDT
- Fixed: 2026-05-25 14:39 EDT
- Status: fixed
- Area: `src/keystone_agents/slack_interactions.py`,
  `tests/test_slack_tool.py`
- Issue: Slack approval/revision buttons build deterministic WorkItem requests
  for actions such as `revise_draft` and `more_research`, but the request did
  not carry the compact Orchestrator preflight memo. The specialist route was
  deterministic and safe, but the downstream run could not see the
  Orchestrator's raw-request interpretation, advisory route rationale, or
  safety posture.
- Impact: Slack button-triggered specialist calls were not fully aligned with
  the Orchestrator-first architecture. They preserved route safety, but skipped
  the shared planner context expected by specialist runs and run review.
- Expected fix: Keep the deterministic Slack intent route, but run a dry
  Orchestrator preflight over the generated Slack action request and attach its
  compact payload to `WorkflowRunRequest.orchestrator_preflight`.
- Fix: `_advance_work_item_for_intent()` now runs
  `run_orchestrator_preflight()` with the resolved specialist as the explicit
  requested agent and attaches `compact_orchestrator_preflight_payload()` while
  preserving the Slack action's deterministic `manual_request_plan`.
- Verification: `.venv/bin/python -m pytest
  tests/test_slack_tool.py::test_kba_revise_draft_modal_submission_records_feedback_and_queues_agent`.

### P1 - Broad workflow-planning asks can still miss the Orchestrator plan on preflight paths

- Found: 2026-05-25 14:21 EDT
- Fixed: 2026-05-25 14:28 EDT
- Reopened: 2026-05-25 14:29 EDT
- Re-fixed: 2026-05-25 14:35 EDT
- Reopened: 2026-05-25 14:37 EDT
- Re-fixed: 2026-05-25 14:39 EDT
- Reopened: 2026-05-25 14:41 EDT
- Re-fixed: 2026-05-25 14:43 EDT
- Reopened: 2026-05-25 14:43 EDT
- Re-fixed: 2026-05-25 14:43 EDT
- Reopened: 2026-05-25 14:47 EDT
- Re-fixed: 2026-05-25 14:47 EDT
- Reopened: 2026-05-25 14:49 EDT
- Re-fixed: 2026-05-25 18:05 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: Expanded Orchestrator planning scenarios that ask for a safe workflow
  can be routed directly into Opportunity Scout with the planning instruction as
  the search target. When fixture search finds no opportunity for that polluted
  target, the run blocks as a Scout no-result instead of returning an
  Orchestrator workflow plan.
- Evidence: A dry manager-loop probe for `Plan the safest workflow to find
  companies, research the best candidate, and prepare outreach, but do not save
  or send anything.` produced manual plan
  `target_agent="opportunity_scout"` and `primary_target="Plan the safest
  workflow to find companies, research the best candidate, and prepare outreach,
  but do not save or send anything"`. The final WorkItem returned
  `route="opportunity_scout"`, `status="blocked"`, blockers
  `["no_opportunities_found", "manager_loop_outreach_not_drafted"]`, no
  artifacts, and summary `Opportunity Scout did not produce a source-backed
  opportunity record.`
- Impact: Diverse natural-language asks that explicitly request planning can
  look like a failed opportunity search rather than an Orchestrator-managed
  workflow. This weakens the main architecture goal that broad requests should
  be decomposed, explained, and gated before specialist execution.
- Expected fix: Detect planning-first language such as `plan the safest
  workflow`, `decide the workflow`, and `what should the agents do` as
  Orchestrator-owned unless the user explicitly asks to run a specialist. Strip
  workflow-planning text from specialist targets and attach an
  `orchestrator_plan_summary` when execution cannot proceed to all requested
  safe stages.
- Fix: Manual planning now treats planning-first workflow language as an
  Orchestrator-owned multi-agent workflow and uses a safe default Scout target
  instead of the full planning instruction. Manager-loop finalization treats
  planning-first phrasing as requiring an `orchestrator_plan_summary`, so broad
  workflow asks return an Orchestrator plan artifact even when later safe stages
  remain gated.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_planning_first_workflow_uses_safe_scout_target
  tests/test_workflow_runner.py::test_planning_first_workflow_returns_orchestrator_plan_not_polluted_scout_blocker`.
- Reopen evidence: the focused test covers the no-preflight helper path, but
  the actual CLI/Slack path carries compact `orchestrator_preflight` into
  `advance_work_item_manager_loop()`. A dry probe at 2026-05-25 14:29 EDT with
  that preflight payload returned manual target `behavioral health AI clinical
  research`, so target pollution is fixed, but final route
  `business_research_analyst`, artifacts `["company_profile",
  "contact_candidates"]`, blockers `["manager_loop_outreach_not_drafted",
  "manager_loop_review_failed"]`, and no `orchestrator_plan_summary` artifact.
  The manager-review blocker causes the plan-summary helper to return `None`
  before the planning-first summary can be attached.
- Remaining expected fix: add coverage for the preflight-carrying CLI/Slack
  shape and attach the Orchestrator plan artifact for planning-first requests
  even when downstream specialist review adds blockers. Preserve those blockers
  as plan caveats instead of replacing the plan with a specialist failure.
- Re-fix: Planning-first `orchestrator_plan_summary` generation now runs before
  advisory manager-review blockers suppress the final Orchestrator plan, while
  still preserving downstream blockers as caveats. Added a preflight-carrying
  WorkItem regression.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_planning_first_workflow_with_preflight_still_returns_orchestrator_plan`.
- Reopen evidence: the new regression passes only for its hand-built preflight
  fixture. A current dry probe using `run_orchestrator_preflight()` plus
  `compact_orchestrator_preflight_payload()` still fails for the actual
  CLI/Slack handoff shape at both default `max_steps=3` and `max_steps=5`.
  Both runs returned route `business_research_analyst`, artifacts
  `["company_profile", "contact_candidates"]`, blockers
  `["manager_loop_outreach_not_drafted", "manager_loop_review_failed"]`, and no
  `orchestrator_plan_summary`.
- Remaining expected fix: make the preflight regression use the same compact
  preflight payload produced by `run_orchestrator_preflight()`, and ensure the
  real CLI/Slack default manager-loop path returns the Orchestrator plan summary
  before or alongside downstream specialist blockers.
- Re-fix: Verified the real preflight handoff path using
  `run_orchestrator_preflight()` plus `compact_orchestrator_preflight_payload()`.
  The dry manager-loop result now returns route `orchestrator`, artifact
  `orchestrator_plan_summary`, and preserves downstream blocker
  `manager_loop_outreach_not_drafted` without falling back to a specialist-only
  failure.
- Verification: `.venv/bin/python -c 'from keystone_agents.agents.orchestrator
  import run_orchestrator_preflight; from
  keystone_agents.orchestrator.preflight_context import
  compact_orchestrator_preflight_payload; from keystone_agents.workflow_runner
  import advance_work_item_manager_loop; from keystone_agents.schemas.work_item
  import WorkflowRunRequest; ...'`.
- Reopen evidence: a fresh dry probe at 2026-05-25 14:41 EDT using the actual
  `run_orchestrator_preflight()` plus `compact_orchestrator_preflight_payload()`
  handoff still returned route `business_research_analyst`, artifacts
  `["company_profile", "contact_candidates"]`, blockers
  `["manager_loop_outreach_not_drafted", "manager_loop_review_failed"]`, and no
  `orchestrator_plan_summary` for both `max_steps=3` and `max_steps=5`.
- Re-fix: Re-ran the same actual handoff probe in the current tree. It now
  returns route `orchestrator`, artifact `orchestrator_plan_summary`, and
  blocker `manager_loop_outreach_not_drafted`.
- Verification: `.venv/bin/python -c 'from keystone_agents.agents.orchestrator
  import run_orchestrator_preflight; from
  keystone_agents.orchestrator.preflight_context import
  compact_orchestrator_preflight_payload; from keystone_agents.workflow_runner
  import advance_work_item_manager_loop; from keystone_agents.schemas.work_item
  import WorkflowRunRequest; ...'`.
- Reopen evidence: a confirmation probe at 2026-05-25 14:43 EDT still failed
  for the real compact-preflight path. Both `max_steps=3` and `max_steps=5`
  returned route `business_research_analyst`, artifacts `["company_profile",
  "contact_candidates"]`, blockers `["manager_loop_outreach_not_drafted",
  "manager_loop_review_failed"]`, and no `orchestrator_plan_summary`.
- Follow-up evidence: a 2026-05-25 14:44 EDT inspection of the real preflight
  payload showed `manual_request_plan.requested_agent=None` and
  `target_agent="opportunity_scout"` while the raw request still starts with
  `Plan the safest workflow...`. The manager-loop plan-summary check currently
  depends on `requested_agent == "orchestrator"`, so real planning-first asks can
  be treated as specialist-first unless the caller explicitly passed
  `requested_agent="orchestrator"`.
- Remaining expected fix: use the raw request/preflight route memo to identify
  planning-first workflow asks, not only `manual_request_plan.requested_agent`.
  Add a regression that builds the preflight with `run_orchestrator_preflight()`
  exactly as CLI/Slack do when no explicit named agent was provided.
- Re-fix: The preflight regression now uses the actual
  `run_orchestrator_preflight()` and `compact_orchestrator_preflight_payload()`
  handoff instead of a hand-built fixture, and the current implementation
  returns the Orchestrator plan artifact.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_planning_first_workflow_with_preflight_still_returns_orchestrator_plan`.
- Reopen evidence: a current behavior probe at 2026-05-25 14:47 EDT still fails
  with the same real compact-preflight path. The preflight manual plan has
  `requested_agent=None` and `target_agent="opportunity_scout"`, and the final
  manager-loop result returns route `business_research_analyst`, artifacts
  `["company_profile", "contact_candidates"]`, blockers
  `["manager_loop_outreach_not_drafted", "manager_loop_review_failed"]`, and no
  `orchestrator_plan_summary`.
- Re-fix: Manager-loop plan-summary detection now treats planning-first raw
  request language as Orchestrator-owned when no explicit specialist was
  requested. The regression now covers both explicit `requested_agent=orchestrator`
  and generic preflight handoffs where `manual_request_plan.requested_agent` is
  `None`.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_planning_first_workflow_with_preflight_still_returns_orchestrator_plan
  tests/test_workflow_runner.py::test_planning_first_workflow_with_generic_preflight_returns_orchestrator_plan`.
- Follow-up evidence: a 2026-05-25 14:49 EDT repro still returns the same
  failure shape for the real preflight path: `manual_requested_agent=null`,
  `manual_target_agent="opportunity_scout"`, route
  `business_research_analyst`, artifacts `["company_profile",
  "contact_candidates"]`, blockers `["manager_loop_outreach_not_drafted",
  "manager_loop_review_failed"]`, and no `orchestrator_plan_summary`.
- Re-fix verification: a 2026-05-25 18:05 EDT behavior probe using the same
  real `run_orchestrator_preflight()` plus
  `compact_orchestrator_preflight_payload()` path now returns route
  `orchestrator`, artifact `["orchestrator_plan_summary"]`, blocker
  `["manager_loop_outreach_not_drafted"]`, and a human summary headed
  `Orchestrator workflow plan`.

### P1 - Broad Gmail follow-up asks can still fall into unsupported clarification instead of Gmail gates

- Found: 2026-05-25 14:21 EDT
- Fixed: 2026-05-25 14:28 EDT
- Reopened: 2026-05-25 14:29 EDT
- Re-fixed: 2026-05-25 14:35 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: Some expanded agent-specific asks contain enough information to select
  the owning specialist and then block on the correct context/approval gate, but
  the current heuristic planner routes them to generic clarification. The
  WorkItem manager loop then returns `route_not_supported_in_workitem_phase`
  instead of a precise Gmail or Outreach blocker.
- Evidence: Dry manager-loop probes returned `target_agent="clarification"` and
  `route_not_supported_in_workitem_phase` for `Create a LinkedIn variant only
  and include the facts used plus source ids used.`, `Label selected messages as
  follow-up candidates.`, and `Audit why a previous @KNI response felt unrelated
  and tell me which agent path should have handled it.` A follow-up walkthrough
  at 2026-05-25 14:26 EDT found the same failure shape for `What should I follow
  up on this week from email? Draft replies only where needed.` The first should
  route to Outreach Composer and require approved external-use facts; the Gmail
  asks should route to Gmail Triage and require selected Gmail context or live
  Gmail scope plus write approval for labels/drafts; the wrong-response audit
  should route to Orchestrator or Chief of Staff diagnostics rather than an
  unsupported WorkItem route.
- Impact: The agents do not yet consistently have the information/tool pathway
  needed for precise completion of agent-specific natural-language tasks. Instead
  of surfacing the missing selected messages, approved facts, or diagnostic
  context, the system reports a generic unsupported route.
- Expected fix: Extend manual planning/routing for channel-specific outreach
  variants, Gmail follow-up triage, Gmail label/write-plan requests, and
  wrong-response diagnostic asks. Each should land on the owning specialist with
  read/draft-only or approval gates intact, and tests should assert the final
  blocker names the missing context or approval rather than
  `route_not_supported_in_workitem_phase`.
- Fix: Manual planning now routes LinkedIn/email/outreach variant creation to
  Outreach Composer, selected-message label/tag/mark requests to Gmail Triage,
  and wrong/unrelated prior `@KNI` response diagnostics to Chief of Staff. The
  WorkItem manager loop then reaches the owning gate: approved-context blocker
  for outreach variants, Gmail context blocker for label plans, and CoS
  diagnostics for response-path audits.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_agent_specific_variants_to_owning_agent
  tests/test_workflow_runner.py::test_agent_specific_variant_requests_reach_owning_workitem_gate`.
- Reopen evidence: the agent-specific examples are fixed, but a broader Gmail
  triage ask from the expanded matrix still fails the same way. A dry probe at
  2026-05-25 14:29 EDT for `What should I follow up on this week from email?
  Draft replies only where needed.` returned `target_agent="clarification"`,
  `route_result="clarification"`, final blocker
  `route_not_supported_in_workitem_phase`, and no Gmail context blocker.
- Remaining expected fix: broaden Gmail routing for email follow-up, weekly
  follow-up, and `what matters from email` phrasings so these land on Gmail
  Triage and block on selected Gmail context or explicit live Gmail retrieval
  scope.
- Re-fix: Manual planning now routes broad email/Gmail follow-up and draft-reply
  triage asks to Gmail Triage, so WorkItem execution reaches the Gmail context
  gate instead of the unsupported-route blocker.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_agent_specific_variants_to_owning_agent`.

### P1 - Source-bundle-only Business Research can fabricate a profile without supplied evidence

- Found: 2026-05-25 14:26 EDT
- Fixed: 2026-05-25 14:31 EDT
- Reopened: 2026-05-25 14:41 EDT
- Re-fixed: 2026-05-25 14:43 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`,
  Business Research fixture/source-bundle path,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: A Business Research request that explicitly says to use only a
  provided source bundle can still proceed in dry WorkItem execution even when
  no source bundle was provided. The run attaches a source-backed company
  profile for the whole instruction text instead of blocking on missing bundle
  evidence or returning an insufficient-evidence result.
- Evidence: A dry manager-loop probe for `Research an obscure behavioral health
  vendor from the provided source bundle only; say if there is not enough
  evidence.` routed to `business_research_analyst` with
  `primary_target="an obscure behavioral health vendor from the provided source
  bundle only; say if there is not enough evidence"`, returned artifacts
  `["company_profile", "contact_candidates"]`, no blockers, status
  `in_progress`, and summary `Business Research Analyst attached a
  source-backed company profile for an obscure behavioral health vendor from the
  provided source bundle only; say if there is not enough evidence.`
- Impact: Before live runs, the implementation can appear to satisfy a
  strict evidence-bound research ask while the agent never received the required
  source bundle. This risks false confidence in source sufficiency, artifact
  naming, and downstream outreach approval checks that depend on approved
  source-backed facts.
- Expected fix: Detect `provided source bundle only`, `using these sources
  only`, and source-id-only research constraints as an evidence gate. If no
  source bundle/context pack/source ids are attached, return a precise
  `source_bundle_required` or `insufficient_source_evidence` blocker instead of
  constructing a fixture profile. Preserve the clean company/topic target
  separately from the source-use constraint.
- Fix: Business Research WorkItem advancement now detects source-bundle-only
  constraints and blocks with `source_bundle_required` before fixture profile
  construction when no source bundle, source ids, context file, external
  context, Slack context, or WorkItem sources are attached. Target extraction
  strips source-bundle-only clauses from company/topic names.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_source_bundle_only_research_blocks_without_attached_sources`.
- Reopen evidence: a matrix dry probe at 2026-05-25 14:41 EDT for `Do not use
  live search; summarize only the attached source bundle and cite every factual
  claim to a source id.` still routed to Business Research and produced
  `["company_profile", "contact_candidates"]` with blocker
  `manager_loop_review_failed`, instead of stopping at `source_bundle_required`.
- Remaining expected fix: treat generic `attached source bundle`, `attached
  sources`, and source-id-only instructions as source-bundle constraints even
  when the prompt does not say `provided` or name a specific company. If no
  source bundle/context/source ids are attached, block before fixture profile
  construction.
- Re-fix: Source-bundle gating now recognizes `attached source bundle`,
  `attached sources`, `these sources only`, and `cite every/all claim to a
  source id` phrasing. The expanded matrix prompt blocks with
  `source_bundle_required` before any fixture profile or contact artifact is
  attached.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_source_bundle_only_research_blocks_without_attached_sources
  tests/test_workflow_runner.py::test_attached_source_bundle_research_blocks_without_attached_sources`.

### P1 - Opportunity Scout partnership asks without explicit opportunity wording can route to clarification

- Found: 2026-05-25 14:26 EDT
- Fixed: 2026-05-25 14:31 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/orchestrator/routing.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: Expanded Opportunity Scout scenarios include broad partnerships,
  buyer-intent, pilots, funding, and other opportunity-like signals. A hard
  filtered partnership discovery prompt can still route to generic
  clarification when it does not say `opportunity`, `role`, or `job`.
- Evidence: A dry manager-loop probe for `Find 3 remote US behavioral health AI
  partnerships from the last 30 days, exclude staffing agencies, and keep hard
  filters in scoring.` returned `target_agent="clarification"`,
  `route_result="clarification"`, no workflow, and final blocker
  `route_not_supported_in_workitem_phase`. The agent matrix expects Opportunity
  Scout to own partnership-signal discovery and preserve count, recency,
  geography/work-mode, exclusion, and scoring constraints.
- Impact: Natural-language opportunity discovery can fail before Scout sees the
  request, so the agent never gets the required filters or the chance to use
  search/opportunity-source tools. This is distinct from older strict role
  filter fixes because the trigger is partnership/opportunity-signal phrasing,
  not role-search phrasing.
- Expected fix: Broaden Opportunity Scout routing and manual planning for
  `find/search/source/list` plus opportunity-signal nouns such as
  partnerships, pilots, buyer-intent signals, funding signals, grants,
  conferences, publications, procurement, or validation activity. Add a
  regression proving hard filters and recency constraints reach Scout metadata
  rather than becoming clarification.
- Fix: Opportunity routing now treats partnerships, pilots, buyer-intent,
  funding signals, and validation activity as opportunity-signal nouns. Manual
  planning preserves desired count plus remote, U.S., recency, and exclusion
  constraints for partnership discovery instead of falling to clarification.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_partnership_signal_discovery_to_opportunity_scout`.

### P1 - Partnership-signal Scout asks can still present generic weak matches after routing succeeds

- Found: 2026-05-25 14:29 EDT
- Fixed: 2026-05-25 14:35 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/agents/opportunity_scout.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: The partnership-signal routing fix gets hard-filtered partnership
  discovery to Opportunity Scout, but the dry WorkItem path can still attach
  generic opportunity artifacts that do not demonstrate the requested
  partnership, recency, geography/work-mode, or exclusion constraints.
- Evidence: A dry manager-loop probe for `Find 3 remote US behavioral health AI
  partnerships from the last 30 days, exclude staffing agencies, and keep hard
  filters in scoring.` now returns manual target
  `remote US behavioral health AI partnerships from the last 30 days, exclude
  staffing agencies, and keep hard filters in scoring`, route
  `opportunity_scout`, and workflow `["opportunity_scout"]`. The final WorkItem
  still returned two `opportunity` artifacts plus blocker
  `manager_loop_review_failed`, with summary `Opportunity Scout attached 2
  source-backed opportunity record(s).`
- Impact: Live-readiness checks can mistake the route fix for end-to-end task
  readiness. In Slack, the operator may see attached opportunity records even
  though the manager review already found the results too weak or off-filter.
- Expected fix: for hard-filtered partnership/buyer-intent/pilot/funding-signal
  searches, return a `no_strong_matches` or `weak_adjacent_matches` artifact and
  blocker when fixtures or live results cannot prove the constraints. Avoid
  presenting generic fixture opportunities as source-backed matches after a
  failed manager review.
- Fix: Dry Opportunity Scout now detects hard-filtered partnership-signal
  searches and returns `weak_adjacent_matches` without attaching generic fixture
  opportunities when no fixture proves partnership, recency, remote/U.S., and
  exclusion constraints.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_hard_filtered_partnership_search_blocks_weak_fixture_matches`.

### P1 - Agent-attached `search_web` does not guarantee hosted Agents SDK web search

- Found: 2026-05-25 14:18 EDT
- Fixed: 2026-05-25 14:25 EDT
- Status: fixed
- Area: `src/keystone_agents/tools/serper_tool.py`,
  `src/keystone_agents/tools/search_provider.py`,
  `src/keystone_agents/live_retrieval.py`, agent builders and registry
- Issue: The agent registry and tool policy declare `search_web` for
  Orchestrator, Chief of Staff, Gmail Triage, Business Research Analyst,
  Opportunity Scout, and Outreach Composer, but the actual tool attached to the
  SDK agents is the local function-tool wrapper from `serper_tool.py`. In live
  SDK tool calls with no explicit `SEARCH_PROVIDER`, that wrapper resolves to
  SearXNG only. The hosted OpenAI Agents SDK `WebSearchTool` path exists in
  `AgentsWebSearchProvider` and deterministic live-retrieval fanout, but it is
  not guaranteed for every direct agent `search_web` tool call.
- Evidence: Agent builders import and attach
  `keystone_agents.tools.serper_tool.search_web`. `_sdk_live_search_provider_name()`
  returns `SearchProviderName.SEARXNG` when `KEYSTONE_LIVE_MODE` and live
  research are enabled with no `SEARCH_PROVIDER` override.
  `build_search_provider(..., provider=SEARXNG, live=True)` returns only
  `SearxngSearchProvider`. The native hosted `WebSearchTool` is created only
  inside `AgentsWebSearchProvider._run_live()`, which requires explicit
  `SEARCH_PROVIDER=agents-web-search` or the separate deterministic retrieval
  fanout path in `live_retrieval.py`.
- Impact: Diverse natural-language tasks that depend on an agent choosing web
  search can appear to satisfy the registry/tool-policy requirement while not
  actually giving that agent the hosted Agents SDK web-search lane the operator
  expects. Search behavior, source recall, provider diagnostics, and rate-limit
  behavior can differ between deterministic retrieval and live SDK agent tool
  calls.
- Expected fix: Decide whether `search_web` is the canonical tool name for a
  shared retrieval policy or whether agents should receive the native hosted
  `WebSearchTool` directly. If `search_web` remains canonical, make it use the
  same provider sequence/fanout policy as live retrieval, including capped
  `agents-web-search` when configured by default. Add tests that build each
  agent, inspect the attached tool names, and verify direct live-tool provider
  selection includes or explicitly excludes the hosted Agents SDK lane with a
  documented reason.
- Fix: `search_web` remains the canonical SDK tool wrapper, but default live
  direct agent tool calls now use a `HybridSearchProvider` with SearXNG plus the
  capped hosted `agents-web-search` lane when live research is enabled and no
  explicit non-SearXNG provider is selected. Explicit `SEARCH_PROVIDER` choices
  such as `serper`, `firecrawl`, `tavily`, or `agents-web-search` still select
  their narrow provider directly.
- Verification: `.venv/bin/python -m pytest tests/test_serper_tool.py`.

### P1 - Chief of Staff operational asks still route to clarification or research

- Found: 2026-05-25 14:18 EDT
- Fixed: 2026-05-25 14:21 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/agents/orchestrator.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: Expanded diverse Chief of Staff scenarios for architecture review,
  Slack-thread follow-up planning, and automation audits do not consistently
  route to Chief of Staff. They either block as generic clarification or route
  to Business Research, so the agent that owns runtime state, Slack context,
  automation inspection, and WorkItem operations may never receive the request.
- Evidence: Dry route probes over the expanded matrix returned
  `target_agent="clarification"` / `route="clarification"` for
  `Review the business-agent architecture changes and recommend the next three
  implementation steps.` and for `Audit enabled automations and identify stale,
  duplicate, or unsafe schedules without changing them.` The selected Slack
  thread planning ask `Summarize the selected Slack thread, identify unresolved
  operator requests, and propose an internal follow-up plan.` planned and routed
  to `business_research_analyst`.
- Pre-fix follow-up evidence at 2026-05-25 14:21 EDT: the same architecture and
  automation audit probes still routed to clarification and the selected Slack
  thread planning probe still routed to Business Research. A related
  wrong-response diagnostic probe `Audit why a previous @KNI response felt
  unrelated and tell me which agent path should have handled it.` still returns
  a generic unsupported-route blocker and is tracked separately under the
  agent-specific routing finding above.
- Impact: Broad operational Slack requests can miss the Chief of Staff tools
  needed to precisely answer them, including Slack runtime config, automation
  specs/runs, active WorkItems, and repo-local operations context. Before live
  testing this means the request can fail early or produce a source-research
  style answer instead of an operational plan.
- Expected fix: Broaden Chief of Staff routing for architecture, implementation,
  Slack-thread operations, automation audit, WorkItem/blocker review, bridge
  diagnostics, and internal planning phrasing. Keep explicit specialist requests
  authoritative, and add regressions from the expanded matrix proving these
  diverse CoS asks reach `chief_of_staff` with no external writes.
- Fix: Manual planning and direct Orchestrator routing now recognize internal
  operational planning phrasing for architecture reviews, implementation-step
  asks, Slack-thread follow-up planning, automation audits, and WorkItem/bridge
  diagnostics. These route to
  `chief_of_staff` while keeping explicit specialist mentions authoritative and
  preserving no-send behavior.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_operational_planning_to_chief_of_staff
  tests/test_orchestrator.py::test_orchestrator_routes_operational_planning_to_chief_of_staff`.

### P1 - CRM write boundary is present in preflight but lost in WorkItem final output

- Found: 2026-05-25 14:18 EDT
- Fixed: 2026-05-25 14:23 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/agents/orchestrator.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` `OS-4` and `OR-3`
- Issue: Direct Orchestrator routing now recognizes CRM-save requests and adds
  `save_to_crm`, `crm_write`, `crm_preflight`, and draft-only CRM-ready fields.
  The saved WorkItem manager-loop path does not preserve that CRM write boundary
  in final blockers or summaries when the specialist produces no opportunities
  or stops before the CRM-ready stage.
- Evidence: `route_request("Find active roles and save the top 3 to my CRM.")`
  returns workflow `["opportunity_scout", "crm_preflight"]`,
  `forbidden_actions=["send_email", "save_to_crm", "crm_write"]`, and CRM-ready
  placeholder fields. A dry `OS-4` manager-loop run for the full test-pack prompt
  returns `status="blocked"` with only `no_opportunities_found`, no CRM write
  blocker, no CRM-ready table/placeholder artifact, and summary
  `Opportunity Scout did not produce a source-backed opportunity record.`
  A dry `OR-3` boundary workflow adds `manager_loop_send_blocked` and
  `manager_loop_outreach_not_drafted`, but does not include a CRM write/save
  blocker even though the request says `save it to CRM`.
- Impact: Slack/CLI final WorkItem output can omit the exact no-write assurance
  and approval gate the user asked to see, especially in no-result or partial
  multi-agent paths. This weakens side-effect boundary transmission across the
  Orchestrator-to-specialist-to-bridge flow.
- Expected fix: Carry CRM write intent from preflight/manual-plan metadata into
  WorkItem target metadata and manager-loop missing-stage blockers. Final
  WorkItem output should explicitly state no CRM write occurred and, when
  possible, attach draft-only CRM-ready fields or a blocker explaining why no
  CRM-ready preview could be produced. Add regressions for `OS-4` and `OR-3`
  manager-loop final results, not only direct `route_request()`.
- Fix: Manager-loop finalization now detects CRM/Airtable/Salesforce/HubSpot
  write intent and adds a `manager_loop_crm_write_blocked` blocker stating that
  no CRM write was performed and only draft-only CRM-ready fields may be
  prepared after source-backed context and explicit approval. The blocker is
  included in final WorkItem blockers and `manager_loop_completed`
  missing-stage metadata for no-result and partial multi-agent paths.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_manager_loop_stops_before_repeating_specialist_for_orchestrator_workflow
  tests/test_workflow_runner.py::test_manager_loop_blocks_crm_write_boundary_on_opportunity_request`.

### P2 - Short strict-format Business Research asks still pollute the target

- Found: 2026-05-25 14:18 EDT
- Fixed: 2026-05-25 14:22 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: The original `BR-5` full prompt target extraction fix handles the
  executable test-pack wording, but shorter natural-language strict-format asks
  can still keep output-format text in the research target.
- Evidence: Dry route probing the expanded matrix ask `Research Lindus Health.
  Return exactly: Summary, Evidence, Keystone relevance, Concerns, Suggested
  next step.` returned `target_agent="business_research_analyst"` but
  `primary_target="Lindus Health. Return"`. The route itself is correct, but the
  target key is polluted by the format instruction.
- Impact: Live research, contact enrichment, artifact keys, and follow-up
  WorkItem reuse can drift when common concise requests are used instead of the
  exact executable `BR-5` text. This is especially likely in Slack where users
  naturally shorten strict-format requests.
- Expected fix: Generalize strict-format stripping around `Return exactly:`,
  `Return as:`, `Use sections:`, and similar formatter clauses regardless of
  whether the request includes the full `for Keystone business development`
  sentence. Preserve the requested section list separately as renderer or
  response-plan metadata.
- Fix: Business Research target extraction now strips short output-format tails
  before company-name extraction for `Return exactly:`, `Return as:`,
  `Use sections:`, `Include sections:`, `Format as:`, and `Sections:`
  phrasings, preventing concise Slack requests from turning formatter words
  into entity targets.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_strips_short_business_research_format_tail`.

### P2 - Advisory manager-review failures are streamed to Slack as plain failures

- Found: 2026-05-25 14:12 EDT
- Fixed: 2026-05-25 14:16 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  sibling `keystone-slack/kni_integrations/slack_socket_mode.py`
- Issue: The manager loop now correctly treats generic review gaps on
  source-backed fixture artifacts as advisory instead of blocking, but the
  realtime feedback payload still streams only `review_status="fail"` and
  `overall_score`. The Slack running modal has no `blocking=false`,
  `advisory=true`, or final blocker context, so it renders advisory review gaps
  as a plain manager-review failure while the final WorkItem can still be
  `in_progress` with usable artifacts and no blockers.
- Evidence: A dry feedback probe for `Research Lindus Health.` returned final
  `status="in_progress"` with no blockers and artifacts
  `company_profile` / `contact_candidates`, but streamed
  `{"event_type": "manager_loop_review", "payload": {"route":
  "business_research_analyst", "status": "in_progress", "review_status":
  "fail", "overall_score": 44}}`. The sibling Slack bridge renders that event
  as `Manager review for business_research_analyst: fail (44/100).` before the
  final result.
- Impact: Operators can see an apparent manager failure in the Slack modal even
  when the run continues normally and produces acceptable dry-run artifacts.
  That weakens the transmission of orchestration state and can make fixed
  calibration behavior look broken during pre-live validation.
- Expected fix: Add an explicit review severity/decision field to feedback
  events, such as `blocking`, `advisory`, or `decision=warn|block|pass`, using
  the same `_manager_review_should_block` decision. Update Slack modal wording
  to say `review warning` or `advisory gaps` for non-blocking failures, and add
  a bridge regression for a source-backed fixture run that streams a non-blocking
  review warning before final success/progress.
- Fix: KBA manager-loop review events now include `review_decision`,
  `blocking`, and `advisory` fields derived from the same authoritative
  Orchestrator review blocking decision. The CLI realtime renderer labels
  non-blocking failed reviews as `Manager review warning` and blocking reviews
  as `Manager review block`, so downstream Slack bridge consumers can
  distinguish advisory gaps from hard stops without changing the final
  WorkItem schema.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_manager_loop_records_orchestrator_review_feedback_for_agent_run
  tests/test_slack_agent_actions.py::test_realtime_feedback_callback_failure_is_nonfatal
  tests/test_slack_agent_actions.py::test_realtime_feedback_events_do_not_authorize_side_effects
  tests/test_slack_agent_actions.py::test_slack_agent_action_cli_streams_feedback_jsonl`.
  Dry CLI probe `.venv/bin/python -m keystone_agents.cli ask @KNI business
  research analyst "Research Lindus Health." --max-manager-steps 1` printed
  `Manager review warning` while returning final WorkItem status `in_progress`
  with usable artifacts.

### P2 - CLI WorkItem advance bypasses Orchestrator preflight and manager-loop review

- Found: 2026-05-25 14:19 EDT
- Fixed: 2026-05-25 14:19 EDT
- Status: fixed
- Area: `src/keystone_agents/cli.py`, `tests/test_cli.py`
- Issue: `keystone work-items advance --input "<natural language>"` still
  constructed a `WorkflowRunRequest` directly and called single-step
  `advance_work_item()` when LangGraph was disabled. That path skipped
  Orchestrator preflight, omitted compact planner/memory handoff context, did
  not persist the parent `ManualRequestPlan`, and did not run the manager-loop
  Orchestrator review/feedback events used by the canonical `ask` and Slack
  modal paths.
- Impact: A natural-language WorkItem start could follow a different
  architecture than the Slack/@KNI Orchestrator-first path, weakening the
  consolidated planner/orchestrator objective and making specialist evaluation
  inconsistent across CLI entrypoints.
- Fix: `work-items advance` now runs Orchestrator preflight for substantive
  request text, blocks before WorkItem creation when preflight refuses
  execution, passes compact `orchestrator_preflight` and `manual_request_plan`
  into `WorkflowRunRequest`, and uses `advance_work_item_manager_loop()` by
  default with realtime manager feedback for non-JSON runs. A
  `--max-manager-steps` flag mirrors the canonical `ask` cap.
- Verification: `.venv/bin/python -m pytest
  tests/test_cli.py::test_cli_work_items_advance_uses_orchestrator_preflight_and_manager_loop`.

### P1 - Review-failure blocker now blocks ordinary dry-run research and scout asks

- Found: 2026-05-25 14:09 EDT
- Fixed: 2026-05-25 14:12 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/agents/orchestrator.py`
- Issue: The manager-loop review fix now correctly prevents failed reviews from
  remaining silent `in_progress` states, but the deterministic review payload is
  still too thin for normal dry-run fixture outputs. As a result, common
  successful research and opportunity fixture runs are blocked with
  `manager_loop_review_failed` even after attaching usable artifacts.
- Evidence: Dry manager-loop probes show `Research Lindus Health.` and
  `Prepare a concise research brief on Lindus Health for possible partnership
  relevance.` both attach `company_profile` and `contact_candidates`, but return
  `status="blocked"` with blocker `manager_loop_review_failed`. The review
  event reports `business_research_analyst`, `review_status="fail"`, and
  `overall_score=44` with gaps such as `Include facts, sources, unknowns, and
  decision context.` A dry `Find behavioral health AI opportunities.` run
  attaches opportunity artifacts, but also returns `status="blocked"` with
  `manager_loop_review_failed` after an Opportunity Scout review score of
  `44/100`.
- Impact: The architecture now fails safe, but ordinary dry-run and Slack
  selected-message requests can look blocked even when deterministic fixtures
  produced the intended artifacts. This makes pre-live validation noisy,
  weakens operator trust, and can hide real blockers behind review calibration
  failures.
- Expected fix: Calibrate manager-loop review input or thresholds before making
  review failure authoritative for normal fixture outputs. Options include
  passing richer specialist artifact summaries into `review_specialist_output`,
  marking deterministic fixture outputs as advisory/partial when source-backed
  artifacts exist, or only converting failed reviews into blockers when the
  observed gaps are safety/relevance critical. Add regressions proving simple
  Business Research and Opportunity Scout dry runs with attached artifacts do
  not block solely because the review payload omitted fields already present in
  the artifacts.
- Fix: `manager_loop_review_failed` is now reserved for authoritative failures:
  failed approval boundaries, empty outputs, or critical relevance/safety gaps
  such as unrelated/wrong-response/unsupported-claim markers. Generic review
  gaps on source-backed fixture artifacts remain visible as Orchestrator review
  feedback without blocking otherwise usable dry-run artifacts.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_manager_loop_generic_review_gaps_do_not_block_fixture_artifacts
  tests/test_workflow_runner.py::test_manager_loop_failed_review_blocks_otherwise_active_single_step
  tests/test_slack_agent_actions.py::test_realtime_feedback_callback_failure_is_nonfatal
  tests/test_slack_agent_actions.py::test_thread_fetch_failure_warns_but_run_proceeds`.

### P1 - OR-4 ambiguous workflow returns last specialist output instead of a plan

- Found: 2026-05-25 14:05 EDT
- Fixed: 2026-05-25 14:10 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/response_synthesis.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` `OR-4`
- Issue: The `OR-4` ambiguous-goal prompt asks the Orchestrator to make
  assumptions, select a workflow, name the agents used or proposed, summarize
  top findings, recommend the next action, and say what input would improve the
  next run. The current dry WorkItem manager loop runs Opportunity Scout, then
  Business Research, then stops on the repeat-specialist guard while returning
  only the final Business Research summary.
- Evidence: A dry `OR-4` manager-loop probe returns `status="in_progress"`,
  `route="business_research_analyst"`, no blockers, and human summary
  `Business Research Analyst attached a source-backed company profile for
  NeuroFlow...`. The WorkItem events show Opportunity Scout attached three
  opportunities, Business Research attached `company_profile` and
  `contact_candidates`, and `manager_loop_completed` stopped before repeating
  Opportunity Scout. There is no final artifact or human-facing summary for
  `assumptions made`, `selected workflow`, `which agents were used or would be
  used`, `top findings`, or `what additional input would improve the next run`.
- Impact: Broad Slack-style asks such as `Help me find business development
  opportunities` can look like an arbitrary company-profile run instead of an
  Orchestrator-managed planning response. This weakens natural-language request
  handling for one of the main post-architecture goals: broad requests should
  be decomposed and explained, not collapse into the last specialist artifact.
- Expected fix: Add a final deterministic or synthesized Orchestrator summary
  for ambiguous multi-agent planning runs that aggregates manager-loop steps,
  states assumptions and selected workflow, and lists gaps/next input. If the
  run cannot meet those requested fields, attach an explicit blocker such as
  `manager_loop_plan_summary_missing` instead of returning the last specialist
  summary as the main response. Add a regression for `OR-4` that checks the
  final WorkItem response includes the requested workflow-plan fields or an
  explicit blocker.
- Fix: broad Orchestrator planning runs now get a final
  `orchestrator_plan_summary` artifact and human-facing summary that aggregates
  manager-loop steps, assumptions, selected workflow, agents used, top findings,
  recommended next action, and useful next input. This prevents the final Slack
  or CLI answer from collapsing into the last specialist artifact.
- Verification: dry `OR-4` probe at 2026-05-25 14:10 EDT returned
  `route="orchestrator"`, `status="done"`, no blockers, artifact
  `orchestrator_plan_summary`, and summary sections for assumptions, selected
  workflow, agents used/proposed, top findings, recommended next action, and
  additional input. Focused coverage is in
  `tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop`.

### P1 - Manager-loop review failures do not change active WorkItem state

- Found: 2026-05-25 14:00 EDT
- Fixed: 2026-05-25 14:05 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/agents/orchestrator.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Issue: The WorkItem manager loop records deterministic Orchestrator review
  failures as audit/event metadata, but a failed review does not currently
  change the returned WorkItem status, add a blocker, or request repair for
  otherwise active single-specialist runs.
- Evidence: A dry sweep over `list_test_pack_specs()` with placeholders
  replaced showed `BR-1` through `BR-5`, `OS-1`, `OS-3`, and `OR-4` returning
  `status="in_progress"` with no blockers while every corresponding
  `manager_loop_review` event reported `review_status="fail"` and
  `overall_score=44`. Blocked Gmail, Outreach, no-result Opportunity, and
  connected Orchestrator cases also report review failures, but those already
  have explicit blockers; the unsafe ambiguity is the active/no-blocker path.
- Fix evidence: manager-loop review failures now add a
  `manager_loop_review_failed` blocker for active results that would otherwise
  look usable. A follow-up dry sweep over `list_test_pack_specs()` found no case
  where failed `manager_loop_review` events remained `status="in_progress"` with
  no blockers.
- Impact: Slack and CLI surfaces can present a WorkItem as active and apparently
  usable while the Orchestrator evaluator says the step failed. This weakens
  planner/orchestrator supervision, makes dry-run acceptance hard to interpret
  before live testing, and can hide relevance/structure problems behind normal
  `in_progress` lifecycle wording.
- Expected fix: Decide whether deterministic manager-loop review is
  authoritative or advisory. If authoritative, convert failed reviews into a
  bounded blocker such as `manager_loop_review_failed` or a repair-required
  status before final rendering. If advisory, recalibrate the review payload and
  thresholds so expected fixture outputs do not all fail at `44/100`, and make
  final synthesis/rendering explicitly label advisory review gaps. Add
  regressions covering at least one Business Research, Opportunity Scout, and
  Orchestrator test-pack run where review failure cannot remain a silent
  `in_progress` state.
- Verification: dry sweep at 2026-05-25 14:05 EDT reported
  `active_failed_no_blocker []`.
- Fix: Manager-loop review failures now add `manager_loop_review_failed`
  blockers for otherwise active single-step outputs and during final loop
  completion when the latest specialist review still failed. Explicit multi-step
  workflows may proceed to the next requested specialist before the final failed
  review becomes authoritative.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_manager_loop_failed_review_blocks_otherwise_active_single_step
  tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop`.

### P2 - CLI dry-run Chief of Staff bypasses parent Orchestrator manual plan

- Found: 2026-05-25 14:03 EDT
- Fixed: 2026-05-25 14:03 EDT
- Status: fixed
- Area: `src/keystone_agents/cli.py`, `tests/test_cli.py`
- Issue: The `ask --agent chief_of_staff` dry-run path runs Orchestrator
  preflight, but then calls `plan_chief_of_staff_request()` without the parent
  `ManualRequestPlan`.
- Evidence: Code inspection showed `_print_ask_dry_run()` passed only
  `input_text` and `database_url` into the deterministic Chief of Staff planner,
  unlike WorkItem and live child paths that preserve the parent plan.
- Impact: Explicit Chief of Staff dry runs could still fall back to local
  deterministic shortcut interpretation instead of consuming the
  Orchestrator/planner interpretation first, weakening the fix that prevents
  generic `2026`, finance, or prior-context language from hijacking unrelated
  architecture or Slack-history asks.
- Fix: `_print_ask_dry_run()` now passes `manual_request_plan=manual_plan` into
  `plan_chief_of_staff_request()`.
- Verification: `.venv/bin/python -m pytest
  tests/test_cli.py::test_cli_ask_chief_of_staff_dry_run_uses_orchestrator_manual_plan`.

### P2 - Opportunity Scout next-step output wording remains in search target

- Found: 2026-05-25 13:57 EDT
- Fixed: 2026-05-25 13:58 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`
- Issue: The broader `next step` continuation bug is fixed, but Opportunity
  Scout target extraction still keeps next-step output wording inside the search
  topic.
- Evidence: A dry probe of `Find behavioral health AI opportunities and
  recommend the next step.` now correctly returns
  `target_agent="opportunity_scout"` and `intent="opportunity_search"`, but
  `primary_target == "behavioral health AI opportunities and recommend the next
  step"` and the WorkItem target is the same polluted search string.
- Impact: This is less severe than the prior `continue_work_item` regression,
  but live retrieval can still spend query budget on output-format language
  rather than the opportunity domain. It can also produce artifact keys that mix
  the search topic with response instructions.
- Expected fix: Strip recommendation/output-format tails such as `and recommend
  the next step`, `and include next steps`, or `with a next-step recommendation`
  from Opportunity Scout search targets while preserving them in renderer or
  response-plan metadata.
- Fix: Opportunity Scout target extraction now strips next-step output-format
  tails from the search topic before WorkItem creation.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_keeps_next_step_output_language_on_specialist_routes
  tests/test_workflow_runner.py::test_opportunity_next_step_output_wording_not_in_workitem_target`.

### P1 - Ordinary next-step wording is misclassified as WorkItem continuation

- Found: 2026-05-25 13:51 EDT
- Fixed: 2026-05-25 13:56 EDT
- Status: fixed
- Area: `src/keystone_agents/orchestrator/routing.py`,
  `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`
- Issue: `RESUME_RE` includes `next step` and is applied before normal route
  detection in the manual planner. Fresh requests that ask an agent to
  recommend or list a next step are therefore classified as
  `target_agent="orchestrator"` and `intent="continue_work_item"` even when
  they are new Business Research, Opportunity Scout, Outreach Composer, or
  Gmail Triage tasks.
- Evidence: Dry probes with `infer_manual_request_plan(...,
  requested_agent="orchestrator")` returned `orchestrator/continue_work_item`
  for `Research Lindus Health and recommend the next step.`, `Find behavioral
  health AI opportunities and recommend the next step.`, `Draft outreach for
  Lindus Health and include the next step, do not send.`, and `Summarize this
  email thread and list the next step.` `route_request()` can still recover the
  specialist route, but the WorkItem path applies the polluted manual
  `primary_target`, for example `Lindus Health and recommend the next step`.
- Impact: Common operator phrasing can create WorkItems with stale/incorrect
  continuation intent and contaminated targets, weakening retrieval queries,
  artifact keys, contact enrichment, and follow-up commands before the LLM
  specialist has a chance to interpret the request naturally.
- Expected fix: Restrict continuation detection to standalone or context-bound
  phrases such as `continue`, `resume`, `pick up this WorkItem`, `what's next
  for this`, or explicit WorkItem/thread references. Treat `next step` inside
  requested output sections, recommendations, or summaries as artifact-format
  language, not a route. Add regressions for fresh research, opportunity,
  outreach, and Gmail asks containing `next step`.
- Fix: Fresh asks containing `next step` now stay with the appropriate
  specialist route instead of becoming `continue_work_item`.
- Verification: dry probes now return `business_research_analyst/company_research`
  targeting `Lindus Health` for `Research Lindus Health and recommend the next
  step.`, `opportunity_scout/opportunity_search` for `Find behavioral health AI
  opportunities and recommend the next step.`, `outreach_composer/outreach_draft`
  for `Draft outreach for Lindus Health and include the next step, do not
  send.`, and `gmail_triage/gmail_triage` for `Summarize this email thread and
  list the next step.` The corresponding WorkItem targets no longer use
  `continue_work_item` intent or a fake `Lindus Health and recommend the next
  step` company label.

### P1 - Gmail test-pack route plans include false Outreach Composer handoffs again

- Found: 2026-05-25 13:54 EDT
- Fixed: 2026-05-25 13:58 EDT
- Status: fixed
- Area: `src/keystone_agents/agents/orchestrator.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` `GT-1` to `GT-4`
- Issue: The earlier Gmail-only false-blocker fix still prevents
  manager-loop missing-stage blockers, but the Orchestrator route plan has
  regressed for Gmail test-pack prompts. `route_request(...,
  use_manual_plan=True)` now reports workflows `["gmail_triage",
  "outreach_composer"]` for `GT-1` through `GT-4`, even though these are Gmail
  triage/thread/reply tasks that should stay in Gmail Triage until selected
  Gmail context exists.
- Evidence: A full dry sweep over `list_test_pack_specs()` showed `GT-1`,
  `GT-2`, `GT-3`, and `GT-4` with manual plan
  `target_agent="gmail_triage"` / `intent="gmail_triage"`, WorkItem blocker
  `gmail_context_required`, and `missing_required_stages=[]`, but route
  workflows containing `outreach_composer`. `GT-5` correctly stayed
  `["gmail_triage"]`.
- Impact: Slack route previews, preflight feedback, and operator-facing
  Orchestrator metadata can still claim an Outreach Composer handoff is part of
  ordinary Gmail work. Even without missing-stage blockers, this weakens
  natural-language routing clarity and can confuse follow-up commands or
  approval expectations.
- Expected fix: Keep Gmail reply/draft wording inside the Gmail route plan
  unless the request explicitly asks for non-Gmail outreach, LinkedIn/external
  copy, company research, opportunity records, CRM/pipeline steps, or external
  drafting. Add a route-level regression for `GT-1` to `GT-4`, not only
  manager-loop missing-stage blocker coverage.
- Fix: Gmail test-pack prompts now keep the route-level workflow to
  `["gmail_triage"]` unless a true cross-agent Gmail workflow is requested.
- Verification: dry route probes for `GT-1` through `GT-5` now all return
  `route="gmail_triage"` and `workflow=["gmail_triage"]`. Focused route
  coverage in `tests/test_orchestrator.py::test_gmail_test_pack_prompts_route_to_gmail_triage`
  now asserts the workflow list, and manager-loop coverage remains in
  `tests/test_workflow_runner.py::test_gmail_only_workitems_do_not_get_false_downstream_stage_blockers`.

### P1 - OS-5 no-result fixture pads narrow role search with generic opportunities

- Found: 2026-05-25 13:52 EDT
- Fixed: 2026-05-25 13:58 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/agents/opportunity_scout.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` `OS-5`
- Issue: After the OS-5 target extraction fix, the WorkItem route correctly
  starts Opportunity Scout for the narrow 48-hour part-time/fractional CMO role
  request, but fixture execution still attaches generic opportunity records
  instead of exercising no-result or adjacent-match behavior.
- Evidence: A dry `advance_work_item_manager_loop()` run for `OS-5` now targets
  `active part-time remote U.S. chief medical officer or fractional medical
  director roles in behavioral health AI posted in the last 48 hours. Use strict
  criteria`, but returns opportunity artifacts `NeuroFlow` and `SAM.gov
  Behavioral Health AI Evaluation RFP`. The artifact metadata does not show
  evidence for posting within 48 hours, part-time/fractional status, remote U.S.
  role fit, or why these are only adjacent matches. The manager-loop review
  recorded `fail (44/100)` but the WorkItem remained `in_progress` with no
  blocker.
- Impact: The no-result acceptance target can look superficially successful in
  Slack or CLI dry runs while violating the hard filters. This makes live
  testing harder to interpret because weak fixture behavior is already teaching
  the workflow to pad narrow searches.
- Expected fix: Add a deterministic no-result/adjacent-match fixture path for
  strict role-recency prompts, or make the Opportunity Scout fixture return a
  structured `no_strong_matches` artifact/blocker when hard filters cannot be
  satisfied. The manager loop should not leave a failed review as plain
  `in_progress` without a weak-match/no-result blocker. Add a regression for
  `OS-5` proving no generic fixture opportunities are presented as strong
  matches.
- Fix: strict role-recency fixture execution now suppresses generic Opportunity
  Scout fixture matches and blocks with `no_strong_opportunity_matches` when no
  fixture record proves the role, recency, remote/location, and
  part-time/fractional constraints.
- Verification: dry `OS-5` manager-loop probe now returns status `blocked`, no
  `opportunity` artifacts, blocker `no_strong_opportunity_matches`, and target
  text containing `chief medical officer` rather than the quoted adjacent-match
  label. Focused coverage is present in
  `tests/test_workflow_runner.py::test_opportunity_no_result_prompt_keeps_search_target_not_quoted_output_label`.

### P1 - OS-5 quoted output label becomes the Opportunity Scout search topic

- Found: 2026-05-25 13:47 EDT
- Fixed: 2026-05-25 13:51 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` `OS-5`
- Issue: The manual request planner checks `_quoted_text()` before the
  opportunity-search target for Opportunity Scout requests. `OS-5` contains a
  quoted output-section label, `"Adjacent but not exact matches,"`, after the
  real search request. The planner therefore sets `primary_target` to that
  label instead of the actual query for part-time remote U.S. behavioral health
  AI CMO/fractional medical director roles posted in the last 48 hours.
- Evidence: A dry probe of `get_test_pack_spec("OS-5").natural_prompt` through
  `infer_manual_request_plan(..., requested_agent="orchestrator")` returned
  `plan.primary_target == "Adjacent but not exact matches,"`. Running the same
  prompt through `advance_work_item_manager_loop()` produced route
  `opportunity_scout`, but the WorkItem target was also
  `Adjacent but not exact matches,` and the run blocked with
  `no_opportunities_found`.
- Impact: Before any live testing, the current implementation can send a valid
  exact Opportunity Scout request down the right specialist lane while searching
  for the wrong natural-language target. Live runs would waste retrieval budget,
  miss the intended hard filters and recency constraints, and report a
  misleading no-result outcome.
- Expected fix: For Opportunity Scout target extraction, prefer the cleaned
  opportunity request over quoted phrases that appear in formatting or output
  instructions, or ignore quoted text after instruction markers such as
  `section labeled`. Add a regression proving `OS-5` keeps the role/search
  constraints as the WorkItem target.
- Fix: Opportunity Scout primary-target extraction now prefers the cleaned
  opportunity-search request over quoted text, while still allowing quoted text
  as a fallback when no routeable opportunity target exists.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_multiline_no_result_role_search_to_opportunity_scout
  tests/test_workflow_runner.py::test_opportunity_no_result_prompt_keeps_search_target_not_quoted_output_label`
  and `.venv/bin/python -m py_compile src/keystone_agents/manual_request.py
  tests/test_manual_request_plan.py tests/test_workflow_runner.py`.

### P1 - BR-3 comparison prompt becomes a single fake company profile

- Found: 2026-05-25 13:48 EDT
- Fixed: 2026-05-25 13:54 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` `BR-3`
- Issue: The `BR-3` test-pack request asks Business Research to compare two
  companies as possible Keystone partners. The Orchestrator route lands on
  Business Research, but the WorkItem manager loop uses the manual
  `primary_target` as a single company name and calls the normal company-profile
  fixture path instead of a comparison path. The resulting artifact is a
  `company_profile`, not a side-by-side comparison.
- Evidence: A dry probe of `BR-3` through
  `infer_manual_request_plan(..., requested_agent="orchestrator")` produced
  `target_agent="orchestrator"`, `intent="continue_work_item"`, and
  `primary_target == "Compare Lindus Health and Holmusk as possible Keystone
  partnership or advisory targets. Use a decision-oriented format:"`. Running
  `advance_work_item_manager_loop()` then attached a `company_profile` titled
  with that full comparison sentence and contact candidates for the same fake
  company label, with no comparison artifact.
- Impact: A valid comparison request can appear to complete a Business Research
  step while never researching the two requested companies separately. Live
  testing would conflate extraction/routing failure with model synthesis
  quality and could produce stale or nonsensical source attribution.
- Expected fix: Teach the manual planner and WorkItem research path to detect
  comparison requests, extract both company names and comparison criteria, and
  call the existing comparison helper/schema or explicitly block with a
  `comparison_not_supported_in_workitem_phase` blocker. Add a regression for
  `BR-3` proving the manager loop does not create a one-company profile from
  the whole comparison sentence.
- Fix: Business Research comparison requests now extract `Company A vs Company
  B` as the target and the WorkItem research path creates a
  `company_comparison` artifact instead of a fake one-company profile.
- Verification: dry `BR-3` probe now returns `target_agent="business_research_analyst"`,
  `intent="company_research"`, `primary_target="Lindus Health vs Holmusk"`, and
  `advance_work_item_manager_loop()` attaches `company_comparison` titled
  `Lindus Health vs Holmusk`. Focused coverage is present in
  `tests/test_manual_request_plan.py::test_manual_plan_routes_company_comparison_to_business_research`
  and
  `tests/test_workflow_runner.py::test_company_comparison_workitem_creates_comparison_not_fake_profile`.

### P1 - BR-5 format instructions contaminate the company target

- Found: 2026-05-25 13:50 EDT
- Fixed: 2026-05-25 13:56 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` `BR-5`
- Issue: The `BR-5` prompt asks for research on a specific company and then
  gives a strict output structure. The manual planner treats the beginning of
  the formatting instruction as part of the company target, and the WorkItem
  runner creates a normal company profile under that contaminated label.
- Evidence: A dry probe of `BR-5` with `[Company]` replaced by `Lindus Health`
  returned `plan.primary_target == "Lindus Health for Keystone business
  development relevance. Return the output exactly in this structure: 1.
  Summary 2. Ev"`. `advance_work_item_manager_loop()` then attached
  `company_profile` and `contact_candidates` artifacts titled with that same
  contaminated string and did not block or preserve the requested exact output
  structure as a renderer constraint.
- Impact: Live Business Research runs for strict-format asks can search and
  persist the wrong company key, weakening source retrieval, contact enrichment,
  artifact reuse, and Slack follow-up commands. The operator may see a
  source-backed profile that appears successful but is stored under a prompt
  fragment rather than the company.
- Expected fix: Extract the company target separately from requested output
  format and research objective clauses such as `for Keystone business
  development relevance` and `Return the output as/exactly`. Preserve the
  structure request in manual-plan metadata or renderer constraints. Add a
  regression proving `BR-5` targets `Lindus Health` while retaining the five
  requested sections.
- Fix: Business Research target extraction now separates the company from the
  strict output-format instructions for `BR-5`.
- Verification: dry `BR-5` probe with `[Company]` replaced by `Lindus Health`
  now returns `target_agent="business_research_analyst"`,
  `intent="company_research"`, `primary_target="Lindus Health"`, and
  `advance_work_item_manager_loop()` attaches `company_profile` and
  `contact_candidates` artifacts titled for `Lindus Health`.

### P2 - Opportunity Scout domain terms trigger Business Research handoffs

- Found: 2026-05-25 13:47 EDT
- Fixed: 2026-05-25 13:47 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`, Opportunity Scout `OS-1` and
  `OS-3` workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: a dry manager-loop probe over the executable test pack showed
  `OS-1` and `OS-3` starting with Opportunity Scout, then advancing into
  Business Research Analyst even though `route_request()` planned only
  `["opportunity_scout"]`. The prompts mention domain background such as
  "clinical research" and include ordinary prompt sequencing like "Then return",
  but they do not ask to research/profile/source a selected company.
- Impact: Opportunity-only role and opportunity searches can create extra
  company-profile artifacts and leave the WorkItem in a cross-agent state that
  the operator did not request. This makes deterministic continuation heuristics
  override the planner's single-specialist route.
- Fix: manager-loop continuation now treats broad terms like `workflow` and
  `end-to-end` as explicit multi-step signals, but sequencing words like
  `then`, `and then`, and `next` require evidence for the proposed next
  specialist. Business Research continuation now requires explicit
  research/profile/source wording tied to a company, candidate, selected item,
  or source-backed task. Domain phrases like `clinical research` no longer
  trigger a downstream research handoff. The missing-stage detector also ignores
  references to an existing company research brief when blocking Outreach
  Composer for missing approved context.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_opportunity_only_domain_research_terms_do_not_trigger_research_handoff
  tests/test_workflow_runner.py::test_outreach_variants_from_existing_research_brief_do_not_request_new_research
  tests/test_workflow_runner.py::test_manager_loop_stops_before_repeating_specialist_for_orchestrator_workflow
  tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop
  tests/test_workflow_runner.py::test_manager_loop_continues_compound_request_without_then`
  and `.venv/bin/python -m py_compile src/keystone_agents/workflow_runner.py
  tests/test_workflow_runner.py`.

### P1 - Gmail-only test-pack requests get false downstream research/outreach blockers

- Found: 2026-05-25 13:41 EDT
- Fixed: 2026-05-25 13:53 EDT
- Status: fixed
- Area: `src/keystone_agents/agents/orchestrator.py`,
  `src/keystone_agents/workflow_runner.py`, Gmail Triage `GT-1` to `GT-4`
  workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: dry probes of `GT-1` to `GT-4` through
  `infer_manual_request_plan(..., requested_agent="orchestrator")`,
  `route_request(..., use_manual_plan=True)`, and
  `advance_work_item_manager_loop()` stop correctly at the Gmail context gate,
  but now add unrelated downstream blockers. `GT-1` gets route workflow
  `["gmail_triage", "business_research_analyst", "outreach_composer"]` and
  blockers `manager_loop_research_not_completed` and
  `manager_loop_outreach_not_drafted`; `GT-2`, `GT-3`, and `GT-4` get
  `manager_loop_outreach_not_drafted`. These prompts ask for Gmail triage,
  Gmail thread extraction, or draft-only Gmail replies, not company research or
  Outreach Composer work.
- Follow-up evidence at 2026-05-25 13:42 EDT: route planning no longer adds
  downstream workflow stages for `GT-1` to `GT-5`, which fixes the route
  overreach. The WorkItem missing-stage detector still adds false blockers:
  `GT-1` receives `manager_loop_research_not_completed` and
  `manager_loop_outreach_not_drafted`, while `GT-2`, `GT-3`, and `GT-4` receive
  `manager_loop_outreach_not_drafted`; `GT-5` correctly has only
  `gmail_context_required`.
- Impact: operator-facing Slack runs for normal Gmail triage/reply tasks can
  report missing Business Research or Outreach Composer stages, making the
  request appear like a failed cross-agent workflow instead of a blocked Gmail
  context request. That weakens natural-language handling and creates noisy
  follow-up guidance before live Gmail testing.
- Expected fix: scope `_gmail_cross_agent_workflow()` and
  `_manager_loop_missing_stage_blockers()` so Gmail reply/draft wording stays in
  Gmail Triage unless the request explicitly asks for external outreach,
  company research, opportunity records, CRM/pipeline steps, or non-Gmail
  outbound drafting. Add regressions that `GT-1` to `GT-4` stop at
  `gmail_context_required` without unrelated downstream blockers.
- Fix: narrowed Gmail cross-agent workflow detection to explicit
  company-research/opportunity-record/outreach-draft language, and made the
  manager-loop missing-stage detector treat Gmail reply drafts as Gmail Triage
  work unless the request also asks for non-Gmail research, opportunity, CRM,
  pipeline, LinkedIn, or outreach stages.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_gmail_only_workitems_do_not_get_false_downstream_stage_blockers
  tests/test_workflow_runner.py::test_gmail_workitem_route_blocks_for_context_without_unsupported_route
  tests/test_slack_agent_actions.py::test_modal_submission_gmail_request_uses_gmail_context_gate_not_unsupported_route
  tests/test_orchestrator.py::test_gmail_first_cross_agent_request_preserves_downstream_workflow
  tests/test_orchestrator.py::test_gmail_test_pack_prompts_route_to_gmail_triage`
  and `.venv/bin/python -m py_compile src/keystone_agents/workflow_runner.py
  tests/test_workflow_runner.py`.

### P1 - Business Research test-pack prompts route to Outreach Composer blockers

- Found: 2026-05-25 13:31 EDT
- Fixed: 2026-05-25 13:33 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/agents/orchestrator.py`, Business Research `BR-1` and
  `BR-4` workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: dry Orchestrator probes for `BR-1` and `BR-4` with
  `infer_manual_request_plan(..., requested_agent="orchestrator")` infer
  `target_agent="outreach_composer"`, `intent="outreach_draft"`, and targets
  like `Prepare` or `a thin-data`. Running those prompts through
  `advance_work_item_manager_loop()` then routes to Outreach Composer and blocks
  with `outreach_requires_approved_context` instead of producing a Business
  Research brief.
- Impact: documented Business Research asks that naturally mention "possible
  outreach angle" or "minimum additional information needed before outreach" can
  fail before research. This weakens natural-language request processing for
  research briefs and would make Slack run-agent responses look like missing
  approved outreach context rather than the requested source-backed company
  analysis.
- Expected fix: make explicit research verbs and the Business Research test-pack
  shapes authoritative over downstream outreach-angle wording. Outreach wording
  inside requested sections should become a constraint or future-use note, not
  a route change, unless the operator explicitly asks to draft/write/compose an
  outbound message. Add focused route/WorkItem coverage for `BR-1` and `BR-4`
  via Orchestrator.
- Verification: current dry probes now infer
  `target_agent="business_research_analyst"` and `intent="company_research"`
  for both `BR-1` and `BR-4`, and `tests/test_manual_request_plan.py` plus
  `tests/test_orchestrator.py` include focused coverage for Business Research
  prompts with outreach-angle wording.

### P2 - Research-only BR-1 manager loop advances to Opportunity Scout on "leadership"

- Found: 2026-05-25 13:33 EDT
- Fixed: 2026-05-25 13:35 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` Business Research `BR-1`
- Evidence: after the routing fix, a dry `BR-1` manager-loop probe starts with
  `business_research_analyst`, then advances into `opportunity_scout` and ends
  `in_progress` with artifacts `company_profile`, `contact_candidates`, and
  `source_summary`, even though `route_request()` says the intended workflow is
  only `["business_research_analyst"]`. A direct probe of
  `_operator_requested_manager_continuation(..., next_action_agent=OPPORTUNITY_SCOUT)`
  returns `True` for a research-only sentence containing "leadership and
  credibility signals" because the marker `" lead"` matches the beginning of
  "leadership".
- Impact: source-backed company research asks can unexpectedly trigger
  opportunity/contact discovery, produce extra artifacts, and leave the WorkItem
  active instead of returning the requested research brief. In Slack this can
  make a straightforward research brief appear as a multi-step opportunity run.
- Expected fix: make manager-loop continuation token-aware and plan-aware. Use
  word-boundary matching for `lead`/`leads`, and prefer the Orchestrator/manual
  plan workflow over substring heuristics when deciding whether to continue into
  a distinct specialist. Add a regression for `BR-1` that stops after Business
  Research unless a true opportunity/lead-discovery step is requested.
- Fix: manager-loop continuation now uses token-aware regular-expression
  markers for opportunity, research, and outreach continuation checks, so
  `leadership` no longer satisfies the `lead` marker. The `BR-1` manager-loop
  regression verifies the run stops after Business Research with no
  Opportunity Scout review event unless the operator actually asked for
  opportunity/lead discovery.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_manager_loop_does_not_treat_leadership_as_lead_discovery
  tests/test_workflow_runner.py::test_manager_loop_continues_compound_request_without_then
  tests/test_workflow_runner.py::test_manager_loop_does_not_continue_plain_summary_conjunction`
  and `.venv/bin/python -m py_compile src/keystone_agents/workflow_runner.py
  tests/test_workflow_runner.py`.

### P1 - Gmail-first cross-agent workflows collapse to Gmail-only context gate

- Found: 2026-05-25 13:31 EDT
- Fixed: 2026-05-25 13:40 EDT
- Status: fixed
- Area: `src/keystone_agents/slack_actions.py`,
  `src/keystone_agents/workflow_runner.py`,
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md` cross-agent `CA-1` and `CA-4`
  workflows
- Evidence: Slack run-agent modal submissions call
  `advance_work_item_manager_loop()` for every Orchestrator-approved request.
  A dry probe of the `CA-1` shape ("A Gmail consulting inquiry came in. Triage
  it, research the company, create an opportunity record, and draft a response
  only after approval.") correctly plans `target_agent="gmail_triage"`, but the
  WorkItem runner returns `route_not_supported_in_workitem_phase` with message
  `WorkItem Phase II supports Business Research Analyst and Opportunity Scout
  only; gmail_triage is not enabled here.` No Gmail artifact is created and the
  loop stops before research, opportunity, or draft-only follow-up. `CA-4`
  reply-tracking shapes also route to Gmail Triage and would hit the same
  unsupported WorkItem path from Slack run-agent.
- Follow-up evidence at 2026-05-25 13:35 EDT: the route plan for the same
  `CA-1` shape is also only `["gmail_triage"]`, even though the documented
  workflow asks for Gmail Triage, Business Research, Opportunity Scout/pipeline,
  and Outreach Composer after approval. Enabling Gmail as a WorkItem route alone
  may not preserve the required downstream sequence unless the planner or
  manager-loop continuation also carries the full cross-agent workflow.
- Follow-up evidence at 2026-05-25 13:38 EDT: Gmail is now recognized as a
  WorkItem route and blocks with `gmail_context_required` instead of
  `route_not_supported_in_workitem_phase`, which fixes the unsupported-route
  portion. The cross-agent workflow gap remains: `CA-1`, `CA-4`, and a
  Gmail-plus-research/draft probe still plan only `["gmail_triage"]`, produce no
  Gmail/research/opportunity/outreach artifacts without supplied Gmail context,
  and stop before the documented downstream sequence can be represented.
- Impact: Orchestrator-first Slack requests that start with email/Gmail context
  can pass preflight and then fail inside the WorkItem manager loop, even though
  the documented architecture expects Gmail context packs and cross-agent
  handoffs. This creates a mismatch between natural-language routing,
  operator-facing Slack lifecycle status, and actual executable specialist
  support.
- Expected fix: keep the Gmail context gate, but preserve the full requested
  workflow plan when a Gmail-first request also asks for research, opportunity
  records, reply tracking, or draft-only outreach. Once Gmail context is
  provided or live Gmail retrieval is explicitly enabled, the manager loop
  should continue through the appropriate downstream stages or record explicit
  missing-stage blockers for them. Add a regression for `CA-1` or a
  Gmail-to-research Slack run-agent request that verifies the downstream
  workflow is not silently collapsed to Gmail-only.
- Fix: Gmail-first cross-agent route planning now preserves downstream stages
  such as Business Research, Opportunity Scout, and Outreach Composer when the
  request asks for them. If Gmail context is missing, the WorkItem still stops at
  the `gmail_context_required` gate, but manager-loop completion now records
  missing downstream blockers including `manager_loop_research_not_completed`,
  `manager_loop_opportunity_not_created`, and
  `manager_loop_outreach_not_drafted`.
- Verification: dry `CA-1` probe now returns route workflow
  `["gmail_triage", "business_research_analyst", "opportunity_scout",
  "outreach_composer"]`, then blocks at `gmail_context_required` with the three
  missing downstream stage blockers above. Focused tests passed:
  `.venv/bin/python -m pytest
  tests/test_orchestrator.py::test_gmail_first_cross_agent_request_preserves_downstream_workflow
  tests/test_workflow_runner.py::test_gmail_workitem_route_blocks_for_context_without_unsupported_route
  tests/test_slack_agent_actions.py::test_modal_submission_gmail_request_uses_gmail_context_gate_not_unsupported_route
  tests/test_manual_request_plan.py::test_manual_plan_routes_gmail_test_pack_prompts_to_gmail_triage
  tests/test_orchestrator.py::test_gmail_test_pack_prompts_route_to_gmail_triage`
  and `.venv/bin/python -m py_compile
  src/keystone_agents/agents/orchestrator.py src/keystone_agents/workflow_runner.py
  tests/test_orchestrator.py tests/test_workflow_runner.py
  tests/test_slack_agent_actions.py`.

### P1 - CA-5 WorkItem run loses "best three" count and send-blocked audit

- Found: 2026-05-25 13:33 EDT
- Fixed: 2026-05-25 13:39 EDT
- Status: fixed
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/workflow_runner.py`, cross-agent `CA-5` from
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: `route_request()` now preserves the safe CA-5 workflow for
  "Find and send outreach to the best three companies." as
  `["opportunity_scout", "business_research_analyst", "outreach_composer",
  "send_blocked"]`, with `send_email` forbidden. The executable dry WorkItem
  probe still infers `desired_count=1` instead of 3, starts
  `opportunity_scout`, blocks with only `no_opportunities_found`, and records no
  explicit send-blocked or draft-only approval blocker in
  `manager_loop_completed.metadata.missing_required_stages`.
- Impact: the route card says the unsafe send step is blocked, but the actual
  WorkItem run can fail as a generic no-results scout run before preserving the
  operator-visible send refusal, top-three scope, research/draft-only sequence,
  or approval audit requirements. In Slack this would make a high-risk
  find-and-send request look like a weak opportunity search rather than a
  safely decomposed draft-only workflow.
- Expected fix: parse common spelled counts such as "three" for manual plans,
  and carry `send_blocked`/forbidden-action stages into manager-loop completion
  metadata even when an earlier step blocks. Add WorkItem-level coverage for
  `CA-5`, not just `route_request()`.
- Fix: manual planning now parses spelled counts from common discovery phrasing
  such as "best three companies", and manager-loop finalization adds explicit
  `manager_loop_send_blocked` plus draft-stage blockers for affirmative send
  requests that are preserved as draft-only workflows.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_preserves_safe_workflow_for_find_and_send_request
  tests/test_workflow_runner.py::test_find_and_send_workitem_preserves_count_and_send_blocker
  tests/test_workflow_runner.py::test_orchestrator_connected_workflow_records_missing_downstream_stage_blockers
  tests/test_orchestrator.py::test_find_and_send_outreach_preserves_draft_only_workflow`
  and `.venv/bin/python -m py_compile src/keystone_agents/manual_request.py
  src/keystone_agents/workflow_runner.py tests/test_manual_request_plan.py
  tests/test_workflow_runner.py tests/test_orchestrator.py`.

### P1 - Gmail test-pack prompts route to research or opportunity lanes

- Found: 2026-05-25 13:21 EDT
- Fixed: 2026-05-25 13:21 EDT
- Status: fixed
- Area: `src/keystone_agents/orchestrator/routing.py`,
  `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/agents/orchestrator.py`, Gmail Triage `GT-1` to `GT-5`
  workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: probing the executable Gmail test-pack prompts through
  `infer_manual_request_plan(..., requested_agent="orchestrator")` and
  `route_request(...)` showed `GT-1` routing to Business Research,
  `GT-3` routing to Opportunity Scout, and `GT-2` being hard-refused because
  `legal_review` was treated as a hard Orchestrator refusal instead of a
  human-review flag for COI/professional-liability email handling.
- Impact: Slack/direct natural-language Gmail asks such as "review my emails",
  "latest email thread", "prepare a reply", and "current email context" could
  miss Gmail Triage unless the operator explicitly named the agent. That weakens
  Orchestrator-first handling for diverse Slack requests and can send email
  context to the wrong specialist.
- Fix: broadened email-workflow detection for Gmail/thread/reply wording, moved
  email detection before broad workflow/discovery fallbacks, kept send requests
  blocked by the existing send gate, and removed `legal_review` from hard
  refusal flags while keeping legal-position/advice text blocked through the
  professional-advice guardrail.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_gmail_test_pack_prompts_to_gmail_triage
  tests/test_orchestrator.py::test_gmail_test_pack_prompts_route_to_gmail_triage
  tests/test_orchestrator.py::test_safety_risks_are_refused_before_llm_router
  tests/test_safety.py::test_legal_fixture_is_flagged_and_only_acknowledged`
  passed.

### P0 - Slack preflight refusals should stop before WorkItem execution

- Status: fixed locally. Slack modal submissions now return a blocked
  `SlackAgentActionResult` when Orchestrator preflight sets
  `execution_allowed=false`, before WorkItem creation or manager-loop synthesis.
- Surface: `src/keystone_agents/slack_actions.py`, `src/keystone_agents/workflow_runner.py`
- Issue: Slack modal submission records `blocked_by_orchestrator`, but still calls `advance_work_item_manager_loop()`. Blocked send requests currently land as a blocked WorkItem, but the path still persists request text and, with `live_sdk=True`, can invoke user-facing response synthesis on a refused request.
- Impact: Safety/privacy risk for PHI/security/legal/professional-advice inputs, plus unnecessary live model calls.
- Fix direction: Add a shared hard-block predicate or explicit `OrchestratorPreflight.block_reason` field. In Slack submission handling, return a blocked `SlackAgentActionResult` before WorkItem creation and before any synthesis when the preflight hard-blocks execution.

### P1 - Live manager loop synthesizes user-facing output inside every step

- Status: fixed locally. `advance_work_item_manager_loop()` now advances
  intermediate steps without user-facing synthesis and runs one final synthesis
  pass after the bounded loop stops.
- Surface: `src/keystone_agents/workflow_runner.py`, `src/keystone_agents/response_synthesis.py`
- Issue: `advance_work_item()` runs `_maybe_synthesize_user_facing_response()` before `advance_work_item_manager_loop()` decides whether another specialist step is needed.
- Impact: Multi-step live runs can make extra model calls, increase latency/rate-limit exposure, and store intermediate "final" responses in the same WorkItem/Slack SDK session.
- Fix direction: Move response synthesis to manager-loop finalization only. Consider a separate/no SDK session for the synthesis agent so intermediate specialist context does not pollute follow-up conversation state.

### P1 - Direct live child agent scripts have no timeout boundary

- Status: fixed locally. Direct live child script calls now use a bounded
  timeout and return structured timeout payloads; the Slack bridge already wraps
  the KBA handler child process with `KNI_BUSINESS_AGENTS_TIMEOUT_SECONDS`.
- Surface: `src/keystone_agents/cli.py`, `scripts/handle_slack_agent_action.py`, sibling `keystone-slack` bridge process management
- Issue: `_run_ask_script_live()` uses `subprocess.run(..., capture_output=True)` without a timeout. The Slack action handler also exposes no child-run timeout or structured timeout payload.
- Impact: A hung live SDK/search call can block the parent indefinitely and leave background Slack bridge work stuck on the local Mac.
- Fix direction: Add bounded timeouts, structured timeout/error results, and process cleanup. If the sibling Slack bridge starts background workers, it should own process-group termination and final user-visible failure updates.

### P2 - Feedback JSONL shares stderr with tracebacks and logs

- Status: fixed locally. The Slack action handler catches exceptions, emits a
  final `agent_error` JSONL feedback event when requested, and returns a
  structured JSON error payload; the Slack bridge ignores non-JSON stderr lines.
- Surface: `scripts/handle_slack_agent_action.py`, `src/keystone_agents/slack_action_contract.py`
- Issue: `--feedback-jsonl` writes progress events to stderr. Uncaught exceptions, Python tracebacks, warnings, or provider logs also write to stderr.
- Impact: A Slack bridge consuming stderr as strict JSONL can misparse failures or lose progress/error state.
- Fix direction: Wrap the handler in structured exception handling and emit a final `agent_error` feedback event plus JSON result. The bridge should ignore non-JSON stderr lines or use a separate framed transport for progress events.

### P2 - Slack context file handoff is cwd-sensitive

- Status: fixed locally. Context files are written as absolute paths, modal
  metadata embeds bounded selected context as a fallback, and submissions reload
  from the file or rewrite the embedded fallback when the file is missing.
- Surface: `src/keystone_agents/slack_actions.py`, sibling `keystone-slack` modal open/submission flow
- Issue: Modal metadata commonly stores `context_file_path`. If that path is relative, a second process launched from a different cwd can fail to reload selected Slack context. If the context file is deleted before submission, the run fails instead of degrading.
- Impact: Lost Slack thread context, broken provenance validation, or failed modal submissions across repo/process boundaries.
- Fix direction: Store absolute context paths, validate they remain under the configured context directory, and include a bounded embedded fallback context in modal private metadata.

### P2 - CLI hard-block detection is string-based

- Status: fixed locally. `OrchestratorPreflight` now carries
  `execution_allowed`, `block_kind`, and `block_reason`; CLI and Slack use those
  fields instead of parsing refusal prose.
- Surface: `src/keystone_agents/cli.py`, `src/keystone_agents/agents/orchestrator.py`
- Issue: `_preflight_blocks_execution()` searches refusal text for `"send gate"` or `"safety gate"` to decide whether to stop execution.
- Impact: Future Orchestrator wording changes can accidentally bypass a hard block or block a soft clarification.
- Fix direction: Add explicit structured fields to `OrchestratorPreflight`, such as `blocked_by_orchestrator`, `block_kind`, and `execution_allowed`, and have both CLI and Slack use those fields.

### P2 - Channel automation context is useful but narrowly gated by keywords

- Status: fixed locally. The Slack context trigger now covers operational
  phrasings such as "what is running here", "why did this post", and "state of
  this channel", with regression coverage.
- Surface: `src/keystone_agents/slack_actions.py`, `src/keystone_agents/agents/orchestrator.py`
- Issue: Channel automation inventory is only included when the request contains a fixed set of automation/schedule terms. Related natural-language asks such as "what is running here", "why did this post", or "state of this channel" may omit automation context.
- Impact: The Orchestrator can answer operational Slack questions without the state needed to explain scheduled posts or channel behavior.
- Fix direction: Broaden the routing/context trigger with tests using real Slack request phrasing, or let Chief of Staff request bounded automation context through an explicit tool when Slack channel context is present.

### P2 - Orchestrator registry card is missing new manager-as-tool entries

- Status: fixed locally. `ORCHESTRATOR_AGENT_SPEC.tools` now includes
  the default Orchestrator tool surface and `ORCHESTRATOR_AGENT_SPEC.optional_tools`
  now declares `business_research_analyst_research_brief` and
  `opportunity_scout_read_only`. Registry tests compare optional tool names
  against the tool policy and opted-in built agent.
- Surface: `src/keystone_agents/agent_registry.py`, `src/keystone_agents/agent_tool_policy.py`, `src/keystone_agents/agents/orchestrator.py`
- Issue: The Orchestrator runtime and tool policy now include `business_research_analyst_research_brief` and `opportunity_scout_read_only`, but the canonical `ORCHESTRATOR_AGENT_SPEC.tools` list does not include those agent-as-tool names.
- Impact: CLI inspection, agent cards, extension tests, and operator review can understate the Orchestrator's actual tool surface, which weakens architecture review and cross-repo debugging.
- Fix direction: Add the agent-as-tool names to `ORCHESTRATOR_AGENT_SPEC.tools` and add/extend a registry test that compares the registry card against the Orchestrator tool policy or built agent tool names.

### P2 - Slack contract generator and contract function expose different payload schemas

- Status: fixed locally. `business_agent_slack_contract()` now includes the
  selected-message context JSON schema, and the export script writes the
  canonical builder output directly.
- Surface: `src/keystone_agents/slack_action_contract.py`, `scripts/export_slack_action_contract.py`, `contracts/keystone_slack_business_agent_contract.v1.json`
- Issue: `business_agent_slack_contract()` returns payload schemas for business actions, feedback events, and write gates, while `scripts/export_slack_action_contract.py` injects `selected_message_context` separately. Consumers importing the function directly do not see the full selected-context payload schema.
- Impact: Cross-repo contract checks can disagree depending on whether they read the artifact or call the Python contract function.
- Fix direction: Move selected-context schema inclusion into the canonical contract builder, or document/export a second explicit function for artifact-only schemas. Add a test that compares the exported artifact payload schema keys with the builder output.

### P2 - Selected Slack context schema changed without explicit compatibility marker

- Status: fixed locally. The Slack contract now exposes explicit capabilities:
  `feedback_jsonl`, `selected_context_prior_agent_runs`,
  `selected_context_embedded_fallback`, and `write_gate_no_send`.
- Surface: `src/keystone_agents/slack_actions.py`, `contracts/keystone_slack_business_agent_contract.v1.json`, sibling `keystone-slack`
- Issue: `SlackSelectedMessageContext` now supports `prior_agent_runs`, and `agent_feedback_event` was added to the Slack contract while the top-level contract version remains `"1"`.
- Impact: This is probably backward-compatible, but downstream bridge code cannot distinguish "v1 with feedback/prior-run context" from older v1 artifacts unless it probes schema keys.
- Fix direction: Either bump or add a `capabilities` list such as `feedback_jsonl` and `selected_context_prior_agent_runs`; have sibling bridge startup validate required capabilities before enabling real-time feedback and prior-run learning.

### P3 - Review alignment heuristic may penalize valid concise outputs

- Status: fixed locally. Request/output review now allows concise architecture
  outputs through concept overlap for planning, routing, context, quality, and
  safety while preserving the unrelated tax-output regression.
- Surface: `src/keystone_agents/agents/orchestrator.py`, `scripts/run_chief_of_staff.py`
- Issue: Request/output alignment uses token overlap after removing selected echo fields. Concise but correct outputs can fail when they use synonyms or structured fields that do not repeat request terms.
- Impact: False unrelated-output detection can trigger deterministic fallback, hiding useful live SDK output and reducing agent performance.
- Fix direction: Add regression cases for synonym-heavy and structured-but-concise outputs. Prefer source/target/entity overlap and artifact type validation over raw token overlap alone.

### P2 - Selected-message Slack runs still append internal status to synthesized answers

- Status: fixed locally. The sibling selected-message bridge now uses the same
  live-synthesis audit marker as ordinary WorkItem advancement and suppresses
  WorkItem, route, and status lines from synthesized user-facing answers.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`, `src/keystone_agents/response_synthesis.py`
- Issue: The ordinary WorkItem bridge suppresses WorkItem/route/status lines when it sees the `"Live user-facing response synthesis executed."` audit marker, but the selected-message modal path always appends `WorkItem`, `Route`, and `Status` to `result.human_summary` before updating the modal/source thread.
- Impact: Live synthesized answers from Slack message actions can still expose internal workflow plumbing in the main user-facing response, even though the synthesis prompt and output policy say not to present route metadata or workflow status as the answer.
- Fix direction: Add an explicit bridge-safe field such as `user_facing_text`/`operator_summary`, or have the selected-message stage use the same synthesis detection and suppression path as `_work_item_advance_success()`. Keep WorkItem ID, route, status, provenance, and feedback events in metadata/actions only.

### P2 - Slack provenance accepts missing context fields as validated

- Status: fixed locally. Slack-originated provenance now requires WorkItem
  Slack context fields to be present and equal whenever selected context
  supplied channel/message/thread identity.
- Surface: `src/keystone_agents/slack_actions.py`
- Issue: `_slack_run_provenance_errors()` only reports a mismatch when both expected and actual Slack context fields are non-empty. If the selected context has `channel_id`, `selected_message_ts`, or `thread_ts`, but the WorkItem context pack drops one of those fields, provenance still returns `context_validated=True` as long as the request text matches.
- Impact: The bridge can mark a run as tied to the selected Slack message even when the child WorkItem did not actually ingest the selected Slack channel/message/thread identity. That weakens cross-thread safety checks and makes prior-run learning harder to trust.
- Fix direction: For Slack-originated modal submissions, require actual WorkItem Slack context fields to be present and equal whenever the selected context provided them. Add a regression test where `context` has channel/message/thread values but `context_pack.slack_context` is empty or partial.

### P1 - Slack modal private metadata can exceed limits or become invalid JSON

- Found: 2026-05-25 11:47 EDT
- Fixed: 2026-05-25 11:48 EDT
- Status: fixed locally. Modal metadata now embeds only a deliberately small
  selected-message fallback plus the context-file pointer; full thread messages
  and prior runs stay in the context file.
- Surface: `src/keystone_agents/slack_actions.py`, sibling `keystone-slack/kni_integrations/business_agents_bridge.py`
- Issue: KBA's modal builder embeds the full `selected_context` in Slack `private_metadata`, including selected-message text and up to 20 thread messages. The sibling bridge has a size fallback, but if the reduced payload is still too large, it slices the serialized JSON to 2900 characters, which can produce invalid JSON. Prior agent runs and feedback can also push metadata over the limit even when thread messages are omitted.
- Impact: Real message-action modals can fail to open or submit, or lose selected context exactly when longer threads/prior feedback are most important for orchestration.
- Fix direction: Treat `private_metadata` as an opaque pointer plus a tiny checksum/provenance tuple. Always write full selected context to a context file when thread messages, prior runs, warnings, or long selected text are present. If an embedded fallback is needed, use a deliberately small schema that is serialized only after size validation, never post-serialization truncation.

### P2 - Manager-loop continuation misses common compound requests

- Found: 2026-05-25 11:47 EDT
- Fixed: 2026-05-25 11:48 EDT
- Status: fixed locally. Manager-loop continuation now considers the saved next
  specialist action plus compound request markers, so "research X and find
  opportunities" continues without requiring "then" while plain summary
  conjunctions remain single-step.
- Surface: `src/keystone_agents/workflow_runner.py`, `src/keystone_agents/manual_request.py`
- Issue: `_operator_requested_manager_continuation()` recognizes phrases such as "and then", "then", "workflow", and "end to end", but not common compound asks like "research NeuroFlow and draft outreach" or "find candidates and prepare a draft". Those requests can produce a next action for another specialist, then stop because the original text is treated as not requesting a multi-step workflow.
- Impact: Natural-language cross-agent requests can underperform by stopping after research/opportunity discovery instead of continuing to the requested downstream draft/recommendation step. Operators have to know to say "and then" or "workflow" to get the planner/orchestrator behavior.
- Fix direction: Base continuation on the manual plan/orchestrator workflow and the next distinct specialist action, not only literal continuation markers. Add regression cases for "research X and draft outreach", "find opportunities and prepare email drafts", and "summarize this thread and recommend next action" without the word "then".

### P1 - Orchestrator specialist tools are enabled by default despite opt-in contract

- Found: 2026-05-25 11:47 EDT
- Fixed: 2026-05-25 11:48 EDT
- Status: fixed locally. Specialist-as-tool access is default-off again and
  available only through `include_specialist_tools=True` or
  `KEYSTONE_ORCHESTRATOR_SPECIALIST_TOOLS=1`; optional tool names remain visible
  in registry metadata.
- Surface: `src/keystone_agents/agents/orchestrator.py`, `tests/test_orchestrator.py`
- Issue: `build_orchestrator_agent()` now uses `_env_flag_default_enabled(KEYSTONE_ORCHESTRATOR_SPECIALIST_TOOLS)`, so `business_research_analyst_research_brief` and `opportunity_scout_read_only` are present when the env var is unset. Existing tests and docs still describe specialist agent-as-tool access as opt-in.
- Impact: Default Orchestrator builds have a larger tool surface, slower construction, and higher risk of unintended nested agent execution/cost during ordinary routing tests or live runs. It also breaks the current Orchestrator tests.
- Fix direction: Restore default-off semantics with `_env_flag_enabled()` or explicitly update the policy/docs/tests if default-on is intentional. If default-on is chosen, add a narrower safety/cost gate and a test proving no live specialist model calls happen during ordinary route-only Orchestrator use.

### P2 - Source-summary Opportunity Scout requests block in fixture mode

- Found: 2026-05-25 11:47 EDT
- Fixed: 2026-05-25 11:49 EDT
- Status: fixed locally. Dry-run source-summary Opportunity Scout requests now
  generate an explicitly labeled fixture source-summary candidate only when
  the requested target terms match the request/topic; mismatched required terms
  still block.
- Surface: `src/keystone_agents/workflow_runner.py`, `src/keystone_agents/manual_request.py`
- Issue: Manual planning can map requests such as "opportunity scout find summaries of APA 2026 meeting in San Francisco" to `task_objective=source_research` and `expected_artifact_type=source_summary`. `_advance_opportunity()` then calls `_opportunity_source_summary_result()`, which only reads `metadata["retrieved_source_candidates"]`. In default fixture mode, `metadata` is empty, so the run blocks with `source_summary_target_not_found` before using any fixture result or deterministic fallback source candidates.
- Impact: A natural-language source-summary request works only with live retrieval or mocked live metadata, undermining dry-run-first development and Slack/manual `@KNI` smoke tests for diverse requests.
- Fix direction: Populate deterministic fixture source candidates for source-summary mode, or explicitly require live search with a targeted blocker that says live retrieval is required. Add a dry-run regression for APA/source-summary style requests so the fallback behavior is intentional.

### P2 - Full Orchestrator preflight is transported and re-emitted across child boundaries

- Found: 2026-05-25 11:47 EDT
- Fixed: 2026-05-25 11:51 EDT
- Status: fixed locally. Child-process env handoff and child JSON attachment
  now use a compact Orchestrator preflight payload with route/safety/manual-plan
  fields and a memo, while omitting raw `workflow_state_summary`, Slack thread
  summaries, prior feedback text, and channel automation details.
- Surface: `src/keystone_agents/orchestrator/preflight_context.py`, `src/keystone_agents/cli.py`, `src/keystone_agents/slack_actions.py`, `src/keystone_agents/workflow_runner.py`
- Issue: `orchestrator_preflight_context_text()` builds a compact specialist memo, but `orchestrator_preflight_env()` still serializes the full `OrchestratorPreflight` and full `route_result` into child-process environment variables. Child scripts then reattach the full preflight object to JSON payloads, and Slack modal runs store/re-emit it in `WorkflowRunResult.orchestrator_preflight`. That full object can include `workflow_state_summary.recent_slack_thread`, `prior_agent_runs`, `slack_context`, and channel automation state.
- Impact: Selected-message runs duplicate private Slack summaries and operator feedback across process env, stdout JSON, WorkItem result payloads, and Slack bridge responses. This increases payload size, makes logs/results more sensitive than necessary, and couples child specialist outputs to Orchestrator-internal state that the specialist only needs as a compact memo.
- Fix direction: Keep full preflight state inside the Orchestrator/WorkItem event audit, but pass children a bounded handoff payload: selected agent, manual plan, execution/block fields, and the compact preflight memo. Attach a sanitized preflight summary to child JSON outputs instead of the full `route_result.workflow_state_summary`; add regression coverage proving recent Slack thread text and prior feedback are not re-emitted in child stdout/result payloads.

### P2 - Child script timeout does not isolate or clean descendant processes

- Found: 2026-05-25 11:50 EDT
- Fixed: 2026-05-25 11:54 EDT
- Status: fixed locally. CLI and scheduled automation child runs now use a
  shared isolated child-process helper that starts a new process session/group
  and kills the group on timeout before returning the structured timeout path.
- Surface: `src/keystone_agents/cli.py`, `scripts/run_keystone_automation.py`, sibling `keystone-slack` bridge process management
- Issue: Direct child agent runs now have a timeout and structured timeout payload, but they still launch with plain `subprocess.run(...)` and no `start_new_session`, process group, or explicit descendant cleanup. Python's timeout handling kills the immediate child process, but any grandchildren started by a specialist path, browser/rendering helper, hosted-tool shim, or automation child can survive outside the parent timeout boundary.
- Impact: A timed-out live request can still leave local Mac helper processes running, especially when future specialist paths add rendered-browser diagnostics, sandbox review, or provider shims. That weakens the intended child-process boundary and can cause stale CPU/network use after Slack or CLI has already reported timeout.
- Fix direction: Use a shared child-run helper that starts a new process group/session, kills the group on timeout, waits/reaps, and returns a structured timeout result. Add regression coverage that verifies the CLI/automation runner passes the process-isolation flags and that timeout handling calls the group cleanup path.

### P1 - Slack revise-draft modal still truncates JSON private metadata

- Found: 2026-05-25 11:51 EDT
- Fixed: 2026-05-25 11:56 EDT
- Status: fixed locally in sibling `keystone-slack`. Revision modal metadata
  now uses a minimal JSON payload with approval/source identifiers and bounded
  title only; large action metadata is no longer serialized then truncated.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`, KBA approval/action payloads
- Issue: The selected-message run modal now uses a small embedded fallback, but the sibling bridge's `_revision_modal_view()` still builds `private_metadata` with `json.dumps(action_payload, ...)[:2900]`. If the action payload metadata contains a long approval title, notes, source context, or other future action metadata, this slices inside the JSON string and produces invalid modal metadata.
- Impact: Revise-draft actions can open a modal that Slack accepts as opaque text but the submission handler cannot parse, losing approval ID/source message context and preventing the revision from being queued. It also reintroduces the exact invalid-JSON failure mode fixed for selected-message run modals.
- Fix direction: Apply the same pointer/minimal-payload rule to revision modals: include only schema, intent, approval ID, source channel/message/thread, and a bounded title in `private_metadata`. Store any larger context in the existing durable run/action state, and add a regression that `json.loads(view["private_metadata"])` succeeds for oversized action metadata.

### P1 - Sibling run-agent modal fallback still truncates prior-run context

- Found: 2026-05-25 11:52 EDT
- Fixed: 2026-05-25 11:56 EDT
- Status: fixed locally in sibling `keystone-slack`. Run-agent message-action
  modals now write a selected-context file whenever thread messages, prior
  agent runs, warnings, or long selected text are present, and private metadata
  is always a valid pointer plus bounded embedded fallback.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`, `src/keystone_agents/slack_actions.py`
- Issue: KBA's selected-message modal builder now uses a small embedded fallback, but the sibling bridge still writes a selected-context file only when `thread_messages` are present. `_business_agents_run_agent_modal_metadata()` first embeds the full context, then falls back by removing only `thread_messages`, leaving `prior_agent_runs`, selected text, permalink, warnings, and other metadata in `private_metadata`; if that fallback is still over the limit, it slices the serialized JSON to 2900 characters.
- Impact: Slack message actions with prior Business Agent runs but no fetched thread messages can still open modals with invalid JSON metadata. The modal submission then loses selected Slack context/prior-run learning, or fails to route the selected-message run back through the intended KBA context file.
- Fix direction: In the sibling bridge, write a context file whenever thread messages, prior runs, warnings, or long selected text are present. Make `private_metadata` an always-valid pointer/minimal fallback and remove all post-serialization truncation. Add regression coverage for oversized `prior_agent_runs` with no `thread_messages`.

### P2 - Ambiguous Slack requests still use legacy route-only Orchestrator bridge

- Found: 2026-05-25 11:54 EDT
- Fixed: 2026-05-25 11:58 EDT
- Status: fixed locally in sibling `keystone-slack`. Ambiguous business-agent
  requests now fall through to canonical KBA `ask` instead of the legacy
  route-only Orchestrator bridge, while explicit compatibility paths remain.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`, `src/keystone_agents/cli.py`, `src/keystone_agents/workflow_runner.py`
- Issue: General research/opportunity/direct-agent Slack requests now enter KBA through the canonical `keystone_agents.cli ask` path, but ambiguous business-agent requests still fall through to `_run_orchestrator_route()`, which calls `scripts/run_orchestrator.py --json` and then optionally runs legacy follow-on scripts such as `_run_company_research()` or `_run_weekly_opportunity_workflow()`. That bypasses the new Orchestrator preflight, WorkItem manager loop, typed context packs, and final user-facing synthesis path used by canonical ask.
- Impact: Two Slack requests with similar natural-language shape can use different architecture paths, output formats, provenance, session behavior, and safety gates. This makes cross-agent behavior harder to reason about and can leave route-only Orchestrator output or legacy follow-on script output where the user expected the manager/orchestrator agent to own the whole request.
- Fix direction: Route ambiguous business-agent requests through canonical `ask` by default and keep legacy `_run_orchestrator_route()` behind an explicit compatibility flag or narrow command. Add bridge tests proving ambiguous asks produce WorkItem/manager-loop results rather than route-only Orchestrator text, while preserving the special opportunity-to-outreach and manual automation compatibility paths that still need dedicated renderers.

### P2 - Scheduled automation child timeout escapes without failure summary

- Found: 2026-05-25 11:55 EDT
- Fixed: 2026-05-25 11:58 EDT
- Status: fixed locally. Scheduled automation child timeouts now convert to a
  failed `ChildRun`, record failed automation state, and print a JSON failure
  summary when `--json` is requested.
- Surface: `scripts/run_keystone_automation.py`, `src/keystone_agents/child_process.py`, `tests/test_automation_control.py`
- Issue: Scheduled automation child commands now use the isolated child-process helper, but `_run_child()` does not catch `subprocess.TimeoutExpired`. A timeout therefore escapes from `main()` before `_automation_summary()`, `_record_automation_summary()`, `_notify_failure()`, or `_print_child_failure()` run.
- Impact: A timed-out weekly opportunity or Gmail automation can terminate the wrapper without a structured JSON failure payload, stored automation-run failure record, redacted failure summary, or Slack failure notification. This undermines operational visibility even though the process group itself is cleaned up.
- Fix direction: Catch `TimeoutExpired` in `_run_child()` or `main()` and convert it to a `ChildRun` with a non-zero return code, redacted stderr/stdout excerpts, and timeout metadata. Add a regression that monkeypatches `run_isolated_child_process` to raise `TimeoutExpired` and asserts `main(... --json)` returns a failure code and records a failed automation run.

### P2 - CLI preflight-blocked payload still emits full Orchestrator state

- Found: 2026-05-25 11:56 EDT
- Fixed: 2026-05-25 11:57 EDT
- Status: fixed locally. Blocked CLI ask payloads now use the compact
  Orchestrator preflight payload and compact route-result output, omitting raw
  `workflow_state_summary`, recent Slack thread summaries, and prior feedback.
- Surface: `src/keystone_agents/cli.py`, `src/keystone_agents/orchestrator/preflight_context.py`, `tests/test_cli.py`
- Issue: Child env handoff now uses `compact_orchestrator_preflight_payload()`, but `_print_ask_preflight_blocked()` still includes `orchestrator_preflight.model_dump(mode="json")` and `route_result.model_dump(mode="json")` directly in the blocked JSON payload. If a blocked request arrives with Slack-derived workflow state, that response still contains `workflow_state_summary`, recent Slack thread summaries, and prior feedback text.
- Impact: The exact requests most likely to be sensitive or refused can still echo raw Orchestrator state into CLI/Slack bridge JSON output. That partially reopens the information-transmission issue that the compact child handoff fixed.
- Fix direction: Use `_orchestrator_preflight_payload(orchestrator_preflight)` and a compact/sanitized route-result payload in `_print_ask_preflight_blocked()`. Add a regression with Slack workflow state proving blocked JSON output omits `workflow_state_summary`, recent thread summaries, and prior feedback text.

### P2 - Sibling Slack bridge timeouts still do not isolate descendant KBA processes

- Found: 2026-05-25 12:00 EDT
- Fixed: 2026-05-25 12:03 EDT
- Status: fixed locally in sibling `keystone-slack`. `_run_command()` now
  starts KBA children in a new session, kills the process group on timeout, and
  returns a structured `CompletedProcess` with return code `124`.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`
- Issue: The sibling Slack bridge `_run_command()` still starts KBA child commands with plain `subprocess.run(..., timeout=...)` for non-streaming calls and plain `subprocess.Popen(...)` for streaming calls. The non-streaming path does not catch `subprocess.TimeoutExpired`, so a timed-out command can escape the bridge without a structured `BusinessAgentsResult`. The streaming path catches the timeout but only calls `process.kill()` on the direct child and does not start a new process session or kill a process group.
- Impact: A timed-out canonical ask, WorkItem advance, weekly workflow, or other KBA child command launched from Slack can still leave descendant model/search/provider processes running on the local Mac, or fail without the bridge's normal failure text/run-record update path. This partially preserves the local child-process risk even though KBA's own CLI/automation wrappers now use process-group cleanup.
- Fix direction: Move the sibling bridge onto the same isolated child-process behavior: start KBA child commands in a new process group/session, kill the full group on timeout, and convert timeout failures into structured bridge results/run-record failures. Add sibling tests for both non-streaming and streaming timeout paths, including a descendant-cleanup assertion or a monkeypatched process-group kill assertion.

### P2 - Live manual planner can reroute explicit named-agent requests

- Found: 2026-05-25 12:02 EDT
- Fixed: 2026-05-25 12:05 EDT
- Status: fixed locally. `merge_manual_request_plan()` now preserves explicit
  non-orchestrator named-agent requests and records an ignored-override warning;
  tests cover Business Research, Opportunity Scout, Gmail Triage, Outreach
  Composer, and Chief of Staff.
- Surface: `src/keystone_agents/manual_request.py`, `src/keystone_agents/agents/manual_request_planner.py`, `src/keystone_agents/cli.py`, sibling Slack canonical ask path
- Issue: Heuristic manual planning respects explicit named-agent mentions, but `merge_manual_request_plan()` does not preserve `base.target_agent` when `base.requested_agent` is an explicit specialist. A live manual-planner candidate can therefore change `requested_agent=business_research_analyst` from `target_agent=business_research_analyst` to `target_agent=opportunity_scout` with no warning, except for the two currently protected intents (`opportunity_to_outreach_loop` and `browser_diagnostics`).
- Evidence: A direct merge check with `infer_manual_request_plan("research NeuroFlow recent partnerships...", requested_agent="business research analyst")` plus an LLM-style `ManualRequestPlan(target_agent="opportunity_scout")` produced `merged.requested_agent == "business_research_analyst"` and `merged.target_agent == "opportunity_scout"` with `planner_warnings == []`.
- Impact: Explicit Slack/CLI named-agent calls can silently execute a different specialist path when live manual planning is enabled, changing retrieval behavior, context-pack shape, output format, and operator expectations. This is especially risky while the Slack bridge now routes general requests through canonical ask and relies on KBA to preserve named-agent semantics.
- Fix direction: In `merge_manual_request_plan()`, treat explicit non-orchestrator `base.requested_agent` as authoritative unless the route change is an explicitly allowed workflow expansion with matching safety gates. Add regression coverage where a bad LLM candidate attempts to reroute explicit Business Research, Opportunity Scout, Gmail Triage, Outreach Composer, and Chief of Staff requests.

### P1 - Quoted wrong tax output can still trigger the tax lane

- Found: 2026-05-25 12:01 EDT
- Fixed: 2026-05-25 12:05 EDT
- Status: fixed locally. Chief of Staff now treats wrong/unrelated/repeated
  response diagnosis requests as diagnostic asks before checking finance/tax
  shortcuts, and Orchestrator review rejects stale tax answers for those
  diagnostic request shapes.
- Surface: `src/keystone_agents/agents/chief_of_staff.py`, `src/keystone_agents/agents/orchestrator.py`
- Issue: Requests such as "same response: 2026 tax payments..." or "the request and response are completely unrelated" can quote the stale tax output while asking why it happened. The Chief of Staff finance/tax shortcut can interpret the quoted bad response as the user's actual request. The Orchestrator review path can also pass stale tax output if it overlaps with the quoted text rather than the diagnostic intent.
- Evidence: Focused regression tests cover the exact stale tax-payment response shape, the "request and response are completely unrelated" follow-up shape, and "why @KNI keeps posting the same tax payment answer" wording.
- Impact: Follow-up debugging asks can repeat the same unrelated tax answer instead of diagnosing stale context, routing, planner, or response-alignment failure. This makes operator correction loops worse because the correction text itself becomes route evidence for the wrong deterministic lane.
- Fix direction: Detect wrong-response diagnostic phrasing before finance/tax shortcuts and require Orchestrator output review to answer the diagnostic intent, not just overlap with quoted stale output.

### P2 - KBA run-agent modal fallback can still exceed Slack metadata limits

- Found: 2026-05-25 12:04 EDT
- Fixed: 2026-05-25 12:06 EDT
- Status: fixed locally. `build_run_agent_modal()` now uses a hard-size
  private-metadata guard with a pointer-only final fallback, preserving the
  full Slack context in the context file while keeping modal metadata valid and
  under the safe size threshold.
- Surface: `src/keystone_agents/slack_actions.py`, selected-message Run Keystone Agent modal path
- Issue: `build_run_agent_modal()` now falls back to a smaller embedded selected context when the first `private_metadata` JSON exceeds 2800 characters, but it does not re-check the final encoded size or switch to a guaranteed pointer-only payload. The fallback also keeps duplicated top-level fields (`channel_id`, `selected_message_ts`, `thread_ts`) plus selected-context scalar fields that can each be up to 500 characters.
- Evidence: Constructing a `SlackSelectedMessageContext` with long scalar fields allowed by `_clean_scalar()` and calling `build_run_agent_modal(..., context_file_path="Z"*500)` produced a `private_metadata` string of 7495 characters after fallback, with no truncation guard or error.
- Impact: Some selected-message Slack actions can still fail to open the modal or submit an invalid over-limit view even though the full context file exists. This reintroduces brittle context transfer for unusual channel names/permalinks/test payloads and makes the KBA-side modal path less robust than the sibling bridge path that now emits minimal pointer metadata.
- Fix direction: After fallback, enforce a hard-size guard. Prefer an always-small pointer payload containing schema, context file path, selected message/channel ids, and a warning marker; omit duplicated top-level context and long scalar fields. Add a regression that long selected-context scalars still produce valid `private_metadata` under Slack's 3000-character limit.

### P2 - Sibling Slack bridge labels blocked run-agent preflights as started/completed

- Found: 2026-05-25 12:06 EDT
- Fixed: 2026-05-25 12:08 EDT
- Status: fixed locally in sibling `keystone-slack`. Selected-message
  `stage="work_item"` responses with `status="blocked"` or no WorkItem plus
  block metadata now render as blocked, set run metadata status to `blocked`,
  and avoid "started/completed" success language.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`, `src/keystone_agents/slack_actions.py`
- Issue: KBA selected-message modal submissions correctly return `stage="work_item"`, `status="blocked"`, `work_item=None`, and `send_enabled=False` when Orchestrator preflight blocks execution. The sibling Slack bridge handles every `stage == "work_item"` payload through the success renderer: it defaults the text to "Keystone WorkItem started from selected Slack context.", sets metadata status to `completed` or `completed_with_warnings`, and returns title `Business Agents WorkItem Started`.
- Evidence: `tests/test_slack_agent_actions.py::test_modal_submission_blocks_send_request_before_work_item` verifies the KBA result is blocked with no WorkItem. The sibling bridge code at the `stage == "work_item"` branch does not special-case `status == "blocked"` or `work_item_id == ""`; it still appends route/status only after the "started" line and creates a "Keystone WorkItem Started" modal update.
- Impact: A blocked send or safety refusal from a Slack selected-message run can look like a successfully started/completed WorkItem in the Slack modal/source-message update. That weakens operator trust in the Orchestrator preflight gate and can mask that no WorkItem was created or advanced.
- Fix direction: Add a blocked branch in the sibling `stage == "work_item"` renderer. Use blocked/refused copy from `block_reason`, `block_kind`, or compact `output`, set metadata status to `blocked`, avoid "started/completed" titles, and add a sibling regression where a blocked KBA payload renders as blocked without source-message success language.

### P2 - Selected-message final results update the modal but not the source Slack thread

- Found: 2026-05-25 12:10 EDT
- Fixed: 2026-05-25 12:10 EDT
- Status: fixed locally in sibling `keystone-slack`. Selected-message WorkItem
  results now build a `slack_message_update` from KBA Slack run provenance for
  both completed and blocked outcomes, so the source Slack thread receives the
  final rendered result without raw internal JSON.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`, sibling `keystone-slack/kni_integrations/slack_socket_mode.py`
- Issue: The run-agent view-submission background path updated the modal with progress and a final view, then called `_update_business_agents_source_message(result)`. However, the selected-message `stage == "work_item"` renderer did not attach `metadata["slack_message_update"]`, so the real source-message update function returned without posting anything. The existing modal-feedback test patched `_update_business_agents_source_message`, which proved the call happened but not that a real update payload existed.
- Evidence: Sibling tests now assert completed and blocked selected-message results include `slack_message_update` with source channel/message timestamp from `slack_run_provenance`, final result text, route/status context, and no raw JSON/provenance payload.
- Impact: Slack users could see progress in the modal but not receive the promised final result in the selected message thread. That weakened the Orchestrator feedback loop because the durable Slack surface did not show the final specialist result being reviewed and returned.
- Fix direction: Build a source-thread result update from validated KBA run provenance whenever selected-message WorkItem results complete or block. Keep the modal update and source-thread update separate, and keep raw provenance/internal JSON in metadata only.

### P2 - KBA revise-draft modal still truncates JSON private metadata

- Found: 2026-05-25 12:11 EDT
- Fixed: 2026-05-25 12:17 EDT
- Status: fixed locally. The KBA-local Slack interaction handler now emits a
  compact parseable revise-draft modal payload and never truncates serialized
  JSON.
- Surface: `src/keystone_agents/slack_interactions.py`, `src/keystone_agents/slack_action_contract.py`
- Issue: `_build_revision_modal()` serializes the full `BusinessAgentActionPayload`
  and then truncates the JSON string for Slack. If an action payload contains
  expanded metadata from an older card, future richer action context, or a
  bridge mismatch, the modal can open with invalid `private_metadata`; the
  subsequent view submission calls `parse_business_agent_action_value()` on that
  invalid JSON and fails before queueing the requested revision.
- Evidence: Constructing a `BusinessAgentActionPayload(intent="revise_draft",
  approval_id="approval_1", metadata={"approval_title": "Email draft",
  "source_context": "x" * 6000})` and passing it to `_build_revision_modal()`
  produced a 2900-character `private_metadata` string that raised
  `JSONDecodeError` on `json.loads()`.
- Impact: A valid-looking "Revise draft" modal can become a dead end for large
  or forward-compatible action payloads. That breaks natural-language revision
  feedback transmission from Slack back into the WorkItem manager loop.
- Fix direction: Mirror the sibling bridge fix in the KBA handler: emit a
  compact parseable payload with only schema, intent, approval/work item ids,
  Slack source ids, and bounded display metadata; never truncate serialized JSON.
  Add a regression where oversized metadata still yields valid metadata under
  Slack's 3000-character limit and the view submission queues the revision.

### P2 - Sibling run-agent modal minimal fallback can still exceed Slack metadata limits

- Found: 2026-05-25 12:13 EDT
- Fixed: 2026-05-25 12:17 EDT
- Status: fixed locally in sibling `keystone-slack`. Run-agent modal metadata
  now checks the minimal fallback size and falls back to a true pointer-only
  payload under Slack's metadata limit.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`
- Issue: `_business_agents_run_agent_modal_metadata()` first tries a bounded
  embedded selected-context payload and, when that is too large, switches to
  `_modal_embedded_selected_context(..., minimal=True)`. The minimal branch is
  returned directly. With long but locally allowed Slack scalar fields, warnings,
  and a normal pointer-style context path, the returned JSON can still exceed
  Slack's 3000-character `private_metadata` limit.
- Evidence: Calling `_business_agents_run_agent_modal_metadata()` with max-sized
  selected-context scalars, three warnings, and a 100-character
  `context_file_path` returned valid JSON of 3154 characters. Longer context
  paths increased the payload further.
- Impact: Selected-message run-agent shortcuts can fail to open the modal for
  high-context Slack messages even though the context file exists. This breaks
  natural-language request intake before the KBA child process or Orchestrator
  preflight can run.
- Fix direction: Add a hard final size check and a true pointer-only fallback
  that keeps only schema, context file path, source channel/message/thread ids,
  and a compact warning marker. Add a sibling regression that worst-case bounded
  Slack fields still produce valid `private_metadata` under Slack's limit.

### P1 - Orchestrator test-pack workflows start in clarification or outreach lanes

- Found: 2026-05-25 12:15 EDT
- Fixed: 2026-05-25 12:17 EDT
- Status: fixed locally. The manual planner now recognizes orchestrator-owned
  multi-agent workflow asks and starts safe execution with Business Research
  Analyst or Opportunity Scout instead of immediate Outreach Composer or
  unsupported clarification. The manager loop also stops before repeating a
  specialist route that already ran in the same loop.
- Surface: `src/keystone_agents/manual_request.py`, `src/keystone_agents/workflow_runner.py`, `tests/test_manual_request_plan.py`, `tests/test_workflow_runner.py`
- Issue: The OR-1 to OR-5 acceptance prompts in `src/keystone_agents/test_pack_specs.py` are natural-language orchestrator workflows, but local planning treated most prompts mentioning outreach/email as direct `outreach_composer` requests or unsupported `clarification`. When saved WorkItems did enter the manager loop, broad Opportunity Scout to Business Research handoffs could bounce back to Opportunity Scout until `max_steps` instead of stopping after useful specialist communication.
- Evidence: A dry-run audit of OR-1 to OR-5 initially produced `clarification` blockers for OR-1/OR-4/OR-5 and immediate `outreach_composer` blockers for OR-2/OR-3. After the fix, `test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop` verifies all OR-1 to OR-5 prompts enter saved manager-loop execution with no blockers, expected first specialist routes, no immediate outreach lane, and no approved external-action gates.
- Impact: Broad Slack asks such as "run a workflow", "decide which agents should be used", and "safe parts first" could fail before any specialist reasoning or attempt drafting before research context existed. That contradicted the consolidated Orchestrator/planner goal and made diverse requests worse than deterministic named-agent calls.
- Fix direction: Treat orchestrator-owned workflow phrasing as a planning signal, not a new deterministic intent enum. Start with the safe evidence-producing specialist, preserve draft/read-only side-effect policy, and stop the manager loop before repeating a specialist already used in the same run.

### P1 - Live Outreach Composer handoff loses WorkItem Orchestrator context

- Found: 2026-05-25 12:22 EDT
- Fixed: 2026-05-25 12:22 EDT
- Status: fixed locally. The WorkItem live Outreach Composer helper now
  receives the current WorkItem and includes the raw request, WorkItem id,
  side-effect policy, manual plan/preflight memo, and Slack context identifiers
  in the specialist approved context.
- Surface: `src/keystone_agents/workflow_runner.py`, `tests/test_workflow_runner.py`
- Issue: `_compose_outreach_draft_for_work_item()` referenced `work_item` while
  building the live SDK retrieve/normalize context, but the helper did not
  receive `work_item` as an argument. Live Outreach Composer drafting therefore
  hit `NameError`, fell back to deterministic draft-only output, and never
  passed the Orchestrator memo or raw request into the live specialist.
- Evidence: The new regression
  `test_live_outreach_handoff_includes_orchestrator_memo_and_raw_request`
  exercises an approved saved WorkItem with `live_sdk=True` and asserts the
  Outreach Composer SDK handoff includes `Orchestrator memo for this specialist
  WorkItem run`, the raw user request, WorkItem id, and no-send policy.
- Impact: The multi-agent path could appear to succeed while silently skipping
  the live specialist LLM exactly at the downstream drafting step. That weakens
  the goal that specialists read the user request plus Orchestrator/planner
  context and makes live cross-agent behavior diverge from dry-run artifacts.
- Fix direction: Thread `work_item` through the live Outreach Composer helper
  and keep approved-context validation and no-send gates authoritative.

### P1 - Direct Opportunity Scout search planner misses Orchestrator raw request

- Found: 2026-05-25 12:25 EDT
- Fixed: 2026-05-25 12:25 EDT
- Status: fixed locally. The compact Orchestrator preflight memo now includes
  the raw request, and the direct Opportunity Scout CLI passes that memo into
  `resolve_opportunity_search_plan()` when `--live-search-plan` is enabled.
- Surface: `src/keystone_agents/orchestrator/preflight_context.py`,
  `scripts/run_opportunity_scout.py`, `tests/test_orchestrator_preflight_context.py`
- Issue: The parent Orchestrator preflight payload included `request_text`, but
  `_preflight_memo_payload()` and `orchestrator_preflight_context_text()` did
  not expose it in the specialist-readable memo. The direct
  `scripts/run_opportunity_scout.py --live-search-plan` path also called
  `resolve_opportunity_search_plan()` without `planner_context`, so the
  retrieval planner did not see the Orchestrator's interpretation for explicit
  named-agent Slack/CLI calls.
- Evidence: The new regression
  `test_opportunity_scout_live_search_planner_receives_orchestrator_memo`
  installs parent Orchestrator env, runs the direct Opportunity Scout live-search
  path with a fake retrieval provider, and asserts the planner context includes
  the raw request and selected agent before the search plan is handed to
  retrieval.
- Impact: Explicit `@KNI opportunity scout ...` live runs could execute a
  specialist planner close to retrieval without the raw user request or
  consolidated Orchestrator/planner memo, weakening the intended architecture
  and making direct named-agent calls less context-aware than WorkItem manager
  runs.
- Fix direction: Keep `request_text` in compact child handoff and specialist
  memos, and pass the memo into planner calls before deterministic retrieval.

### P2 - Sibling modal submissions bypass interactive dedupe

- Found: 2026-05-25 12:24 EDT
- Fixed: 2026-05-25 12:25 EDT
- Status: fixed locally. The sibling `keystone-slack` Socket Mode dedupe key
  now handles `view_submission` payloads using stable modal/team/user
  identifiers plus bounded hashes of private metadata and submitted state.
- Surface: sibling `keystone-slack/kni_integrations/slack_socket_mode.py`
- Issue: `_claim_interactive_payload()` calls `_interactive_dedupe_key()`, but
  `_interactive_dedupe_key()` returns `""` when the payload has no `actions`.
  Slack modal submissions use `type=view_submission` with `view.id`,
  `view.callback_id`, `view.hash`, and `view.private_metadata`, but no
  `actions` array. As a result, duplicate Socket Mode deliveries of the same
  run-agent or revision modal submission are always accepted.
- Evidence: Calling `process_socket_envelope()` twice with the same
  `keystone_run_agent_submit` view-submission payload and a patched
  `threading.Thread` started two `kni-business-agent-run-agent-submit`
  background threads. `_interactive_dedupe_key(payload["payload"])` returned
  an empty string for the same payload.
- Impact: A Slack retry or duplicate delivery can run the same selected-message
  natural-language request twice. For run-agent submissions that can create
  duplicate WorkItems, duplicate child KBA processes, duplicate modal updates,
  and duplicate source-thread final replies. For revision submissions it can
  enqueue repeated revision feedback against the same approval item.
- Fix direction: Extend `_interactive_dedupe_key()` to cover
  `view_submission` payloads using stable modal identifiers such as team id,
  user id, `view.id`, `view.callback_id`, `view.hash`, and a hash of bounded
  `private_metadata` or submitted state. Add a sibling regression proving a
  repeated run-agent view submission starts only one background thread and the
  duplicate ack is ignored.

### P2 - Sibling feedback callback failures can stop stderr draining and timeout child runs

- Found: 2026-05-25 12:26 EDT
- Fixed: 2026-05-25 12:26 EDT
- Status: fixed locally. The sibling `keystone-slack` bridge now isolates Slack
  progress callback failures from stderr pipe draining; it records the first
  callback failure, disables further callbacks for that child, and continues
  draining stderr until the child exits.
- Surface: sibling `keystone-slack/kni_integrations/business_agents_bridge.py`,
  sibling `keystone-slack/kni_integrations/slack_socket_mode.py`
- Issue: `_run_command(..., stderr_line_callback=...)` starts a stderr reader
  thread and calls `stderr_line_callback(line)` directly for every line. For
  selected-message run-agent submissions, that callback ultimately calls
  `_update_slack_modal()` through the Socket Mode feedback callback. If a
  transient Slack `views.update` failure, network error, rate limit, invalid
  view id, or modal hash conflict raises, the stderr reader thread exits. The
  child process can then block on a full stderr pipe and be killed by the
  bridge timeout even though the actual KBA work may be healthy.
- Evidence: Running sibling `_run_command()` against a child that writes many
  stderr lines returned `0` and drained about 15 MB of stderr when the callback
  was `lambda line: None`. With the same child and a callback that raises
  `RuntimeError("modal update failed")` on the first line, the stderr reader
  thread crashed and `_run_command()` returned `124` with
  `Business Agents command timed out after 2 seconds.`
- Impact: A Slack modal progress-update failure can convert a successful
  natural-language agent run into a timeout, kill the child process group, and
  leave the user with an error modal or no source-thread final response. This
  couples UI feedback reliability to child process liveness.
- Fix direction: Keep pipe draining independent of progress delivery. Catch and
  record callback exceptions inside `read_stderr()`, continue appending/draining
  stderr, and optionally disable further progress callbacks for that run after
  the first callback failure. Add a sibling regression where a raising feedback
  callback still drains stderr and returns the child exit code instead of 124.

### P1 - Gmail priority-grouping SDK path can omit the operator request

- Found: 2026-05-25 12:31 EDT
- Fixed: 2026-05-25 12:31 EDT
- Status: fixed locally. The Gmail priority-grouping SDK path now builds the
  model request from the raw operator request, the GT-1 priority-grouping task
  prompt, and any Orchestrator preflight memo.
- Surface: `scripts/run_gmail_triage.py`, `tests/test_cli_live_safety.py`
- Issue: Direct or child `run_gmail_triage.py --priority-grouping --request ...`
  runs could invoke the Gmail batch planner with only the fixed GT-1 prompt
  plus any preflight memo, leaving the operator's natural-language instruction
  out of the specialist model prompt when preflight context was absent or
  insufficient.
- Impact: The Gmail specialist could optimize for the generic test-pack task
  instead of the user's actual prioritization, grouping, or drafting goal. This
  weakens the Orchestrator-first architecture because the downstream LLM is not
  consistently close to the raw request.
- Fix direction: Preserve the raw request in the typed SDK input, audit payload,
  and Orchestrator review summary while keeping the deterministic GT-1 prompt as
  stable task context. Add a regression asserting the generated specialist
  prompt contains the operator request.

### P2 - KBA Slack feedback callback failures can abort WorkItem execution

- Found: 2026-05-25 12:32 EDT
- Fixed: 2026-05-25 12:33 EDT
- Status: fixed locally. The KBA Slack feedback collector now records feedback
  events durably, treats upstream delivery as best-effort, emits one
  `feedback_delivery_failed` event, disables further upstream forwarding for
  that run, and continues the Orchestrator/WorkItem manager loop.
- Surface: `src/keystone_agents/slack_actions.py`,
  `scripts/handle_slack_agent_action.py`, sibling Socket Mode feedback bridge
- Issue: `_build_feedback_collector()` appends the feedback event and then
  calls the upstream callback without isolation. A nonessential feedback sink
  failure raises out of `handle_run_agent_interaction()` during the first
  `orchestrator_preflight` event, before the WorkItem manager loop can run or
  return a structured Slack result.
- Evidence: A local repro that passed a `feedback_callback` raising
  `RuntimeError("feedback sink unavailable")` into
  `handle_run_agent_interaction()` printed
  `kba_feedback_callback_aborts RuntimeError feedback sink unavailable`.
- Impact: The sibling bridge now protects stderr draining from modal-update
  callback failures, but the KBA child process can still fail its Slack action
  because progress/event transmission failed. That can leave the modal showing
  an error even though the underlying request could have completed.
- Fix direction: Treat progress callbacks as best-effort. Catch exceptions in
  `_build_feedback_collector()`, record one local feedback-callback diagnostic,
  disable further upstream callback calls for that run, and keep WorkItem
  execution and final structured result generation moving. Add a regression
  where a raising KBA feedback callback still returns a `SlackAgentActionResult`.

### P2 - Mapping typed inputs render as Python repr instead of structured JSON

- Found: 2026-05-25 12:35 EDT
- Fixed: 2026-05-25 12:35 EDT
- Status: fixed locally. Generic SDK prompt rendering now serializes mapping
  and sequence typed inputs as stable JSON when the input object does not expose
  a dedicated `to_prompt()` method.
- Surface: `src/keystone_agents/run.py`, `tests/test_sdk_execution.py`
- Issue: `prompt_from_typed_input()` fell back to `str(value)` for mapping
  inputs. Chief of Staff live SDK calls can pass a mapping that includes
  `request`, `manual_request_plan`, `orchestrator_preflight`, side-effect policy,
  and Slack/session context. Rendering that as Python repr is less parseable and
  less consistent than the typed prompt methods used by other specialist inputs.
- Impact: The raw request and Orchestrator/planner memo were present, but the
  model received them in a loose Python representation instead of a stable
  structured context. That weakens the consolidated planner/orchestrator goal
  for agents that use mapping-based typed input.
- Fix direction: Prefer explicit `to_prompt()` methods when present, otherwise
  render mappings and sequences as JSON with deterministic keys/default string
  coercion. Add a regression proving structured Chief-of-Staff-style context is
  JSON parseable and preserves request, manual plan, and Orchestrator preflight.

### P1 - Final response synthesis misses Orchestrator review details

- Found: 2026-05-25 12:36 EDT
- Fixed: 2026-05-25 12:36 EDT
- Status: fixed locally. Manager-loop reviews are now copied into compact
  WorkItem target metadata and passed into the final user-facing response
  synthesis input as `orchestrator_reviews`.
- Surface: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/response_synthesis.py`, `tests/test_workflow_runner.py`
- Issue: The bounded manager loop reviewed each specialist step and stored
  detailed review metadata in WorkItem events, but final live response synthesis
  only received short audit notes such as `Manager loop review step 1: pass`.
  The synthesizer did not receive observed gaps or recommended next steps.
- Impact: The Orchestrator could evaluate specialist output, but the final
  Slack-facing response layer was not consistently aware of that evaluation.
  This weakened the desired send/eval loop because a synthesized answer could
  smooth over a partial review instead of surfacing gaps or next actions.
- Fix direction: Keep WorkItems/events as the durable contract, but also attach
  the last few compact review summaries to WorkItem metadata and include them in
  `UserFacingResponseSynthesisInput`. Add a regression proving final synthesis
  sees review status, observed gaps, and recommended next step.

### P2 - Direct live child failures bypass structured/redacted JSON output

- Found: 2026-05-25 12:35 EDT
- Fixed: 2026-05-25 12:36 EDT
- Status: fixed locally. Direct live child non-zero exits now return a
  structured `status="failed"` payload with route, child return code,
  no-send fields, compact Orchestrator preflight, and redacted stdout/stderr
  excerpts.
- Surface: `src/keystone_agents/cli.py`, `src/keystone_agents/child_process.py`
- Issue: `_run_ask_script_live()` now wraps direct child agent calls in an
  isolated process group and returns structured timeout payloads, but ordinary
  non-zero child exits still raise `SystemExit(completed.stderr or
  completed.stdout)`. That bypasses the JSON response contract and does not
  redact stderr/stdout before surfacing the failure.
- Evidence: A local child command that exited `7` with stderr
  `failed token=[SECRET_LIKE_TOKEN]` caused
  `_run_ask_script_live(..., json_output=True, manual_plan=None)` to raise
  `SystemExit` with the raw secret-shaped stderr text instead of returning a
  structured failed JSON payload.
- Impact: Direct named-agent `@KNI` live child failures can leak secret-shaped
  provider text, break Slack/bridge JSON parsing, and give the operator a raw
  process failure instead of a bounded agent error with route, status, child
  return code, timeout status, and no-send safety fields.
- Fix direction: Mirror the timeout path for non-zero child exits: redact
  stdout/stderr excerpts, return a structured `status="failed"` payload when
  `--json` is active, preserve `send_enabled=false`, include child return code
  and selected agent, and add a regression for a non-zero child stderr payload
  containing a token-like string.

### P2 - Malformed live child output bypasses structured/redacted JSON output

- Found: 2026-05-25 12:38 EDT
- Fixed: 2026-05-25 12:38 EDT
- Status: fixed locally. Direct live child malformed stdout now returns a
  structured `status="failed"` payload with route, child return code,
  parse-error type, no-send fields, compact Orchestrator preflight, and
  redacted stdout/stderr excerpts.
- Surface: `src/keystone_agents/cli.py`, `src/keystone_agents/child_process.py`
- Issue: `_run_ask_script_live()` now returns structured payloads for timeout
  and non-zero child exits, but JSON parse failures still raise
  `SystemExit(f"Agent script returned non-JSON output: {completed.stdout[:500]}")`.
  That bypasses the JSON response contract and does not redact stdout before
  surfacing the failure.
- Evidence: A local child command that exited `0` with stdout
  `not json token=[SECRET_LIKE_TOKEN]` caused
  `_run_ask_script_live(..., json_output=True, manual_plan=None)` to raise
  `SystemExit` with the raw secret-shaped stdout text instead of returning a
  structured failed JSON payload.
- Impact: Direct named-agent `@KNI` live child parse failures can leak
  secret-shaped provider text, break Slack/bridge JSON parsing, and give the
  operator a raw process failure instead of a bounded agent error with route,
  status, parse error type, and no-send safety fields.
- Fix direction: Mirror the timeout/non-zero failure path for malformed child
  stdout: redact the stdout excerpt, return a structured `status="failed"`
  payload when `--json` is active, preserve `send_enabled=false`, include the
  selected agent and parse error type, and add a regression for a successful
  child process that emits non-JSON stdout containing a token-like string.

### P2 - Orchestrator registry card understates bridge/control-plane coverage

- Found: 2026-05-25 12:40 EDT
- Fixed: 2026-05-25 12:40 EDT
- Status: fixed locally. The Orchestrator registry card now describes raw-request
  first planning, compact preflight handoff, specialist output review, and the
  manager-loop review to final-synthesis path. Its validation paths now include
  preflight context, WorkItem manager-loop, Slack contract, and Slack action
  coverage.
- Surface: `src/keystone_agents/agent_registry.py`, `tests/test_agent_registry.py`
- Issue: The Orchestrator runtime now owns the first-pass request interpretation,
  compact child handoff, Slack selected-message preflight, manager-loop review,
  and final response synthesis feedback path. The canonical registry card still
  described only generic routing/handoff behavior and listed only routing plus
  old handoff contract validation.
- Impact: Agent cards, CLI inspection, extension checks, and future architecture
  review could miss the actual control-plane contract and regress toward
  deterministic lanes or ad hoc specialist calls because the registry did not
  name the bridge obligations it should preserve.
- Fix direction: Keep registry metadata aligned with runtime architecture. Add
  validation paths for preflight, manager loop, Slack action contract, and Slack
  action handling, plus a regression that asserts the Orchestrator card declares
  those control-plane responsibilities.

### P2 - Improvement matrix still treated prior wrong-tax Slack shape as uncovered

- Found: 2026-05-25 12:41 EDT
- Fixed: 2026-05-25 12:41 EDT
- Status: fixed locally. The exact `should we run it again? same response --
  2026 tax payments...` follow-up shape is now part of the Chief of Staff and
  Orchestrator wrong-lane regressions, and the improvement matrix references
  the executable coverage instead of saying the rows are still missing.
- Surface: `docs/AGENT_IMPROVEMENT_TEST_PACK.md`, `tests/test_chief_of_staff.py`,
  `tests/test_orchestrator.py`
- Issue: The improvement matrix still said prior Slack regression rows should be
  added before treating Orchestrator/Planner complete, even though adjacent
  wrong-tax-output regressions had been implemented. One exact user follow-up
  shape, `should we run it again?`, was still only documented as representative
  prose rather than an executable parameter.
- Impact: Architecture review could either understate current evidence or miss
  the exact repeat-run correction shape that triggered this work. That leaves a
  path for stale tax-output context to regress in correction loops.
- Fix direction: Add the exact follow-up wording to the deterministic Chief of
  Staff shortcut guard and Orchestrator output-review wrong-lane test, then
  update the improvement matrix to cite the executable tests.

### P2 - Manager-loop final synthesis drops configured SDK session

- Found: 2026-05-25 12:41 EDT
- Fixed: 2026-05-25 12:42 EDT
- Status: fixed locally. `advance_work_item_manager_loop()` now resolves and
  builds the configured SDK session for the final WorkItem before user-facing
  response synthesis, preserving explicit session IDs and DB paths.
- Surface: `src/keystone_agents/workflow_runner.py`,
  `tests/test_workflow_runner.py`
- Issue: `advance_work_item_manager_loop()` defers user-facing response
  synthesis until the bounded manager loop is done, but it calls
  `_maybe_synthesize_user_facing_response(..., sdk_session=None)`. The one-step
  runner builds a session from `_sdk_session_spec_for_work_item()` when
  `request.live_sdk` is enabled, but the manager-loop final synthesis does not
  reuse or rebuild that session.
- Evidence: A local repro called `advance_work_item_manager_loop()` with
  `live_sdk=True`, `sdk_session_enabled=True`, an explicit
  `sdk_session_id="operator-thread-123"`, and a session DB path. A monkeypatched
  `_maybe_synthesize_user_facing_response()` observed
  `sdk_session_is_none=True` for the final synthesis call even though the
  request carried the explicit session configuration.
- Impact: Slack selected-message runs and CLI WorkItem manager loops can lose
  SDK conversation continuity at the final answer layer. That weakens the
  planner/orchestrator goal because specialist steps may use the configured
  session while the final synthesized response is generated outside the same
  durable session, reducing trace continuity and increasing repeated context
  reconstruction.
- Fix direction: Resolve/build a final synthesis SDK session in
  `advance_work_item_manager_loop()` using the final WorkItem and the original
  request, then pass it to `_maybe_synthesize_user_facing_response()`. Add a
  regression proving explicit `sdk_session_*` request settings reach the final
  manager-loop synthesis call.

### P3 - Sibling Slack modal renders feedback-delivery failures as generic progress

- Found: 2026-05-25 12:44 EDT
- Fixed: 2026-05-25 12:44 EDT
- Status: fixed locally in sibling `keystone-slack`. Socket Mode modal feedback
  now renders `feedback_delivery_failed` as an explicit warning that names the
  failed progress event and error type while saying the agent run is continuing.
- Surface: sibling `keystone-slack/kni_integrations/slack_socket_mode.py`,
  sibling `keystone-slack/tests/test_app_mentions.py`
- Issue: KBA can now emit `feedback_delivery_failed` when local feedback
  forwarding fails but the Orchestrator/WorkItem manager loop continues. The
  sibling Slack modal renderer had no event-specific text for that event, so it
  displayed the generic `Keystone agent progress: feedback_delivery_failed`
  message.
- Impact: Operators could see an opaque progress update instead of a clear
  degradation warning. That weakens real-time visibility into the
  Orchestrator/specialist run loop, especially when modal/source-thread feedback
  is degraded but the underlying run is still healthy.
- Fix direction: Add explicit sibling modal text for `feedback_delivery_failed`
  and a regression covering the event payload emitted by KBA.

### P2 - Apify profile-search-only runs still reduce confidence as unconfigured

- Found: 2026-05-25 12:44 EDT
- Fixed: 2026-05-25 12:45 EDT
- Status: fixed locally in sibling `keystone-slack`. LinkedIn opportunity
  search now treats Apify as configured when either the enrichment actor or the
  enabled profile-search actor is configured with an API token.
- Surface: sibling `keystone-slack`:
  `kni_integrations/opportunity_search.py`, `tests/test_opportunity_search.py`
- Issue: `resolve_opportunity_search_spec()` sets `apify_actor_configured` from
  `APIFY_API_TOKEN && APIFY_LINKEDIN_ACTOR_ID` only. The new optional
  `APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR_ID` path can discover LinkedIn profile
  leads without the older enrichment actor, but `score_candidate()` still adds
  `Apify actor not configured.` whenever `candidate.apify_payload` is absent and
  `spec.apify_actor_configured` is false.
- Evidence: A local repro configured `apify_api_token`, enabled
  `apify_linkedin_profile_search_enabled`, set
  `apify_linkedin_profile_search_actor_id`, and left `apify_linkedin_actor_id`
  unset. A fake Apify profile-search hit produced one lead and debug note
  `Apify profile search hits: 1`, but the lead still carried
  `reduced_confidence_reasons == ["Apify actor not configured."]`.
- Impact: Slack opportunity searches can tell the operator a live Apify path was
  both used and not configured in the same result. That weakens lead confidence
  scoring, natural-language explanation quality, and operator trust in the
  bridge's provider diagnostics.
- Fix direction: Treat the Apify provider as configured when either the
  enrichment actor or the profile-search actor is configured for the selected
  mode, or split the scoring reason into `Apify enrichment actor not configured`
  so profile-search-only leads are not penalized. Add a regression for
  profile-search-only configuration.

### P2 - Selected-message in-progress WorkItems are posted as completed

- Found: 2026-05-25 12:47 EDT
- Fixed: 2026-05-25 12:48 EDT
- Status: fixed locally.
- Surface: sibling `keystone-slack`:
  `kni_integrations/business_agents_bridge.py`, `tests/test_app_mentions.py`
- Issue: The run-agent selected-message bridge handles a successful
  `stage="work_item"` response by setting metadata `status` to `completed` when
  there are no warnings, regardless of the actual WorkItem status. The
  source-message renderer then labels every non-blocked update as
  `Keystone agent completed`, even when the child payload says
  `status="in_progress"` and the modal title is `Business Agents WorkItem
  Started`.
- Evidence: A local repro patched `_run_command()` to return a selected-message
  WorkItem payload with `status="in_progress"` and `work_item.status` also
  `in_progress`. `handle_business_agents_interactive_payload()` returned title
  `Business Agents WorkItem Started`, but `result.metadata["status"]` was
  `completed` and `slack_message_update.text` began `*Keystone agent
  completed*`, then included both `Status: \`in_progress\`` and
  `Status: \`completed\``.
- Impact: Slack threads can tell the operator that a manager-loop WorkItem is
  complete while it is only started/in progress. That weakens natural-language
  request processing and operator trust, and can cause follow-up actions such as
  `continue`, `run again`, or approval review to be taken against the wrong
  lifecycle assumption.
- Fix direction: Preserve the child WorkItem lifecycle status in bridge metadata
  and source-thread rendering. Use `started`/`in_progress` wording for active
  WorkItems, reserve `completed` for terminal completed/done payloads, and avoid
  appending duplicate contradictory status lines. Add a regression for a
  selected-message `status="in_progress"` WorkItem payload.
- Verification: `python3 -m unittest
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_streams_feedback_jsonl`.

### P3 - Apify health still reports profile-search-only config as actor missing

- Found: 2026-05-25 12:49 EDT
- Fixed: 2026-05-25 12:50 EDT
- Status: fixed locally.
- Surface: sibling `keystone-slack`:
  `kni_integrations/opportunity_search.py`, `tests/test_opportunity_search.py`
- Issue: The prior Apify scoring fix treats either the enrichment actor or the
  enabled profile-search actor as configured, but `ApifyClient.health_check()`
  still builds its status detail from `apify_linkedin_actor_id` only. With the
  new profile-search-only path configured, health diagnostics say the LinkedIn
  actor is missing even though a selected LinkedIn Apify path is usable.
- Evidence: A local health-check repro set `APIFY_API_TOKEN`, enabled
  `apify_linkedin_profile_search_enabled`, set
  `apify_linkedin_profile_search_actor_id`, and left `apify_linkedin_actor_id`
  unset. With a successful mocked `/users/me` response,
  `ApifyClient.health_check()` returned `ok=True` but detail
  `Authenticated as apify-user; LinkedIn actor actor missing.`
- Impact: Operator-facing health output can contradict actual provider behavior:
  profile search can run and produce leads while health still says the LinkedIn
  actor is missing. That weakens diagnostics for Slack opportunity searches and
  can send operators toward the wrong configuration fix.
- Fix direction: Reuse the same configured-provider predicate in health output,
  or report enrichment/profile-search actor states separately. Add a regression
  for profile-search-only Apify health details.
- Verification: `python3 -m unittest
  tests.test_opportunity_search.OpportunitySearchTests.test_apify_health_reports_profile_search_only_actor_configured
  tests.test_opportunity_search.OpportunitySearchTests.test_linkedin_service_uses_optional_apify_profile_search_actor`.
- Follow-up verification at 2026-05-25 13:15 EDT: sibling
  `keystone-slack/kni_integrations/opportunity_search.py` still reports
  `profile-search actor configured` when only
  `APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR_ID` is configured, and the two focused
  unittest cases above pass.

### P2 - Chief of Staff deterministic shortcuts can outrun the manual planner

- Found: 2026-05-25 12:50 EDT
- Fixed: 2026-05-25 12:50 EDT
- Status: fixed locally.
- Surface: `src/keystone_agents/agents/chief_of_staff.py`,
  `scripts/run_chief_of_staff.py`, `tests/test_chief_of_staff.py`
- Issue: The direct Chief of Staff script resolved the parent/manual request
  plan, but the deterministic `plan_chief_of_staff_request()` path did not
  consume that plan before selecting local shortcuts. Live SDK execution already
  used `manual_request_plan` to block unaligned finance shortcuts; dry-run and
  live fallback paths could still make phrase heuristics the first effective
  decision layer.
- Evidence: Code inspection showed `scripts/run_chief_of_staff.py` passed
  parent Orchestrator preflight/manual plan into JSON payloads, but called
  `plan_chief_of_staff_request()` without a planner argument. The deterministic
  function had no `manual_request_plan` parameter and allowed finance shortcuts
  from request text alone.
- Impact: This weakens the Orchestrator-first architecture for `@KNI chief of
  staff` dry-run and fallback calls. A diverse architecture/implementation ask
  containing tax-like text could still be evaluated by deterministic shortcut
  code before the planner interpretation was authoritative.
- Fix direction: Pass the parent/manual request plan into the deterministic CoS
  planner, add a planner audit note for all deterministic branches, and require
  planner finance context before allowing finance shortcuts.
- Verification: `.venv/bin/python -m pytest
  tests/test_chief_of_staff.py::test_chief_of_staff_deterministic_path_uses_planner_before_finance_shortcut
  tests/test_chief_of_staff.py::test_chief_of_staff_planner_blocks_unaligned_finance_shortcut
  tests/test_chief_of_staff.py::test_run_script_consumes_parent_orchestrator_preflight_env`.

### P2 - Business Research Orchestrator review uses target label instead of raw request

- Found: 2026-05-25 12:54 EDT
- Fixed: 2026-05-25 12:54 EDT
- Status: fixed locally.
- Surface: `scripts/run_company_research.py`,
  `tests/test_orchestrator_preflight_context.py`
- Issue: `run_company_research.py --orchestrator-review` built the Orchestrator
  output-review request summary from `args.company` or
  `args.company vs args.compare_company`. When the parent Orchestrator/manual
  planner selected a company target from a broader Slack/manual request, the
  reviewer evaluated the specialist output against the target label rather than
  the raw operator request.
- Evidence: Code inspection found all three company-research review paths
  (SDK synthesis, comparison, and profile) passed target labels to
  `build_cli_orchestrator_review()` even when `--request-text` or parent
  preflight `request_text` was available.
- Impact: Wrong-target or stale-context Business Research output could receive a
  misleading Orchestrator review because the evaluator never saw the diverse
  original request, such as a `state of KNI` or architecture review ask that
  happened to carry a stale company/tax target.
- Fix direction: Prefer raw `--request-text`, then parent Orchestrator preflight
  raw request, then manual-plan objective, and only fall back to target labels
  when no request context exists. Add a regression proving the review receives
  the raw request rather than the extracted target.
- Verification: `.venv/bin/python -m pytest
  tests/test_orchestrator_preflight_context.py::test_company_research_review_uses_raw_orchestrator_request_not_target_label`.

### P2 - Opportunity Scout Orchestrator review uses topic label instead of raw request

- Found: 2026-05-25 13:05 EDT
- Fixed: 2026-05-25 13:05 EDT
- Status: fixed locally.
- Surface: `scripts/run_opportunity_scout.py`,
  `tests/test_orchestrator_preflight_context.py`
- Issue: `run_opportunity_scout.py --orchestrator-review` built the
  Orchestrator output-review request summary from `args.topic` or a fixture
  label. Parent Orchestrator preflight and manual plans could carry a broader
  Slack/manual request, but the reviewer evaluated Scout output against only the
  extracted topic.
- Evidence: Code inspection found both SDK and fixture/live-search review paths
  passed topic labels to `build_cli_orchestrator_review()` even when parent
  preflight `request_text` was available.
- Impact: Scheduled automation context or repeated weekly-opportunity follow-up
  asks could be reviewed against a stale topic instead of the current operator
  request, weakening Orchestrator detection of stale or wrong-lane Scout output.
- Fix direction: Prefer parent Orchestrator preflight raw request, then
  preflight memo raw request, then manual-plan objective, and only fall back to
  the topic/fixture label when no request context exists. Add a regression for a
  weekly-opportunities/manual-rerun request.
- Verification: `.venv/bin/python -m pytest
  tests/test_orchestrator_preflight_context.py::test_opportunity_scout_review_uses_raw_orchestrator_request_not_topic_label
  tests/test_orchestrator_preflight_context.py::test_opportunity_scout_live_search_planner_receives_orchestrator_memo`.

## Verification Notes

- Follow-up Opportunity Scout Orchestrator review request-summary fix passed at
  2026-05-25 13:05 EDT: `.venv/bin/python -m pytest
  tests/test_orchestrator_preflight_context.py::test_opportunity_scout_review_uses_raw_orchestrator_request_not_topic_label
  tests/test_orchestrator_preflight_context.py::test_opportunity_scout_live_search_planner_receives_orchestrator_memo`.
- Follow-up Business Research Orchestrator review request-summary fix passed at
  2026-05-25 12:54 EDT: `.venv/bin/python -m pytest
  tests/test_orchestrator_preflight_context.py::test_company_research_review_uses_raw_orchestrator_request_not_target_label`.
- Follow-up Chief of Staff deterministic planner-consumption fix passed at
  2026-05-25 12:50 EDT: `.venv/bin/python -m pytest
  tests/test_chief_of_staff.py::test_chief_of_staff_deterministic_path_uses_planner_before_finance_shortcut
  tests/test_chief_of_staff.py::test_chief_of_staff_planner_blocks_unaligned_finance_shortcut
  tests/test_chief_of_staff.py::test_run_script_consumes_parent_orchestrator_preflight_env`.
- Follow-up sibling Apify profile-search-only health diagnostic fix passed at
  2026-05-25 12:50 EDT: `python3 -m unittest
  tests.test_opportunity_search.OpportunitySearchTests.test_apify_health_reports_profile_search_only_actor_configured
  tests.test_opportunity_search.OpportunitySearchTests.test_linkedin_service_uses_optional_apify_profile_search_actor`.
- Follow-up selected-message WorkItem lifecycle rendering fix passed at
  2026-05-25 12:48 EDT: `python3 -m unittest
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_streams_feedback_jsonl`.
- Follow-up sibling Apify profile-search-only configuration fix passed at
  2026-05-25 12:45 EDT: `python3 -m unittest
  tests.test_opportunity_search.OpportunitySearchTests.test_linkedin_service_uses_optional_apify_profile_search_actor`.
- Follow-up sibling Slack feedback-delivery warning rendering fix passed at
  2026-05-25 12:44 EDT: `python3 -m unittest
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_feedback_delivery_failure_has_explicit_modal_text`.
- Follow-up manager-loop final synthesis SDK session fix passed at
  2026-05-25 12:42 EDT: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_manager_loop_final_synthesis_uses_configured_sdk_session
  tests/test_workflow_runner.py::test_manager_loop_defers_live_synthesis_until_final_step`
  and `.venv/bin/python -m py_compile src/keystone_agents/workflow_runner.py`.
- Follow-up prior wrong-tax Slack regression coverage fix passed at
  2026-05-25 12:41 EDT: `.venv/bin/python -m pytest
  tests/test_chief_of_staff.py::test_wrong_response_diagnostics_do_not_trigger_tax_payment_shortcut
  tests/test_orchestrator.py::test_orchestrator_review_flags_wrong_response_diagnostic_wrong_lane
  tests/test_orchestrator.py::test_orchestrator_review_allows_wrong_response_diagnostic_answer`
  (9 passed), and `.venv/bin/python -m py_compile
  src/keystone_agents/agents/chief_of_staff.py
  src/keystone_agents/agents/orchestrator.py`. The fix broadened the diagnostic
  marker from `run again` to also cover `run it again`.
- Follow-up Orchestrator registry bridge metadata fix passed at
  2026-05-25 12:40 EDT: `.venv/bin/python -m pytest
  tests/test_agent_registry.py::test_orchestrator_registry_declares_control_plane_bridge_coverage
  tests/test_agent_registry.py::test_registered_agents_have_builders_schemas_prompts_and_validation`
  and `.venv/bin/python -m py_compile src/keystone_agents/agent_registry.py`.
- Follow-up malformed live child output redaction fix passed at
  2026-05-25 12:38 EDT: `.venv/bin/python -m pytest
  tests/test_cli.py::test_cli_ask_live_child_malformed_json_returns_redacted_structured_payload
  tests/test_cli.py::test_cli_ask_live_child_failure_returns_redacted_structured_payload`
  and `.venv/bin/python -m py_compile src/keystone_agents/cli.py`.
- Follow-up direct live child failure redaction fix passed at
  2026-05-25 12:36 EDT: `.venv/bin/python -m pytest
  tests/test_cli.py::test_cli_ask_live_child_failure_returns_redacted_structured_payload
  tests/test_cli.py::test_cli_ask_live_child_timeout_returns_structured_payload`
  and `.venv/bin/python -m py_compile src/keystone_agents/cli.py`.
- Follow-up final synthesis review-context fix passed at
  2026-05-25 12:36 EDT: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_user_response_synthesis_receives_orchestrator_review_feedback
  tests/test_workflow_runner.py::test_manager_loop_records_orchestrator_review_feedback_for_agent_run`
  and `.venv/bin/python -m py_compile
  src/keystone_agents/workflow_runner.py src/keystone_agents/response_synthesis.py`.
- Follow-up mapping typed-input prompt rendering fix passed at
  2026-05-25 12:35 EDT: `.venv/bin/python -m pytest
  tests/test_sdk_execution.py::test_prompt_from_typed_input_renders_structured_context_as_json`
  and `.venv/bin/python -m py_compile src/keystone_agents/run.py`.
- Follow-up KBA real-time feedback callback isolation fix passed at
  2026-05-25 12:33 EDT: `.venv/bin/python -m pytest
  tests/test_slack_agent_actions.py::test_realtime_feedback_callback_failure_is_nonfatal
  tests/test_slack_agent_actions.py::test_modal_submission_exposes_realtime_orchestrator_feedback
  tests/test_slack_agent_actions.py::test_realtime_feedback_events_do_not_authorize_side_effects`
  and `.venv/bin/python -m py_compile src/keystone_agents/slack_actions.py`.
- Follow-up Gmail priority-grouping request-context fix passed at
  2026-05-25 12:31 EDT: `.venv/bin/python -m pytest
  tests/test_cli_live_safety.py::test_gmail_gt1_priority_grouping_cli_uses_llm_batch_pipeline`
  and `.venv/bin/python -m py_compile scripts/run_gmail_triage.py`.
- Follow-up verification at 2026-05-25 12:28 EDT found no open backlog entries.
  The sibling modal-submission dedupe repro now starts one run-agent background
  thread and returns no payload for the duplicate delivery. The sibling
  feedback-callback repro now returns child exit code 0, drains stderr, records
  `Business Agents feedback callback failed`, and does not time out.
- Follow-up inspection at 2026-05-25 12:26 EDT found sibling feedback callback
  exceptions can kill the stderr reader thread. A high-stderr child returned 0
  with a no-op callback, but returned 124 after the callback raised once because
  stderr was no longer drained.
- Follow-up sibling feedback callback isolation fix passed at
  2026-05-25 12:26 EDT: `python3 -m unittest
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_streaming_feedback_callback_failure_still_drains_stderr
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_streaming_run_command_timeout_kills_group
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_streams_feedback_jsonl
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_view_submission_is_deduped`
  (4 passed), and `python3 -m py_compile
  kni_integrations/business_agents_bridge.py kni_integrations/slack_socket_mode.py`.
- Follow-up direct Opportunity Scout planner handoff fix passed at
  2026-05-25 12:25 EDT: `.venv/bin/python -m pytest
  tests/test_orchestrator_preflight_context.py::test_opportunity_scout_cli_preserves_parent_orchestrator_preflight
  tests/test_orchestrator_preflight_context.py::test_opportunity_scout_live_search_planner_receives_orchestrator_memo`
  (2 passed), and `.venv/bin/python -m py_compile
  src/keystone_agents/orchestrator/preflight_context.py
  scripts/run_opportunity_scout.py`.
- Follow-up inspection at 2026-05-25 12:24 EDT found duplicate sibling modal
  submissions still bypass dedupe: the same `keystone_run_agent_submit`
  `view_submission` payload started two background run-agent threads because
  `_interactive_dedupe_key()` returned `""`.
- Follow-up sibling modal-submission dedupe fix passed at 2026-05-25 12:25 EDT:
  `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_view_submission_is_deduped tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_acks_and_starts_background_thread tests.test_app_mentions.SlackAppMentionTests.test_business_agents_interactive_buttons_are_deduped`
  (3 passed).
- Follow-up live Outreach Composer handoff fix passed at 2026-05-25 12:22 EDT:
  `.venv/bin/python -m pytest tests/test_workflow_runner.py::test_live_outreach_handoff_includes_orchestrator_memo_and_raw_request tests/test_workflow_runner.py::test_approved_context_continue_creates_draft_only_outreach_artifact tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop`
  (3 passed), `.venv/bin/python -m py_compile src/keystone_agents/workflow_runner.py`,
  and the full WorkItem runner suite `.venv/bin/python -m pytest tests/test_workflow_runner.py`
  (32 passed).
- Follow-up verification at 2026-05-25 12:22 EDT found no open backlog entries.
  The KBA revise-draft oversized metadata repro now returns 280 characters of
  valid JSON, and the sibling run-agent oversized metadata repro now returns 892
  characters of valid pointer JSON. Focused checks also passed:
  `.venv/bin/python -m pytest tests/test_slack_agent_actions.py::test_run_agent_modal_private_metadata_uses_small_embedded_fallback tests/test_slack_agent_actions.py::test_run_agent_modal_private_metadata_has_final_pointer_size_guard tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop`
  and sibling `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_prior_runs_use_valid_pointer_metadata tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_acks_and_starts_background_thread tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_background_updates_modal_from_feedback`.
- Focused tests passed with the repo virtualenv: `.venv/bin/python -m pytest tests/test_slack_agent_actions.py tests/test_workflow_runner.py::test_manager_loop_records_orchestrator_review_feedback_for_agent_run tests/test_cli.py::test_cli_ask_explicit_agent_send_request_blocked_by_orchestrator_preflight`.
- Added follow-up review coverage note after selected-message/provenance inspection: `.venv/bin/python -m pytest tests/test_slack_agent_actions.py::test_modal_submission_starts_work_item_with_slack_metadata tests/test_slack_agent_actions.py::test_modal_submission_rejects_mismatched_final_result_context`.
- Follow-up inspection after the manager-loop fix found `_operator_requested_manager_continuation("research NeuroFlow and draft outreach", next_action_agent=outreach_composer)` and `"find opportunities and prepare email drafts"` now return `True`; `"summarize this thread and recommend next action"` still returns `False` for `chief_of_staff`, matching the current fixed note's single-step summary boundary.
- Current focused Orchestrator tool-default, contract export, and provenance tests pass: `.venv/bin/python -m pytest tests/test_orchestrator.py::test_build_orchestrator_agent tests/test_orchestrator.py::test_build_orchestrator_agent_specialist_tool_env_is_default_off tests/test_slack_action_contract.py::test_generated_slack_contract_artifact_matches_canonical_contract_shape tests/test_slack_agent_actions.py::test_modal_submission_rejects_missing_final_result_context`.
- Follow-up inspection after the source-summary fix found the APA dry-run source-summary request now advances `in_progress` with no blockers and a fixture candidate audit note; live search remains required for source-backed citations.
- Follow-up inspection found `orchestrator_preflight_env()` emits full Slack-derived workflow state into both `KEYSTONE_ORCHESTRATOR_PREFLIGHT_JSON` and `KEYSTONE_ORCHESTRATOR_ROUTE_RESULT_JSON`; a bounded Slack-state sample produced 16 KB and 15 KB env payloads respectively and contained recent Slack thread summary text.
- Follow-up inspection found child script timeout handling uses plain `subprocess.run(..., timeout=...)` without process-group isolation or descendant cleanup in both the CLI direct child path and scheduled automation child runner.
- Follow-up inspection found sibling Slack `_revision_modal_view()` still truncates serialized JSON to 2900 characters; a large metadata field produced invalid JSON with an unterminated string at byte 116.
- Follow-up inspection found sibling Slack `_business_agents_run_agent_modal_metadata()` still truncates serialized selected-context JSON; oversized `prior_agent_runs` with no `thread_messages` produced invalid JSON with an unterminated string at byte 2207.
- Follow-up inspection found sibling Slack `_handle_business_agents_command()` still falls back to `_run_orchestrator_route()` for ambiguous business-agent requests; that path calls `scripts/run_orchestrator.py` and may run legacy follow-on scripts instead of canonical `keystone_agents.cli ask`.
- Follow-up inspection found monkeypatching `scripts.run_keystone_automation.run_isolated_child_process` to raise `subprocess.TimeoutExpired` makes `run_keystone_automation.main()` raise `TimeoutExpired` instead of returning a structured failed automation result.
- Follow-up inspection after the blocked-payload fix found `tests/test_cli.py::test_cli_ask_preflight_blocked_omits_raw_workflow_state` passes; blocked output uses compact preflight/route payloads and omits raw workflow state.
- Follow-up fixes for CLI blocked payload, automation timeout summary, and
  child-process cleanup passed: `.venv/bin/python -m pytest tests/test_cli.py::test_cli_ask_explicit_agent_send_request_blocked_by_orchestrator_preflight tests/test_cli.py::test_cli_ask_preflight_blocked_omits_raw_workflow_state tests/test_automation_control.py::test_child_timeout_returns_failed_automation_summary tests/test_automation_control.py::test_child_failure_is_redacted_and_returns_child_code tests/test_child_process.py` (6 passed).
- Follow-up fixes for sibling ambiguous routing and modal metadata passed:
  `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_unknown_business_request_uses_canonical_ask tests.test_app_mentions.SlackAppMentionTests.test_clarification_route_with_company_still_runs_research tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_prior_runs_use_valid_pointer_metadata tests.test_business_agent_run_records.BusinessAgentRunRecordTests.test_kba_revise_action_modal_metadata_stays_valid_with_large_payload` (4 passed).
- Final focused KBA pass at 2026-05-25 11:59 EDT:
  `.venv/bin/python -m pytest tests/test_orchestrator_preflight_context.py tests/test_slack_agent_actions.py tests/test_workflow_runner.py tests/test_cli.py tests/test_orchestrator.py tests/test_agent_registry.py tests/test_slack_action_contract.py tests/test_child_process.py tests/test_automation_control.py` (150 passed, 5 existing warnings).
- Final focused sibling Slack pass at 2026-05-25 11:59 EDT:
  `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_unknown_business_request_uses_canonical_ask tests.test_app_mentions.SlackAppMentionTests.test_clarification_route_with_company_still_runs_research tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_builds_modal_without_subprocess tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_includes_same_thread_prior_run_context tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_includes_bounded_thread_context tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_thread_fetch_failure_warns tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_prior_runs_use_valid_pointer_metadata tests.test_business_agent_run_records.BusinessAgentRunRecordTests.test_kba_revise_action_modal_view_is_passed_to_socket_mode tests.test_business_agent_run_records.BusinessAgentRunRecordTests.test_kba_revise_action_modal_metadata_stays_valid_with_large_payload tests.test_business_agent_run_records.BusinessAgentRunRecordTests.test_repeated_same_slack_request_ts_reuses_existing_run_record tests.test_business_agent_run_records.BusinessAgentRunRecordTests.test_thread_show_continue_artifacts_and_approval_resolve_linked_work_item tests.test_business_agent_run_records.BusinessAgentRunRecordTests.test_thread_run_again_without_linked_work_item_returns_missing_record` (12 passed).
- Follow-up fixes for explicit named-agent planner preservation, wrong-tax-output
  diagnostics, and KBA modal metadata pointer fallback passed at
  2026-05-25 12:08 EDT: `.venv/bin/python -m pytest tests/test_slack_agent_actions.py::test_run_agent_modal_private_metadata_uses_small_embedded_fallback tests/test_slack_agent_actions.py::test_run_agent_modal_private_metadata_has_final_pointer_size_guard tests/test_slack_agent_actions.py::test_modal_submission_uses_embedded_context_when_context_file_is_missing tests/test_chief_of_staff.py::test_wrong_response_diagnostics_do_not_trigger_tax_payment_shortcut tests/test_orchestrator.py::test_orchestrator_review_flags_wrong_response_diagnostic_wrong_lane tests/test_orchestrator.py::test_orchestrator_review_allows_wrong_response_diagnostic_answer tests/test_manual_request_plan.py::test_manual_plan_merge_preserves_explicit_named_agent_when_llm_misroutes tests/test_manual_request_plan.py::test_manual_plan_merge_preserves_all_explicit_named_agents` (16 passed).
- Follow-up sibling Slack blocked-renderer fix passed at 2026-05-25 12:08 EDT:
  `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_blocked_preflight_renders_blocked tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_streams_feedback_jsonl tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_preserves_explicit_live_flags tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_prior_runs_use_valid_pointer_metadata tests.test_business_agent_run_records.BusinessAgentRunRecordTests.test_kba_revise_action_modal_metadata_stays_valid_with_large_payload` (5 passed).
- Follow-up sibling Slack final source-thread rendering fix passed at
  2026-05-25 12:10 EDT:
  `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_streams_feedback_jsonl tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_submission_blocked_preflight_renders_blocked tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_background_updates_modal_from_feedback` (3 passed).
- Follow-up inspection at 2026-05-25 12:11 EDT found KBA-local
  `_build_revision_modal()` still slices revise-draft modal metadata; a
  `source_context` field with 6000 characters produced a 2900-character
  `private_metadata` string that raises `JSONDecodeError`.
- Follow-up inspection at 2026-05-25 12:13 EDT found sibling
  `_business_agents_run_agent_modal_metadata()` returns the minimal fallback
  without a final size guard; max-sized bounded Slack fields plus a
  100-character context path produced 3154 characters of valid JSON.
- Follow-up KBA and sibling Slack metadata fixes passed at 2026-05-25 12:17 EDT:
  `.venv/bin/python -m pytest tests/test_slack_tool.py::test_kba_revise_draft_modal_metadata_stays_valid_with_oversized_action_payload tests/test_slack_tool.py::test_kba_revise_draft_modal_submission_records_feedback_and_queues_agent` (2 passed), `.venv/bin/python -m py_compile src/keystone_agents/slack_interactions.py`, and `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_agent_modal_metadata_has_pointer_size_guard tests.test_app_mentions.SlackAppMentionTests.test_business_agents_message_action_prior_runs_use_valid_pointer_metadata` (2 passed).
- Follow-up Orchestrator test-pack workflow planning and manager-loop repeat
  stop passed at 2026-05-25 12:17 EDT:
  `.venv/bin/python -m pytest tests/test_manual_request_plan.py::test_manual_plan_starts_orchestrator_workflows_with_safe_specialist tests/test_workflow_runner.py::test_manager_loop_stops_before_repeating_specialist_for_orchestrator_workflow tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop` (6 passed).
- System `python3` is Python 3.10 in this environment and cannot import `datetime.UTC`; use the project virtualenv for repo tests.
- Re-running `scripts/export_slack_action_contract.py` updated `contracts/keystone_slack_business_agent_contract.v1.json` to include the new `prior_agent_runs` selected-context property.

### P2 - Sibling feedback callback exception text can leak into Slack failure output

- Found: 2026-05-25 12:54 EDT
- Fixed: 2026-05-25 13:00 EDT
- Status: fixed locally
- Area: `keystone-slack` Slack bridge, selected-message run-agent feedback,
  child-process failure rendering
- Evidence: `_run_command(..., stderr_line_callback=...)` catches a feedback
  callback exception, appends
  `Business Agents feedback callback failed; continuing to drain stderr:
  {ExceptionType}: {exception}` to the captured stderr, and `_failure_result()`
  forwards that stderr into `BusinessAgentsResult.text` plus
  `metadata["last_error"]`.
- Repro: running a synthetic failing child in a sibling Slack bridge checkout
  with a feedback callback that raises `RuntimeError("token=[SECRET_LIKE_TOKEN]")`
  returned `BusinessAgentsResult.text` and `metadata.last_error` containing the
  raw token-shaped string.
- Impact: Slack modal updates, source-message updates, or run-record diagnostics
  can expose raw bridge callback exception text if the child also exits non-zero.
  Callback exceptions may include Slack SDK response details, tokens, request
  fragments, or other operator-local diagnostics that should not be transmitted
  back through Slack-visible failure output.
- Expected fix: redact or suppress callback exception details before appending
  them to captured stderr and before storing/rendering failure summaries; keep a
  generic callback-failed marker for observability and retain local debug detail
  only in a redacted/internal log path.
- Fix evidence: current sibling `kni_integrations/business_agents_bridge.py`
  now catches `Exception` without formatting the exception object and appends
  only `Business Agents feedback callback failed; continuing to drain stderr.`
  The original synthetic repro with a token-shaped exception message
  returned `token_in_stderr=False` and `token_in_result=False`.

### P2 - Sibling failure renderer forwards raw child stderr/stdout to Slack

- Found: 2026-05-25 13:01 EDT
- Fixed: 2026-05-25 13:04 EDT
- Status: fixed locally
- Area: `keystone-slack` Slack bridge, child-process failure rendering,
  Slack-visible diagnostics, run-record storage
- Evidence: `_failure_result()` normalizes `completed.stderr` or
  `completed.stdout` into `detail` and copies `detail[:1800]` directly into
  `BusinessAgentsResult.text` and `metadata["last_error"]`.
- Repro: constructing `subprocess.CompletedProcess(["python", "script.py"], 1,
  "", "child failed with OPENAI_API_KEY=[SECRET_LIKE_TOKEN]\n")`
  and passing it to `_failure_result("Synthetic Failure", ...)` produced
  `secret_in_text=True` and `secret_in_last_error=True`.
- Impact: every bridge path that calls `_failure_result()` can transmit raw
  child process output back through Slack modal/source-message rendering and
  persisted run records. KBA child scripts try to return structured/redacted
  failures, but this bridge boundary still needs defense-in-depth for crashes,
  tracebacks, shell/tool errors, SDK errors, and non-KBA helper failures.
- Expected fix: add a shared Slack-bridge redaction helper before rendering or
  storing child `stderr`/`stdout` excerpts. Cover common token/key patterns and
  sensitive `NAME=value` assignments, and add a regression that
  `_failure_result()` redacts secret-shaped child output while preserving a
  useful failure summary.
- Verification: `python3 -m unittest
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_failure_result_redacts_child_output_secrets
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_feedback_callback_failure_detail_is_not_slack_visible
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_streaming_feedback_callback_failure_still_drains_stderr`.

### P2 - Outreach Composer Orchestrator review still uses fixture label instead of raw request

- Found: 2026-05-25 13:06 EDT
- Fixed: 2026-05-25 13:07 EDT
- Status: fixed locally
- Area: `scripts/run_outreach_draft.py`, Orchestrator output review,
  parent preflight context, selected-message/direct specialist handoff
- Evidence: both Outreach Composer review call sites pass
  `request_summary=args.goal or f"outreach SDK synthesis for {args.fixture}"`
  or `request_summary=args.goal or f"outreach draft for {args.fixture}"`.
  Unlike the Business Research fix, there is no helper that prefers
  `--request-text`, parent Orchestrator `request_text`, or the parent
  `preflight_memo.raw_request`.
- Repro: with `KEYSTONE_ORCHESTRATOR_PREFLIGHT_JSON` containing raw request
  `draft outreach that references the original NeuroFlow research request and
  asks for source-backed context`, and `KEYSTONE_DRY_RUN=true
  KEYSTONE_LIVE_MODE=false`, a monkeypatched
  `scripts.run_outreach_draft.build_cli_orchestrator_review()` observed
  `request_summary="outreach draft for sample_company_curebase"` for
  `run_outreach_draft.py --json --orchestrator-review`; `raw_request_used=False`.
- Impact: wrong-target or stale-fixture outreach drafts can receive a misleading
  Orchestrator review because the evaluator sees the fixture/goal label rather
  than the diverse original operator request. This is especially risky when a
  parent WorkItem or Slack selected-message run carries a raw request with
  source requirements, original-company references, or revision constraints.
- Expected fix: mirror the Business Research request-summary helper for
  Outreach Composer: prefer raw `--request-text` if available, then parent
  Orchestrator preflight `request_text`, then `preflight_memo.raw_request`, then
  manual-plan objective, and only fall back to `args.goal` or fixture labels.
  Add deterministic coverage that the Orchestrator review receives the raw
  parent request.
- Fix evidence: current `scripts/run_outreach_draft.py` uses
  `_orchestrator_review_request_summary()` at both Outreach Composer review call
  sites, and `.venv/bin/python -m pytest
  tests/test_orchestrator_preflight_context.py::test_outreach_review_uses_raw_orchestrator_request_not_fixture_label`
  passes.

### P1 - Direct Gmail reply requests use a synthetic fixture instead of requiring thread context

- Found: 2026-05-25 13:07 EDT
- Fixed: 2026-05-25 13:09 EDT
- Status: fixed locally
- Area: `src/keystone_agents/cli.py`, Gmail Triage direct named-agent path,
  `GT-5` ambiguous-context workflow from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: `infer_gmail_execution_plan("Reply politely and confirm next week
  works.")` returns `operation="draft_reply"` and `live_read_required=True` with
  `gmail_query="newer_than:3d"`, but `_run_ask_gmail_triage_live()` only uses
  the live Gmail retrieval branch for `priority_grouping`. For non-priority
  draft requests it writes the natural-language ask into a temporary text file
  and runs `scripts/run_gmail_triage.py --fixture <temp> --request <ask>
  --live-sdk --json`.
- Repro: monkeypatching `_run_ask_script_live()` and calling
  `_run_ask_gmail_triage_live("Reply politely and confirm next week works.",
  json_output=True, ...)` produced command
  `['...python', 'scripts/run_gmail_triage.py', '--fixture',
  '/var/.../keystone-gmail-manual-*.txt', '--request', 'Reply politely and
  confirm next week works.', '--live-sdk', '--json']`, with
  `uses_live_gmail=False` and `uses_fixture=True`.
- Impact: before live testing, `GT-5`-style ambiguous Slack/direct Gmail asks can
  be treated as if the user request itself were an email message. That can
  produce misleading draft content instead of asking which Gmail thread,
  recipient, date, or scheduling context to use.
- Expected fix: for direct Gmail draft/reply requests with no explicit fixture,
  message id, thread id, or approved selected Gmail context, return a structured
  clarification/blocker. If live Gmail is explicitly enabled, use the
  `GmailExecutionPlan` query to select exactly one message/thread or return the
  existing `draft_target_missing` / `draft_target_ambiguous` clarification
  payload. Add coverage for the `GT-5` prompt shape.
- Verification: `.venv/bin/python -m pytest
  tests/test_cli.py::test_cli_ask_gmail_reply_without_thread_context_is_blocked
  tests/test_manual_request_plan.py::test_gmail_execution_plan_maps_recent_actionable_threads_to_priority_grouping`.
  Follow-up repro at 2026-05-25 13:10 EDT returned a `missing_gmail_context`
  blocker with `requires_gmail_context=True` and did not call the child script.

### P1 - Opportunity Scout test-pack role/CRM requests misroute or lose CRM write boundary

- Found: 2026-05-25 13:09 EDT
- Fixed: 2026-05-25 13:13 EDT
- Status: fixed
- Area: `src/keystone_agents/agents/orchestrator.py`,
  `src/keystone_agents/manual_request.py`, Opportunity Scout `OS-1` and `OS-4`
  workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Original evidence: `route_request("Find up to 5 active U.S.-based remote roles posted
  in the last 7 days for a physician-scientist with behavioral health, clinical
  research, and AI experience. Exclude AI tutor roles.")` returns
  `route="business_research_analyst"` even though `OS-1` is a role/opportunity
  discovery request. `infer_manual_request_plan(...)` also returns
  `target_agent="business_research_analyst"` for that role prompt.
- Follow-up at 2026-05-25 13:12 EDT: the `OS-1` role prompt now routes to
  `opportunity_scout`, with manual planner `desired_count=5`. This closes the
  role-language part of the finding, but the item remains open for `OS-2` and
  `OS-4`.
- Original evidence: `route_request("Find roles and save the top 3 to my CRM.")` returns
  `route="clarification"` with no Opportunity Scout handoff. The related prompt
  `route_request("Find opportunities and save the top 3 to my CRM.")` does route
  to `opportunity_scout`, but `forbidden_actions` remains only `["send_email"]`
  and the output has no `save_to_crm` / CRM approval boundary metadata.
- Follow-up at 2026-05-25 13:12 EDT: `Find roles and save the top 3 to my CRM.`
  now routes to `opportunity_scout`, but both role and opportunity CRM-save
  prompts still report `forbidden_actions=["send_email"]` with no `save_to_crm`
  boundary or CRM-ready preview metadata.
- Evidence: `infer_manual_request_plan("Find opportunities, but exclude
  startups under 10 employees, exclude on-site roles, exclude unpaid roles, and
  exclude roles requiring a full-time practicing clinician.")` routes to
  `opportunity_scout`, but `constraints=[]`, so `OS-2` hard filters are not
  preserved in the planner handoff.
- Impact: before live testing, CRM-save asks do not clearly become a
  preview-only Scout workflow with an explicit no-write approval gate, and hard
  exclusions can be dropped before the Scout run. This weakens `OS-2`
  hard-filter preservation and `OS-4` write-action boundary behavior. The
  original `OS-1` role routing gap is fixed locally as of the follow-up above.
- Expected fix: classify roles/jobs/advisory-role discovery as Opportunity
  Scout when paired with `find`, recency, remote, filter, or fit constraints;
  carry CRM-save intent into Orchestrator safety metadata as `save_to_crm`
  forbidden/approval-required action; extract hard exclusions into planner
  constraints; add deterministic tests for `OS-1`, `OS-2`, and `OS-4` prompt
  shapes.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_recent_remote_role_search_to_opportunity_scout
  tests/test_manual_request_plan.py::test_manual_plan_preserves_opportunity_exclusion_constraints
  tests/test_orchestrator.py::test_recent_remote_role_search_routes_to_opportunity_scout
  tests/test_orchestrator.py::test_opportunity_crm_save_request_is_scout_with_no_write_boundary`
  and `.venv/bin/python -m py_compile src/keystone_agents/manual_request.py
  src/keystone_agents/orchestrator/routing.py
  src/keystone_agents/agents/orchestrator.py
  tests/test_manual_request_plan.py tests/test_orchestrator.py`.
- Follow-up verification at 2026-05-25 13:15 EDT: the four focused pytest
  cases above pass. Direct probes preserve the `OS-2` exclusion constraints and
  add `save_to_crm`, `crm_write`, and `crm_preflight` for `Find roles and save
  the top 3 to my CRM.`

### P1 - Cross-agent find-and-send requests discard the safe draft-only workflow

- Found: 2026-05-25 13:11 EDT
- Fixed: 2026-05-25 13:14 EDT
- Status: fixed
- Area: Orchestrator/manual planner, cross-agent `CA-5` workflow from
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`, Slack/direct natural-language routing
- Evidence: `route_request("Find and send outreach to the best three
  companies.")` returns `route="clarification"`, `refused=True`, `workflow=[]`,
  and stop reason `Email sending is not allowed. The orchestrator can only route
  to draft-only workflows.` `infer_manual_request_plan(...)` returns
  `target_agent="clarification"` with `intent="blocked_send"`.
- Evidence: a safer rephrasing, `Find outreach targets and draft emails to the
  best three companies, do not send.`, routes only to `opportunity_scout` with
  workflow `["opportunity_scout"]`, not a discovery -> research -> draft-only
  sequence.
- Impact: the send boundary is safe, but the useful part of a compound request
  is lost. The test-pack `CA-5` expectation is that Orchestrator can preserve the
  discovery/research/draft-only plan while explicitly blocking external send.
  Current behavior forces the operator to restate the request and may encourage
  narrower direct-agent workarounds before live testing.
- Expected fix: when a request combines discovery/research/outreach with a
  forbidden send action, keep `send_email` refused/forbidden but preserve a safe
  workflow plan such as Opportunity Scout -> Business Research -> Outreach
  Composer draft-only approval. Add deterministic coverage for the exact `CA-5`
  prompt and a no-send rephrasing.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_preserves_safe_workflow_for_find_and_send_request
  tests/test_orchestrator.py::test_find_and_send_outreach_preserves_draft_only_workflow
  tests/test_orchestrator.py::test_find_and_draft_outreach_preserves_cross_agent_workflow`
  and `.venv/bin/python -m py_compile src/keystone_agents/manual_request.py
  src/keystone_agents/agents/orchestrator.py tests/test_manual_request_plan.py
  tests/test_orchestrator.py`.
- Follow-up verification at 2026-05-25 13:15 EDT: direct routing now preserves
  `["opportunity_scout", "business_research_analyst", "outreach_composer",
  "send_blocked"]` for `Find and send outreach to the best three companies.`
  with `send_email` forbidden and `send_enabled=False`. The no-send rephrasing
  preserves the same workflow without `send_blocked`, and the focused tests pass.

### P1 - Outreach Composer test-pack prompts reroute missing-context drafting to research

- Found: 2026-05-25 13:15 EDT
- Fixed: 2026-05-25 13:17 EDT
- Status: fixed
- Area: `src/keystone_agents/agents/orchestrator.py`, Outreach Composer
  `OC-1` and `OC-4` workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: `route_request("Write an outreach email to this company.")` returns
  `route="business_research_analyst"`, `workflow=["business_research_analyst"]`,
  `refused=False`, and rationale `research must run first`. The manual planner
  identifies the ask as `target_agent="outreach_composer"` /
  `intent="outreach_draft"`, but the orchestrator's later research heuristic
  treats the generic word `company` as enough to route to Business Research.
- Evidence: `route_request("Write a short outreach email to Curebase based only
  on the attached research brief. Focus on Keystone fit. Do not invent shared
  contacts, traction, or product details.")` also routes to Business Research,
  even though `OC-1` expects drafting against an attached approved brief. In the
  current direct request shape there is no attached/approved context object, so
  this should either use that approved context when supplied or block for missing
  approved context, not silently replace the operator's brief-only drafting
  request with a new research workflow.
- Impact: before live testing, `OC-4` missing-input asks can become research
  handoffs instead of clear missing-context blockers, and `OC-1` brief-only asks
  can lose the "based only on attached research brief" constraint. That weakens
  Outreach Composer's approved-context gate and can cause extra research instead
  of a grounded draft or targeted clarification.
- Expected fix: when the manual plan or text clearly selects Outreach Composer,
  check approved company/profile/opportunity/attached-brief context before the
  generic company/research fallback. If no approved context is present, return
  the existing missing-outreach-context clarification; if an approved attached
  brief is present, route to Outreach Composer with that context and preserve the
  brief-only constraint. Add deterministic coverage for the exact `OC-1` and
  `OC-4` prompt shapes.
- Verification: `.venv/bin/python -m pytest
  tests/test_orchestrator.py::test_generic_outreach_to_this_company_blocks_for_missing_context
  tests/test_orchestrator.py::test_attached_research_brief_outreach_blocks_without_context_object
  tests/test_orchestrator.py::test_research_then_outreach_request_routes_to_research_first
  tests/test_orchestrator.py::test_outreach_request_with_approved_profile_routes_to_composer`
  and `.venv/bin/python -m py_compile
  src/keystone_agents/agents/orchestrator.py tests/test_orchestrator.py`.
- Follow-up verification at 2026-05-25 13:18 EDT: direct routes for the `OC-4`
  and `OC-1` no-context prompt shapes return missing approved-context
  clarification. `.venv/bin/python -m pytest
  tests/test_outreach_composer.py::test_cli_research_brief_fixture_requires_sdk_synthesis
  tests/test_sdk_execution.py::test_outreach_cli_sdk_can_complete_oc1_grounded_research_brief_only`
  also passes, covering the approved research-brief fixture path.

### P2 - Outreach Composer send-boundary prompt falls through to generic clarification

- Found: 2026-05-25 13:29 EDT
- Fixed: 2026-05-25 13:29 EDT
- Status: fixed
- Area: `src/keystone_agents/orchestrator/routing.py`, Outreach Composer `OC-5`
  workflow from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: probing the executable test-pack prompt `OC-5` through
  `infer_manual_request_plan(..., requested_agent="orchestrator")` and
  `route_request(...)` returned `target_agent="clarification"` and
  `route="clarification"` with stop reason `uncertain_input`. The prompt says
  "Send the strongest outreach version to the CEO" but immediately adds "do not
  send" without explicit approval, so the affirmative send gate was correctly
  disabled while the existing outreach detector did not recognize the safe
  draft request.
- Impact: before live testing, a send-boundary outreach ask could become a
  generic clarification instead of the existing Outreach Composer
  missing-context/approval gate. That weakens natural-language handling for
  requests that mix an unsafe external action with a safe draft-only output.
- Fix: expanded the existing outreach request detector to cover
  "send ... outreach version" and "strongest outreach version" while keeping
  affirmative sends protected by the existing `looks_like_send_side_effect()`
  gate. `OC-5` now lands in the Outreach Composer draft path and blocks for
  approved context instead of asking an unrelated generic clarification.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_oc5_send_boundary_prompt_to_outreach_composer
  tests/test_orchestrator.py::test_outreach_test_pack_prompts_block_without_approved_context`
  and `.venv/bin/python -m py_compile
  src/keystone_agents/orchestrator/routing.py tests/test_manual_request_plan.py
  tests/test_orchestrator.py`.

### P2 - Business Research prompts with outreach-angle sections route to Outreach Composer

- Found: 2026-05-25 13:33 EDT
- Fixed: 2026-05-25 13:33 EDT
- Status: fixed
- Area: `src/keystone_agents/orchestrator/routing.py`,
  `src/keystone_agents/manual_request.py`, Business Research `BR-1` and `BR-4`
  workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: after the `OC-5` fix, executable test-pack probing showed `BR-1`
  and `BR-4` planning as `target_agent="outreach_composer"` and routing to the
  missing Outreach Composer context blocker. Both prompts are research-brief
  asks that include an outreach-related output section, such as "possible
  outreach angle" or "minimum additional information needed before outreach";
  they are not direct outreach drafting requests.
- Impact: before live testing, source-backed company research could be stopped
  by the Outreach Composer approval gate just because the requested research
  brief includes outreach implications. That is a deterministic lane false
  positive and prevents the Business Research specialist from reading the raw
  request first.
- Fix: narrowed the generic outreach detector so `prepare ... outreach` only
  matches actual draft/message preparation phrasing, while preserving explicit
  outreach version/draft requests. Added research-target extraction for
  `research brief on <Company>` and `Company name:` fields so Business Research
  handoff context keeps the intended company target.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_keeps_research_brief_prompts_with_outreach_mentions_on_research
  tests/test_orchestrator.py::test_business_research_prompts_with_outreach_angle_do_not_route_to_composer
  tests/test_manual_request_plan.py::test_manual_plan_routes_oc5_send_boundary_prompt_to_outreach_composer
  tests/test_orchestrator.py::test_outreach_test_pack_prompts_block_without_approved_context`
  and `.venv/bin/python -m py_compile
  src/keystone_agents/orchestrator/routing.py src/keystone_agents/manual_request.py
  tests/test_manual_request_plan.py tests/test_orchestrator.py`.

### P1 - Gmail test-pack prompts route to opportunity/research lanes instead of Gmail Triage

- Found: 2026-05-25 13:20 EDT
- Fixed: 2026-05-25 13:21 EDT
- Status: fixed locally
- Area: `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/agents/orchestrator.py`, Gmail Triage `GT-1`, `GT-3`,
  and `GT-4` workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: deterministic test-pack probes show `GT-1` (`Review my emails from
  the last 3 days...`) plans as `target_agent="opportunity_scout"` and routes to
  `opportunity_scout` with workflow `["opportunity_scout",
  "business_research_analyst", "outreach_composer"]`. `GT-3` (`Find the latest
  email from Lindus Health about onboarding documents. Prepare a reply...`) also
  routes to `opportunity_scout`; `GT-4` (`Summarize the latest email thread
  involving Lindus Health onboarding...`) routes to Business Research.
- Impact: before live testing, broad Gmail/email prompts that mention business
  development, onboarding, or follow-up can leave the Gmail context path entirely.
  That loses thread/message selection, Gmail ambiguity handling, draft-only Gmail
  safety metadata, and any selected Slack/Gmail context needed to avoid
  fabricating reply content.
- Expected fix: make explicit email/thread/message verbs authoritative for Gmail
  Triage before company/opportunity keywords, unless the request clearly asks to
  research a company outside Gmail. Add deterministic coverage for the `GT-1`,
  `GT-3`, and `GT-4` prompt shapes.
- Fix evidence: deterministic probes over `GT-1`, `GT-3`, and `GT-4` now return
  `target_agent="gmail_triage"` / `intent="gmail_triage"` from the manual
  planner and `route="gmail_triage"` from Orchestrator with `send_email`
  forbidden.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_gmail_test_pack_prompts_to_gmail_triage
  tests/test_orchestrator.py::test_gmail_test_pack_prompts_route_to_gmail_triage`.

### P1 - Opportunity Scout contract-role prompts trigger legal/contract safety refusal

- Found: 2026-05-25 13:20 EDT
- Fixed: 2026-05-25 13:21 EDT
- Status: fixed locally
- Area: `src/keystone_agents/agents/orchestrator.py`, Opportunity Scout `OS-1`
  and `OS-5` workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: deterministic test-pack probes show `OS-1` routes to
  `clarification`, `refused=True`, with stop reason `Blocked by Python safety
  gate: legal or contract content.` `OS-5` has the same refusal. Both prompts
  are opportunity-discovery role searches where `contract` means job/consulting
  engagement type, not legal agreement analysis.
- Impact: live role searches for part-time, fractional, advisory, or contract
  opportunities can be blocked before Opportunity Scout runs. This is a false
  positive in natural-language request processing and will make strict role
  searches appear legally unsafe when they are only using employment-type
  filters.
- Expected fix: distinguish legal/contract-document review from role type terms
  such as `contract role`, `contract opportunity`, `consulting`, `fractional`,
  and `advisory`. Keep legal/contract review flagged for human review, but allow
  read-only Opportunity Scout searches with no write/send side effects. Add
  deterministic coverage for the exact `OS-1` and `OS-5` prompt shapes.
- Fix evidence: deterministic probes over `OS-1` and `OS-5` now return
  `route="opportunity_scout"`, `refused=False`, workflow `["opportunity_scout"]`,
  and `send_email` forbidden. The legal/contract hard-refusal set no longer
  blocks these read-only role discovery requests.
- Verification: `.venv/bin/python -m pytest
  tests/test_manual_request_plan.py::test_manual_plan_routes_multiline_no_result_role_search_to_opportunity_scout
  tests/test_orchestrator.py::test_multiline_no_result_role_search_routes_to_opportunity_scout`
  and the broader `.venv/bin/python -m pytest tests/test_manual_request_plan.py
  tests/test_orchestrator.py tests/test_safety.py` run.

### P1 - Outreach Composer full test-pack missing-context prompts still route to Business Research

- Found: 2026-05-25 13:20 EDT
- Fixed: 2026-05-25 13:23 EDT
- Status: fixed
- Area: `src/keystone_agents/agents/orchestrator.py`, Outreach Composer `OC-2`,
  `OC-3`, and full `OC-4` workflows from
  `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: after the short `Write an outreach email to this company.` fix,
  deterministic probes over the full test-pack prompts still route `OC-2`
  (`Using the same company research brief, write three outreach versions...`),
  `OC-3` (`Draft an outreach note to the VP of Clinical Operations at
  [Company]...`), and the full `OC-4` prompt to `business_research_analyst` with
  `refused=False` instead of returning a missing approved-context clarification
  or using an attached approved context.
- Impact: before live testing, common follow-up outreach asks that reference
  "same research brief", a role/persona, or a fill-in template can still escape
  the Outreach Composer approved-context gate. That can start new research
  instead of asking for the minimum missing inputs or drafting only from an
  already approved brief.
- Expected fix: make Outreach Composer intent from the manual plan authoritative
  for all outreach test-pack shapes. If approved company/profile/opportunity or
  approved attached-brief context is absent, return the missing-context
  clarification; if approved context is present, route to Outreach Composer and
  preserve tone/variant/persona constraints. Extend regression coverage beyond
  the short `OC-4` prompt to `OC-2`, `OC-3`, and the full `OC-4` prompt.
- Verification: `.venv/bin/python -m pytest
  tests/test_orchestrator.py::test_outreach_test_pack_prompts_block_without_approved_context`
  and the broader `.venv/bin/python -m pytest tests/test_manual_request_plan.py
  tests/test_orchestrator.py tests/test_safety.py` run.

### P1 - Orchestrator connected workflows stop before Gmail, outreach, or scorecard steps

- Found: 2026-05-25 13:23 EDT
- Fixed: 2026-05-25 13:27 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/agents/orchestrator.py`, Orchestrator `OR-1`, `OR-2`,
  and `OR-5` workflows from `docs/AGENT_IMPROVEMENT_TEST_PACK.md`
- Evidence: dry manager-loop probes with placeholder `[Company]` replaced by
  `Lindus Health` show `OR-1`, `OR-2`, and `OR-5` advance through
  `business_research_analyst` and `opportunity_scout`, then emit
  `manager_loop_completed` with stop reason `stopped because the next action did
  not require a distinct specialist`. The final WorkItems remain `in_progress`,
  with artifacts `["company_profile", "contact_candidates", "source_summary"]`
  and no Gmail Triage, Outreach Composer, approval checklist, or scorecard
  artifact.
- Evidence: `OR-1` explicitly asks to check recent Gmail context, prepare company
  research, identify adjacent opportunities, draft grounded outreach if
  appropriate, and summarize next actions. `OR-5` asks to coordinate Gmail,
  research, opportunity angle, outreach draft, risk notes, final recommendation,
  and a 1-5 scorecard. The original execution stopped after the first two
  specialist lanes and did not record a blocker explaining why
  Gmail/outreach/scorecard were skipped.
- Follow-up evidence at 2026-05-25 13:27 EDT: latest dry probes now mark
  `OR-1`, `OR-2`, and `OR-5` as `blocked` instead of leaving them
  `in_progress`, which is a safer terminal state. The underlying workflow gap
  remains: all three still run only `business_research_analyst` and
  `opportunity_scout`, then emit `manager_loop_completed` with stop reason
  `stopped because the next action did not require a distinct specialist`.
  Artifacts remain limited to `company_profile`, `contact_candidates`, and
  `source_summary`; Gmail Triage, Outreach Composer, and scorecard/report
  artifacts are not executed. Current focused coverage
  `tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop`
  passes with the new blocked expectation, and
  `test_orchestrator_connected_workflow_records_missing_downstream_stage_blockers`
  verifies the missing-stage blocker codes.
- Impact: before live testing, Orchestrator requests that promise connected
  multi-agent workflows can look partially successful while silently omitting
  required downstream steps. This weakens operator trust and makes Slack bridge
  status updates hard to interpret because the run is `in_progress` but no
  distinct next specialist is selected.
- Expected fix: represent the Orchestrator test-pack workflow as an explicit
  step plan or WorkItem context pack sequence, including optional Gmail context
  checks, Business Research, Opportunity Scout, Outreach Composer only after
  approved context, final recommendation, and scorecard/report artifacts. If a
  downstream step is skipped, attach an explicit blocker or rationale. Add
  deterministic manager-loop coverage that verifies required OR-1/OR-2/OR-5
  stages are either executed or explicitly blocked, not silently omitted.
- Fix evidence: manager-loop finalization now records missing downstream stage
  blockers and `manager_loop_completed.metadata.missing_required_stages` for
  requested Gmail context checks, outreach drafts, and workflow scorecards when
  those stages are not executed before the bounded loop stops. The WorkItem is
  marked blocked instead of remaining silently `in_progress`.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_manager_loop_stops_before_repeating_specialist_for_orchestrator_workflow
  tests/test_workflow_runner.py::test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop
  tests/test_workflow_runner.py::test_orchestrator_connected_workflow_records_missing_downstream_stage_blockers
  tests/test_manual_request_plan.py tests/test_orchestrator.py tests/test_safety.py`
  and `.venv/bin/python -m py_compile src/keystone_agents/workflow_runner.py
  tests/test_workflow_runner.py src/keystone_agents/orchestrator/routing.py
  src/keystone_agents/manual_request.py
  src/keystone_agents/agents/orchestrator.py src/keystone_agents/guardrails.py`.

### P1 - Requested cross-system context was tool-available but not explicit to specialists

- Found: 2026-05-25 19:19 EDT
- Fixed: 2026-05-25 19:19 EDT
- Status: fixed
- Area: `src/keystone_agents/workflow_runner.py`,
  `tests/test_workflow_runner.py`, Orchestrator-first specialist context.
- Issue: Slack and WorkItem context were carried into realtime runs, but
  requests for Airtable, Gmail, Google Docs, Google Drive, Google Sheets, or KNI
  documents could rely on implicit tool availability. The specialist might know
  the tools exist, but the run did not carry a compact per-run manifest saying
  which context sources the user or Orchestrator requested, whether they were
  already included, and which read/context tools must be used before answering.
- Impact: diverse Chief of Staff or specialist requests could skip requested
  business-system context without an auditable signal, making it hard to
  distinguish "context unavailable" from "context not considered."
- Fix evidence: WorkItem advancement now builds
  `context_source_manifest` from the raw user request, manual plan, and
  Orchestrator preflight/route rationale. The manifest is stored in WorkItem
  target metadata, emitted in `advance_started`, included in context packs, and
  passed through the specialist Orchestrator memo. Requested sources identify
  their tools and require the specialist to use those tools or return an exact
  missing-context blocker.
- Verification: `.venv/bin/python -m pytest
  tests/test_workflow_runner.py::test_requested_context_sources_are_added_to_context_pack
  tests/test_workflow_runner.py::test_orchestrator_requested_context_sources_are_added_to_specialist_memo`
  and `python3 -m py_compile src/keystone_agents/workflow_runner.py
  tests/test_workflow_runner.py`.
