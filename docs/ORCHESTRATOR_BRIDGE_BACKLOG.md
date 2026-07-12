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

### P1 - EVAL-TRACE-DETAIL-001: Eval traces need child-step/tool-call metadata, not only summarized events

- Found: 2026-06-21 01:00 EDT
- Fixed: pending
- Status: open
- Area: eval trace capture, OpenAI-style trace explorer, SDK run summaries,
  WorkItem trace metadata, `promptfoo/eval_database.py`,
  `promptfoo/eval_dashboard.py`, Slack/CLI eval run recording.
- Issue: The dashboard can now expand sanitized trace rows, but many manual
  Slack/CLI runs still produce only coarse `manual_run_summary` and
  `slack_run_saved` events. That supports joins and cleanup diagnostics, but it
  does not yet expose a true OpenAI-style internal trace with child tool calls,
  model usage, approval gates, cache/cost state, retries, and step durations at
  the right granularity.
- Evidence: Current trace rows often report missing model/tool/retrieval
  metadata even when the underlying WorkItem or SDK run may have richer
  runtime context. The UI has a Details panel and field-readiness indicators,
  but the source packets still depend on what Slack/CLI recording passes into
  `record_slack_eval_run()` and `eval_trace_events`.
- Impact: Operators can click into a trace, but a real debugging session may
  still require inspecting local run payloads or WorkItem state outside the
  dashboard. This also makes diagnostic categories look like failures when the
  actual gap is instrumentation coverage.
- Expected fix: Add bounded child-step trace rows or a structured
  `tool_call_summary`/`model_usage_summary` packet to saved eval runs. Include
  stable join fields (`case_id`, `run_id`, `work_item_id`, route, Slack thread,
  trace/span ids), child tool name/status/duration/error kind, approval
  checkpoints, retrieval/source counts, and model/cost/cache metadata when
  available. Keep raw prompts, raw responses, Slack message text, secrets, PHI,
  and tool I/O out of trace storage.
- Validation: Add fixture coverage for a saved Slack/CLI eval run with multiple
  child events and verify the Trace Explorer Details panel shows same-run
  timeline, duration, model/tool/retrieval readiness, and bounded sanitized
  packet data without exposing raw content.

### P2 - EVAL-DATABASE-UX-001: Database tab needs a stable inventory contract distinct from scoring workspace

- Found: 2026-06-21 01:00 EDT
- Fixed: pending
- Status: open
- Area: Eval Case Database UX, review item-level score visibility,
  CSV/JSON export, `promptfoo/eval_dashboard.py`,
  `tests/test_promptfoo_framework.py`.
- Issue: The Database tab is useful as an audit/export table, but it can become
  too wide or duplicate Runs & Scoring when it tries to show every run, review,
  scorecard, evidence, analysis, prompt, and response field at once.
- Evidence: The current implementation now exposes item-level review form
  dimensions as explicit human and Orchestrator Review columns so scores like
  `accuracy`, `relevance`, and `source_quality` are database-visible. Subagent
  review still flagged that notes, rationales, run identity, score status, and
  prompt/response text need a clearer long-term ownership boundary.
- Impact: Operators need separate score columns for audit/export work, but the
  screen can become hard to scan if Database also behaves like the editable
  case-review workspace. Runs & Scoring should remain the place for full review
  forms, follow-up queues, case packets, and detailed score rationales.
- Expected fix: Keep CSV/JSON and case bundles as the complete detail surfaces;
  keep the visible Database table read-only with stable inventory columns,
  explicit review-dimension score columns, compact statuses, and links to
  Review form / case bundle for full comments and rationales. Avoid putting
  editable review controls or full rationales in Database.
- Validation: Add UI tests that assert item-level review dimensions remain
  visible as columns, while review editing and detailed comments stay in Runs &
  Scoring/review-detail surfaces.

### P1 - CONTEXT-EVAL-001: RSS and preprint context agents lack Slack eval cases

- Found: 2026-06-20 00:00 EDT
- Fixed: 2026-06-20
- Status: fixed
- Area: RSS/preprint context agents, Promptfoo Slack expansion coverage,
  `src/keystone_agents/agent_registry.py`,
  `promptfoo/tests/slack_agent_expansion_15.yaml`,
  `tests/test_agent_registry.py`, `tests/test_announcement_context_tools.py`.
- Issue: The new `rss_context_agent` and `preprints_context_agent` were
  registered as named specialists and each `AgentSpec` points at
  `promptfoo/tests/slack_agent_expansion_15.yaml`, but that eval file did not
  include direct Slack-style cases for either named agent.
- Evidence: Registry and context-source tests pass for both agents, and
  `tests/test_announcement_context_tools.py` covers the local read tools and
  schemas. The Slack expansion seed pack now includes
  `slack_rss_context_announcement_history_001` and
  `slack_preprints_context_preliminary_evidence_001`, each using direct
  `@KNI <context agent>` wording, read-only constraints, side-effect
  prohibitions, and sectioned-output expectations.
- Impact: The repo can claim registry-level coverage while missing the actual
  Slack/named-agent behavior that matters operationally: mention parsing,
  Orchestrator preflight, WorkItem route, read-only local data retrieval,
  source/preprint caveats, and Slack output shape for these two new agents.
- Fix: Added one Slack expansion eval case for each agent. The RSS case reads
  bounded historical announcement context and the preprint case verifies
  preliminary-evidence language. Both assert no Slack posting, no feed
  mutation, visible source/reference handling, and concise Answer/Detailed
  Summary output.
- Validation: `.venv/bin/python -m pytest
  tests/test_promptfoo_framework.py::test_promptfoo_seed_pack_has_expected_case_agent_coverage
  tests/test_promptfoo_framework.py::test_promptfoo_seed_pack_has_first_class_context_agent_eval_cases
  tests/test_promptfoo_framework.py::test_promptfoo_context_agent_eval_cases_name_specific_metadata_targets`
  and `.venv/bin/python -m pytest tests/test_agent_registry.py -k
  "context or handoff" tests/test_announcement_context_tools.py` passed on
  2026-06-20. Focused Orchestrator/CLI checks also verify that direct RSS
  context asks with negated "do not post to Slack" constraints route to
  `RssContextResult`, while an affirmative "post this message to Slack" ask
  remains blocked by the send gate. Direct promptfoo-provider execution of the
  two new seed-pack cases returns `route`, `status`, `output_type`, and dry-run
  invocation metadata for `RssContextResult` and `PreprintsContextResult`.
  `npm run eval:promptfoo:json` completed with 0 provider errors and both new
  cases passing in the saved JSON output.

### P1 - SLACK-BRIDGE-RSS-001: RSS/preprints context results are routed but not rendered by Slack bridge

- Found: 2026-06-20 13:15 EDT
- Fixed: 2026-07-06 16:33 EDT
- Status: pre-live complete; fixed locally in sibling `keystone-slack`; live
  Slack probe still pending.
- Area: sibling `keystone-slack` app-mention bridge, context-agent direct
  prefixes, context result rendering, KBA RSS/preprints context agents,
  `src/keystone_agents/agent_mentions.py`, `src/keystone_agents/cli.py`.
- Issue: RSS and preprints context agents can run from KBA, but the current
  Slack bridge does not fully support them as first-class context-agent Slack
  routes. Direct `@KNI rss context agent ...` is rejected before KBA runs, and
  a bridge-prefix fallback can reach KBA but the bridge renders only generic
  `Business Agents WorkItem Ready / WorkItem command completed` instead of the
  context-agent answer.
- Evidence: KBA dry-run now resolves `@KNI rss context agent ...`,
  `rss context agent ...`, and `business agents rss context agent ...` to
  `rss_context_agent`, with equivalent preprints coverage. A live Slack probe
  in `#ai-agents-workflow` at
  `https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1781975195668639`
  completed as route `rss_context_agent` with run
  `sbar_a3a1280584f14922b3a2d5cce0295966`, but Slack posted only
  `WorkItem command completed`. The local `agent_runs` row `576` contains
  `output_type='RssContextResult'` and a usable `human_summary` with
  `*Answer:*`, `*Detailed Summary:*`, and `*Useful references:*`. Read-only
  inspection of sibling
  `keystone-slack/kni_integrations/business_agents_bridge.py` shows
  `BUSINESS_AGENT_DIRECT_PREFIXES` and `CONTEXT_AGENT_DIRECT_PREFIX_ROUTES`
  include Airtable, Google Workspace, and Zotero context aliases, but not RSS
  or preprints; `_context_agent_success_from_ask()` accepts only
  `AirtableContextResult`, `GoogleWorkspaceContextResult`, and
  `ZoteroContextResult`.
- Impact: Operators see a completed Slack run without the answer, which makes
  the RSS/preprints agents look broken even when KBA produced a valid result.
  It also blocks reliable one-agent-at-a-time Slack testing for these two
  context agents.
- Fix: In sibling `keystone-slack`, added RSS/preprints direct prefixes and
  context-route mappings, then allowed `RssContextResult` and
  `PreprintsContextResult` through the same context-agent summary path. The
  rendered Slack text now uses the KBA `human_summary` instead of generic
  WorkItem completion text.
- Verification: KBA contract tests passed:
  `.venv/bin/python -m pytest tests/test_slack_action_contract.py -q`
  (`52 passed`) and
  `.venv/bin/python scripts/validate_slack_bridge_contract.py`. Sibling focused
  bridge tests passed:
  `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_direct_rss_context_agent_request_renders_human_summary tests.test_app_mentions.SlackAppMentionTests.test_direct_preprints_context_agent_request_renders_human_summary`.
- Remaining proof boundary: no fresh live Slack probe was run after the sibling
  bridge fix.
- Readiness update: 2026-07-06 17:12 EDT. Restarted the local KBA eval
  dashboard and reran strict Slack readiness outside the sandbox because Python
  localhost sockets are sandbox-blocked while `curl` can reach the same
  dashboard. The unsandboxed preflight passed with 0 warnings:
  `artifacts/anu60_strict_readiness_unsandboxed.json`. The operator-facing
  command `npm run eval:slack:strict-readiness -- --json` also passed with 0
  warnings. The sibling Socket Mode worker is running and the KBA dashboard
  health endpoint is reachable at `http://127.0.0.1:8769/api/status`.
- Current no-live validation: 2026-07-06. After adding the focused ANU-60 proof
  packet, acceptance map, and doc-contract guard, the local contract suite
  `.venv/bin/python -m pytest tests/test_prompt_contracts.py
  tests/test_slack_action_contract.py -q` passed (`111 passed`);
  `scripts/validate_slack_bridge_contract.py` and
  `scripts/validate_slack_result_rendering_examples.py` passed;
  `npm run eval:slack:anu60-proof` and
  `npm run eval:slack:anu60-preflight` passed; and the broad
  Slack expansion gate passed `36/36` with route coverage in
  `artifacts/anu60_expansion_gate_after_acceptance_map.json`.
- Current sibling bridge validation: 2026-07-06 17:50 EDT. In sibling
  `keystone-slack`, the focused no-live bridge suite passed (`8` tests):
  direct RSS/preprints `human_summary` rendering, conversational Business
  Research named-agent routing, focused company-brief `human_summary`
  precedence over metadata-heavy formatting, timeout failure wording, and
  blocked preflight rendering.
- Current timeout/failure validation: 2026-07-06. The focused sibling timeout
  fixture pair passed (`2` tests):
  `python3 -B -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group tests.test_app_mentions.SlackAppMentionTests.test_business_agents_streaming_run_command_timeout_kills_group`.
  This remains fixture-backed proof for structured timeout failure text and
  process-group cleanup unless a safe forced live timeout probe is explicitly
  approved.
- Live proof checklist: after explicit approval for Slack posting, run
  the no-live handoff first with `npm run eval:slack:anu60-preflight`, then
  readiness with `npm run eval:slack:strict-readiness` or, when read-only Slack
  Web API checks are approved,
  `npm run eval:slack:strict-live-readiness`. Confirm the sibling Socket Mode
  worker with `../keystone-slack/scripts/manage_slack_socket.sh status`. In
  `#ai-agents-workflow`, post one direct RSS context ask and one direct
  preprints context ask. Each must render the KBA `human_summary` answer first,
  show `*Answer:*` / `*Detailed Summary:*` and useful-reference or explicit
  retrieval-limit language when applicable, avoid generic `WorkItem Ready` as
  the visible body, and leave feed/preprint state unmutated. Stop after the
  first failed visible render or runtime failure. Use
  `docs/ANU60_LIVE_SLACK_PROOF_PLAN.md` as the focused live-proof packet and
  `docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md` as the capture form.

### P1 - SLACK-BRIDGE-NAMED-001: Named-agent Slack results ignore the KBA human-summary contract

- Found: 2026-06-20 20:45 EDT
- Fixed: 2026-07-06 16:33 EDT
- Status: pre-live complete; fixed locally in sibling `keystone-slack`; live
  Slack probe still pending.
- Area: sibling `keystone-slack` app-mention bridge, named-agent result
  rendering, conversational app mentions, KBA Slack business-agent contract,
  `src/keystone_agents/slack_action_contract.py`,
  `contracts/keystone_slack_business_agent_contract.v1.json`.
- Issue: KBA now produces and exports clean named-agent result summaries, but
  the sibling Slack bridge can still render named-agent runs through a legacy
  metadata-heavy formatter or reject conversational named-agent requests before
  they reach KBA. This makes successful live Business Research runs look noisy
  or unsupported even when the KBA child process completed and exposed a clean
  `human_summary`.
- Evidence: Four bounded live Slack probes were run in `#ai-agents-workflow`
  before pausing for cost control. The direct Business Research/Abridge run
  completed but Slack showed provider, model, retrieval, and timing metadata
  ahead of the answer. A conversational Nabla prompt using "could the business
  research analyst..." was rejected by the sibling bridge as an unsupported KNI
  command before KBA ran. KBA now has no-live planner and WorkItem coverage for
  that polite named-agent wording: it routes to `business_research_analyst`,
  extracts `Nabla` as the company target, and avoids the Opportunity Scout
  clarification path. Corti completed locally but initially exposed only
  `script_payload.human_summary`; KBA now promotes child `human_summary` to the
  top-level CLI envelope. Suki completed locally with both top-level and child
  `human_summary`, and KBA now also exposes the answer-first Business Research
  summary under `slack_display_text`, `display_text`, and `summary`; the
  exported rendering examples include a Suki-shaped legacy fallback fixture.
  The sibling Slack bridge still needs to consume that contract instead of
  rendering the metadata-heavy Company Research block.
- Impact: Operators cannot trust visible Slack output shape as proof of agent
  quality. The live model and WorkItem path can be correct while Slack still
  displays audit details as the main answer, and natural operator phrasing can
  fail at the sibling bridge rather than exercising KBA's Orchestrator-first
  routing.
- Fix: In KBA, aligned `business_agent_result_display_text()` with the published
  renderer contract so matched named-agent/context-agent payloads prefer
  `human_summary` before stale display fields. In sibling `keystone-slack`,
  added a local answer-first resolver before `CompanyResearchFocusedBrief`
  legacy formatting, so provider/model/retrieval/timing/profile metadata no
  longer leads when KBA supplies `human_summary`.
- Verification: KBA contract tests passed:
  `.venv/bin/python -m pytest tests/test_slack_action_contract.py -q`
  (`52 passed`) and
  `.venv/bin/python scripts/validate_slack_bridge_contract.py`. Sibling focused
  bridge tests passed:
  `python3 -m unittest tests.test_app_mentions.SlackAppMentionTests.test_conversational_named_agent_request_uses_canonical_ask_and_human_summary tests.test_app_mentions.SlackAppMentionTests.test_focused_company_brief_prefers_human_summary_over_metadata tests.test_app_mentions.SlackAppMentionTests.test_focused_company_brief_renders_links_contacts_and_clean_copy`.
- Remaining proof boundary: no fresh live Slack probe was run after the
  answer-first rendering and conversational named-agent fixture fixes.
- Follow-up validation: KBA Gmail blocker wording now names `Gmail Triage`
  inside the provider-visible answer body. Chief/context-agent advisory routing
  now stays Chief-owned for read-only advisory prompts instead of being swallowed
  by Airtable/Workspace approval-plan checkpoints. The broad no-live Slack
  expansion gate now passes `36/36` cases with route coverage satisfied
  (`artifacts/anu60_expansion_gate_after_stripped_chief_regression.json`), and
  focused LangGraph regression coverage verifies stripped Chief advisory context
  requests do not stage `airtable_write_plan` or enter an approval checkpoint.
  Live Slack proof remains pending. The local readiness preflight was refreshed
  on 2026-07-06 17:12 EDT after restarting the KBA eval dashboard; unsandboxed
  strict readiness passed with 0 warnings in
  `artifacts/anu60_strict_readiness_unsandboxed.json`, and the operator-facing
  `npm run eval:slack:strict-readiness -- --json` command also passed with 0
  warnings. After adding `docs/ANU60_LIVE_SLACK_PROOF_PLAN.md`, the acceptance
  map, and its doc-contract guard, the local contract suite passed
  (`111 passed`), the Slack bridge contract/rendering validators passed, and the
  ANU-60 proof-packet validator passed. The broad no-live Slack expansion gate
  passed `36/36` in
  `artifacts/anu60_expansion_gate_after_acceptance_map.json`. Sibling
  `keystone-slack` focused bridge tests also passed (`8` tests) for direct
  RSS/preprints `human_summary`, conversational Business Research routing,
  metadata suppression, timeout failure wording, and blocked preflight
  rendering.
- The focused sibling timeout fixture pair also passed independently (`2`
  tests) for structured timeout failure text and process-group cleanup:
  `python3 -B -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group tests.test_app_mentions.SlackAppMentionTests.test_business_agents_streaming_run_command_timeout_kills_group`.
- Live proof checklist: after explicit approval for Slack posting and any API
  spend, run no more than these bounded probes before reassessing:
  0. No-live proof handoff: `npm run eval:slack:anu60-preflight`.
  1. Direct named-agent ask: `@KNI business research analyst "research Suki AI
     for a concise source-backed fit check; include visible source URLs; do not
     draft outreach, send, post elsewhere, schedule, or write files."`
  2. Conversational named-agent ask: `@KNI could the business research analyst
     research Nabla for a concise answer-first fit check? Include visible source
     URLs and keep this read-only.`
  3. Blocked/missing-context ask: `@KNI gmail triage summarize the selected
     email thread` from a thread with no selected Gmail context, or the closest
     existing Slack fixture path that produces the Gmail context-required
     blocker.
  4. Timeout/failure wording only if a safe forced-timeout env override is
     available; otherwise rely on the sibling process-group timeout fixture
     until a non-disruptive live failure probe is approved.
  Passing evidence requires answer-first `human_summary` rendering, no
  provider/model/timing metadata ahead of the answer, conversational wording
  reaching KBA rather than bridge rejection, blocked output titled and worded as
  blocked rather than started/completed, no external writes/sends, and captured
  Slack permalinks plus local run ids for each probe. Use
  `docs/ANU60_LIVE_SLACK_PROOF_PLAN.md` as the focused live-proof packet and
  `docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md` as the capture form.

### P2 - CONTEXT-UX-001: Direct context-agent summaries use a different section contract than Slack synthesis

- Found: 2026-06-20 00:00 EDT
- Fixed: 2026-06-20
- Status: fixed
- Area: context-agent CLI/Slack summaries, `src/keystone_agents/cli.py`,
  `src/keystone_agents/response_synthesis.py`, `tests/test_cli.py`,
  `tests/test_response_synthesis.py`.
- Issue: Direct context-agent fallback summaries used plain `Answer:` and
  `Detailed answer:` headings, while the shared Slack/user-facing synthesis
  renderer used bold `*Answer:*` and `*Detailed Summary:*` sections with
  metadata placed after the main narrative. That left context-agent outputs
  less consistent and could make references, blockers, and approval notes read
  like part of the answer.
- Evidence: `_context_agent_human_summary()` now renders bold `*Answer:*`,
  `*Detailed Summary:*`, and `*Useful references:*` sections with blank-line
  separation. The focused CLI tests assert the section contract for Drive,
  Airtable records, Zotero references, and feed/preprint-style item summaries.
- Impact: Operators reviewing Slack results have to distinguish content,
  references, blockers, and workflow notes by convention instead of by a stable
  renderer-owned contract. This is especially noisy for Airtable, Google
  Workspace, Zotero, RSS, and preprint context handoffs where "what was found"
  should stay separate from "what needs attention" and audit details.
- Fix: Updated `_context_agent_human_summary()` to match the Slack synthesis
  contract: bold `Answer`, bold `Detailed Summary`, a visually separate
  `Useful references` section, and only minimal blockers/approval notes after
  the main answer.
- Validation: `.venv/bin/python -m pytest tests/test_cli.py -k
  "context_agent_human_summary"` passed on 2026-06-20.

### P2 - DOC-ASSET-001: Generated architecture visual is referenced before the release surface is complete

- Found: 2026-06-20 00:00 EDT
- Fixed: pending
- Status: open
- Area: README/docs release surface, generated visual assets,
  `README.md`, `docs/INDEX.md`, `docs/VISUAL_CONTEXT.md`,
  `docs/assets/kba-current-agent-architecture.svg`,
  `scripts/render_agent_architecture_diagram.py`.
- Issue: README and visual-context docs now point to the generated current
  architecture visual and regeneration script, but the asset and generator are
  still untracked in the current worktree.
- Evidence: `git status --short --branch` shows tracked modifications to
  `README.md`, `docs/INDEX.md`, and `docs/VISUAL_CONTEXT.md`, while
  `docs/assets/kba-current-agent-architecture.svg` and
  `scripts/render_agent_architecture_diagram.py` are untracked. The README and
  docs refer directly to `docs/assets/kba-current-agent-architecture.svg` and
  the generator command.
- Impact: A partial publish could merge docs that reference a missing SVG or a
  regeneration command that does not exist in GitHub. That would break the
  documentation path operators use for architecture review and future agent
  context.
- Expected fix: Before publishing, either include the generated SVG and script
  together with their tests, or revert the docs to the previous committed
  visual reference. Add a lightweight doc asset/link check if this visual is
  expected to remain a generated release artifact.
- Validation: Run `git status --short`, `git diff --check`, and the architecture
  diagram generator/test path after the release-surface decision.

### P2 - TEST-HYGIENE-001: Focused tests pass while surfacing serializer and SQLite resource warnings

- Found: 2026-06-20 00:00 EDT
- Fixed: pending
- Status: open
- Area: test hygiene, Pydantic serialization, SQLite connection lifecycle,
  `src/keystone_agents/automation_inventory.py`,
  `src/keystone_agents/schemas/orchestrator.py`,
  `src/keystone_agents/storage/sqlite_store.py`, `tests/test_cli.py`,
  `tests/test_automation_control.py`.
- Issue: Focused tests still pass while emitting Pydantic serializer warnings
  and repeated unclosed SQLite connection `ResourceWarning`s.
- Evidence: `.venv/bin/python -m pytest -q
  tests/test_automation_control.py::test_weekly_dry_run_uses_save_without_live_flags
  tests/test_cli.py::test_cli_ask_preflight_blocked_omits_raw_workflow_state
  -W always` passed, but warned that `AutomationSpec.metadata` expected `str`
  while receiving `{"last_stage": "dry-run"}`, warned that
  `workflow_state_summary` expected `OrchestratorWorkflowStateSummary` while
  receiving a dict, and emitted repeated unclosed `sqlite3.Connection`
  resource warnings from the CLI preflight-blocked path.
- Impact: The warning stream can hide real schema drift and connection leaks,
  and a future `-W error` or stricter CI profile would fail on tests that
  currently look green. The serializer warnings also mean JSON/persistence
  output may not match the intended Pydantic contract.
- Expected fix: Align the affected schema fields with the actual payload shapes
  or coerce inputs before serialization, and close or context-manage SQLite
  connections opened during CLI preflight/blocked-path tests. Keep the fix
  focused on type/resource correctness rather than suppressing warnings.
- Validation: Re-run the focused command above with `-W error::UserWarning
  -W error::ResourceWarning`, then run the relevant CLI and automation test
  modules.

### P1 - EVAL-RUNTIME-001: Slack eval run IDs are not reconstructable from canonical WorkItem state

- Found: 2026-06-18 15:34 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: Slack eval persistence, WorkItem storage, `promptfoo/eval_database.py`,
  `src/keystone_agents/cli.py`, `src/keystone_agents/slack_actions.py`,
  local live `#evals` diagnostics.
- Issue: Recent saved Slack eval rows contain WorkItem-shaped run IDs, but the
  local canonical WorkItem store in this checkout has no corresponding WorkItem
  rows, events, agent run logs, or tool events. That leaves the eval dashboard
  with a case/run shell but no durable state to inspect when a live Slack agent
  call blocks or behaves unexpectedly.
- Evidence: `.keystone/promptfoo/human-reviews.sqlite` has recent Slack eval
  rows such as `wi_f086921d4d3d42bea4e28453102d677f`,
  `wi_d5e9b55a0c20473ca7e05021f4143f20`, and
  `wi_71ffc05cb839487787586550c0e4df98`. In the local Keystone stores,
  `.keystone/keystone.sqlite3` currently reports `0` rows in `work_items`,
  `work_item_events`, `agent_runs`, `agent_run_logs`, and `tool_events`; the
  only `.local/keystone-agents.sqlite` file is empty. The Slack eval row can
  show status, route, source counts, and a compact summary, but not the
  full blocker, gate, context pack, or manager-loop timeline.
- Impact: Recent execution issues cannot be root-caused from saved local state
  after the fact. A reviewer can see that a Slack eval was `blocked` or had a
  warning, but cannot reliably answer which deterministic gate blocked it,
  whether a specialist tool was called, whether context adaptation dropped
  fields, or whether a manager-loop transition happened correctly.
- Expected fix: Make Slack eval row creation preserve a reconstructable
  diagnostic link to canonical state. Either write the WorkItem and event
  timeline into the same configured SQLite database used for eval review, store
  the WorkItem database path/run snapshot in the eval evidence, or persist a
  compact redacted WorkItem diagnostic bundle with blockers, gates, context
  pack type, manager-loop steps, and tool-call summaries. Add a readiness check
  that flags Slack eval rows whose `work_item_id` cannot be resolved to either
  canonical WorkItem state or an approved redacted diagnostic snapshot.

### P1 - EVAL-RUNTIME-002: Slack eval rows still lack run-mode, model, and search-provider provenance

- Found: 2026-06-18 15:34 EDT
- Fixed: pending
- Status: open
- Area: Slack eval provenance, `src/keystone_agents/cli.py`,
  `src/keystone_agents/slack_actions.py`, `promptfoo/eval_database.py`,
  eval dashboard filtering and live-run review.
- Issue: The Slack eval schema now has normalized `run_mode`,
  `model_provider`, `model_name`, `search_provider`, and provider-sequence
  fields, but current saved Slack eval rows are still blank in those fields.
  The evidence payloads also carry blank model/search provenance.
- Evidence: A local query over `.keystone/promptfoo/human-reviews.sqlite`
  showed all `126` rows in `slack_eval_runs` have empty `run_mode`,
  `model_provider`, and `search_provider`. The latest row,
  `slack_business_research_analyst_using_the_selected_slack_thread_as_001`
  at `2026-06-18T17:31:40Z`, records status `done` and a WorkItem id, but
  blank run/model/search fields. Recent trace diagnostics also report
  `has_model_metadata=false` for Slack eval manual summaries.
- Impact: Live Slack execution issues are hard to separate from dry-run or
  fixture behavior. The dashboard cannot reliably answer whether a run used
  live SDK, live search, which model/provider was selected, whether Gemini or
  OpenAI fallback happened, or whether a search provider failure belongs to the
  run being reviewed.
- Expected fix: Populate Slack eval provenance from the resolved run request,
  model config, live flags, retrieval diagnostics, and `WorkflowRunResult`
  metadata instead of relying only on optional nested `model`/`retrieval`
  payloads. Add dashboard/readiness checks that warn or fail for live/manual
  Slack eval rows with blank run mode or provider provenance, and add focused
  tests for CLI/app-mention and selected-message eval row creation.

### P2 - EVAL-RUNTIME-003: Blocked Slack eval runs do not expose blocker diagnostics

- Found: 2026-06-18 15:34 EDT
- Fixed: pending
- Status: open
- Area: Chief of Staff Slack eval runs, eval evidence payloads, blocker
  rendering, trace diagnostics.
- Issue: Recent Chief-of-Staff Slack eval rows are saved with status
  `blocked`, but the eval evidence does not expose a stable `block_kind`,
  `block_reason`, blocker code, next safe action, or diagnostic category. The
  row can therefore look like a clean no-warning run even though the user-facing
  execution did not complete.
- Evidence: Rows for
  `slack_chief_of_staff_summarize_these_remaining_eval_gaps_using_001`
  and `slack_chief_of_staff_make_a_decision_log_from_these_001` are saved as
  `blocked` with `thread_fetch_status=ok`, `warning_count=0`, and compact
  evidence showing only route, status, source counts, and summary hashes. The
  visible `result_summary` is truncated before the actual blocker explanation.
- Impact: A live reviewer cannot tell whether a blocked Chief call was an
  expected safety gate, missing selected context, missing specialist-tool
  readiness, approval boundary, source-sufficiency failure, or a runtime bug.
  This weakens the follow-up queue because blocked runs are not grouped by
  actionable failure mode.
- Expected fix: Extend Slack eval evidence and manual trace summaries with
  blocker diagnostics for blocked runs: `block_kind`, `block_reason`,
  blocker codes, next safe action, readiness-gate names, and a compact
  diagnostic category. Dashboard views should treat `status=blocked` with no
  blocker metadata as an attention item even when `warning_count=0`.

### P1 - STATE-PATH-001: Business-state database path split makes Slack eval joins path-dependent

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `src/keystone_agents/config.py`,
  `src/keystone_agents/storage/sqlite_store.py`, Slack eval persistence,
  WorkItem storage, local runbooks and readiness checks.
- Issue: Keystone business state is not using one obvious canonical local
  SQLite path. The default state path still falls back to
  `sqlite:///keystone_agents.db`, while recent diagnostics and eval review
  paths also reference `.keystone/keystone.sqlite3`. This makes Slack eval to
  WorkItem joins path-dependent.
- Evidence: In this checkout, root `keystone_agents.db` is populated with
  `1151` WorkItems, `8486` WorkItem events, and `1378` artifacts.
  `.keystone/keystone.sqlite3` has zero WorkItems/events/log rows. The Slack
  eval database has `83` rows with a non-empty `work_item_id`; checking the
  wrong state DB makes all of them look unreconstructable.
- Impact: Execution diagnostics can point at the wrong database and report a
  missing WorkItem timeline even when state exists elsewhere. This wastes
  review time and can hide the actual failure mode behind a path/config issue.
- Expected fix: Canonicalize the local business-state DB path for Slack evals,
  or persist the exact business-state DB path/state snapshot with each Slack
  eval row. Add a readiness check that fails when eval rows reference WorkItems
  that do not resolve in the configured canonical state DB.

### P1 - WORKITEM-INTEGRITY-001: WorkItem child audit rows can orphan

- Found: 2026-06-18 15:44 EDT
- Fixed: pending
- Status: open
- Area: `src/keystone_agents/storage/sqlite_store.py`, WorkItem event/artifact
  persistence and repair diagnostics.
- Issue: WorkItem child tables can be written without a valid parent WorkItem.
  `work_item_events` and `work_item_artifacts` do not currently enforce parent
  integrity strongly enough for reliable execution audit.
- Evidence: The populated root `keystone_agents.db` currently has `41` orphan
  `work_item_events` across `16` missing WorkItem IDs. The orphan event types
  include execution-relevant entries such as `advance_started`,
  `skills_selected`, and `workflow_sdk_usage`.
- Impact: A Slack or CLI run can leave audit evidence that cannot be joined to
  the parent WorkItem. That undermines root-cause review, dashboard history,
  repair workflows, and any future automation that treats WorkItem state as
  canonical.
- Expected fix: Save parent WorkItems and child events/artifacts in one
  transaction where possible, verify parent existence before child inserts, and
  add a read-only orphan-audit/repair command. Consider SQLite FK enforcement
  where it can be introduced safely without breaking legacy rows.

### P1 - SLACK-ACTION-001: Direct Slack steering actions can dedupe failed executions as handled

- Found: 2026-06-18 15:44 EDT
- Fixed: pending
- Status: open
- Area: direct Slack WorkItem actions, `src/keystone_agents/slack_interactions.py`,
  action idempotency and retry handling.
- Issue: Direct steering actions such as more-research, find-contact, and
  run-again record the Slack action intent before the WorkItem advance finishes.
  If the process throws or dies after recording the intent, a Slack retry can
  hit the dedupe check and return duplicate/ignored even though the agent step
  did not complete.
- Evidence: The current tests cover successful dedupe behavior, but not failure
  after intent recording and before WorkItem advancement. The implementation
  writes action intent ahead of the execution boundary.
- Impact: A transient execution failure can become unretryable from Slack,
  leaving the operator with an apparent handled action and no completed agent
  step. This is a high-risk reliability issue for long-running or live actions.
- Expected fix: Make action idempotency two-phase: record `started`, then mark
  `completed` only after the WorkItem step succeeds. Retries should resume,
  report in-progress, or safely rerun when the prior attempt never reached a
  terminal state. Add tests for failure between intent save and advancement.

### P1 - SLACK-ACTION-002: Continue WorkItem Slack actions can drop live execution flags

- Found: 2026-06-18 15:44 EDT
- Fixed: pending
- Status: open
- Area: Chief-of-Staff continue action, `src/keystone_agents/slack_interactions.py`,
  `WorkflowRunRequest` construction and live-mode consistency.
- Issue: The direct `continue_work_item` path can run Orchestrator preflight
  under live-mode environment settings, but the actual `WorkflowRunRequest`
  does not consistently forward `live_search` and `live_sdk`. It can therefore
  continue the WorkItem in fixture/dry-run mode while adjacent Slack action
  paths use live flags.
- Evidence: Existing tests assert that preflight metadata is attached, but do
  not verify that the forwarded `WorkflowRunRequest` preserves live execution
  flags for the specialist step.
- Impact: A Slack continuation can look like it ran the same execution mode as
  the initial live request while silently changing retrieval/model behavior.
  That creates inconsistent outputs and misleading eval/runtime diagnostics.
- Expected fix: Pass the resolved live-search/live-SDK settings through every
  Slack continuation/direct-action path and persist those flags in WorkItem
  events. Add regression coverage that `continue_work_item` preserves live
  flags and remains no-send/no-write.

### P1 - EVAL-RUNTIME-004: Provider invocation mode is not authoritative in eval output

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `promptfoo/providers/keystone_agent_provider.py`,
  `promptfoo/assertions/kba_slack_invariants.py`, live eval compaction and
  safety assertions.
- Issue: Promptfoo provider invocation can pass `--live-sdk` or live-search
  options, but the compact top-level eval output derives `live_sdk` and
  `live_search` from returned manager metadata. If the agent output omits or
  misreports that metadata, a live invocation can be reported as dry-run or
  no-live.
- Evidence: Current assertion logic inspects the compacted output flags. The
  provider has access to the invocation config, but that invocation mode is not
  treated as the authoritative source or checked against returned metadata.
- Impact: A paid/live run can look safer and cheaper than it was, weakening
  no-live eval guarantees and making cost/rate-limit diagnosis unreliable.
- Expected fix: Record provider invocation mode separately from agent-reported
  mode, compare the two, and fail diagnostics on mismatch. Assertions should
  use invocation mode for safety/cost classification and use returned metadata
  as corroborating evidence.
- Fix: Provider compaction now records authoritative invocation metadata and
  Slack invariants fail mismatches while classifying live/dry-run safety from
  provider invocation flags.
- Validation: Focused pytest:
  `tests/test_promptfoo_framework.py::test_promptfoo_assertion_uses_provider_invocation_mode`.

### P1 - EVAL-RUNTIME-005: Missing side-effect evidence is treated as explicit no-write evidence

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: Promptfoo provider compaction, Slack invariant assertions, side-effect
  boundary instrumentation.
- Issue: When an agent response lacks a `side_effects` payload, provider
  compaction can still emit write/send flags as false and mark the evidence as
  complete. That conflates "agent explicitly reported no side effects" with
  "instrumentation was missing."
- Evidence: Subagent review found the provider fills no-write booleans from an
  absent side-effect object, and the invariant assertions accept the resulting
  false flags.
- Impact: No-send/no-write checks can pass without proof that the agent
  actually reported or preserved its side-effect boundary. That is a critical
  gap for Slack live execution, Gmail draft boundaries, and future API-backed
  evals.
- Expected fix: Add a tri-state side-effect evidence contract:
  `explicit_none`, `present_with_actions`, or `missing`. Treat missing
  side-effect instrumentation as an eval warning/failure for live or outbound
  capable routes, even when no write was observed.
- Fix: Provider side-effect compaction now emits tri-state evidence and
  invariant assertions reject missing or incomplete side-effect instrumentation.
- Validation: Focused pytest:
  `tests/test_promptfoo_framework.py::test_promptfoo_assertion_rejects_missing_side_effect_instrumentation`.

### P1 - WORKITEM-TELEMETRY-001: WorkItem events drop retry, fallback, retrieval, and cost details

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `src/keystone_agents/run.py`, `src/keystone_agents/workflow_runner.py`,
  `src/keystone_agents/costing.py`, WorkItem SDK/retrieval usage events.
- Issue: Detailed execution telemetry exists in lower-level SDK/search/cost
  paths, but WorkItem events only persist a reduced view. Successful rate-limit
  retries, model-provider fallback, retrieval provider errors/fallback flags,
  and cost component breakdowns can be lost at the WorkItem boundary.
- Evidence: `run_typed_sdk_agent()` can track retry counts and SDK run
  summaries; retrieval telemetry can include attempted/used providers and
  provider errors; cost estimation includes nested component and token
  breakdowns. WorkItem `workflow_sdk_usage` and `workflow_retrieval_usage`
  preserve only selected summaries, so recovered 429s, provider fallback, and
  cache/cost component shifts are not reliably queryable from WorkItem state.
- Impact: A run can appear clean even if it recovered after provider throttling,
  changed model providers, fell back between search lanes, or incurred unusual
  output/cached-token cost. This directly hurts robust live-agent debugging.
- Expected fix: Persist compact telemetry blocks on WorkItem events:
  `retry_state`, `model_attempts`, `fallback_used`, attempted/used search
  providers, provider errors, retrieval fallback flags, and nested cost/token
  components. Keep raw prompts/responses out of telemetry.
- Fix: WorkItem SDK and retrieval events now retain compact retry, fallback,
  model-attempt, provider-attempt/error, session, token, and cost-component
  metadata without raw prompts/responses.
- Validation: Focused pytest:
  `tests/test_workflow_runner.py::test_work_item_records_orchestrator_preflight_sdk_usage`
  and
  `tests/test_workflow_runner.py::test_live_sdk_opportunity_work_item_uses_named_agent_search_plan`.

### P1 - TRACE-OBS-002: Search provider failures are misclassified in trace diagnostics

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `src/keystone_agents/retrieval_policy.py`,
  `src/keystone_agents/trace_processor.py`, eval trace diagnostics.
- Issue: Hybrid search provider failures can be recorded with
  `{provider, error_type, message}`, but trace diagnostic classification can
  prioritize provider names over `error_type`. Real failure classes such as
  timeout or provider adapter errors can trend as generic provider labels.
- Evidence: Retrieval tests and telemetry use `search_provider_errors` with
  explicit `error_type` values. Trace issue classification currently risks
  grouping those by provider instead of failure kind.
- Impact: Repeated execution failures can look like "SearXNG issue" or
  "Agents web-search issue" rather than "timeout", "credential/config", or
  "adapter error", causing the team to fix the wrong layer.
- Expected fix: Update trace issue extraction to prefer `error_type` or a
  normalized failure category before provider name, and add tests using the
  actual hybrid-search telemetry shape.

### P2 - EVAL-RUNTIME-006: Analysis exclusions hide diagnostic evidence

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `promptfoo/eval_dashboard.py`, eval exclusions, readiness and run
  ledger diagnostics.
- Issue: Excluding a case from analysis can also remove its Slack, trace, and
  review evidence from key readiness/diagnostic views instead of only excluding
  it from score aggregates.
- Evidence: The dashboard payload filters excluded cases before several
  data-quality/workflow readiness views. This means a bad or failed live run
  can disappear from operational diagnostics once excluded.
- Impact: Exclusion can become a visual cleanup tool that hides the failure
  evidence needed to improve runtime robustness.
- Expected fix: Keep excluded cases visible in run ledger, trace diagnostics,
  Slack evidence, and human-review diagnostics with an
  `excluded_from_scoring` badge. Exclude only from scoring/trend aggregates.
- Fix: Dashboard diagnostics and run ledger now keep excluded cases visible
  with exclusion metadata while scoring/trend summaries use analysis-included
  cases only.
- Validation: Focused pytest:
  `tests/test_promptfoo_framework.py::test_eval_dashboard_analysis_exclusion_updates_database_and_analysis`.

### P2 - EVAL-RUNTIME-007: Slack source visibility treats missing source metadata as complete

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `promptfoo/eval_dashboard.py`, source/retrieval-tagged eval evidence
  checks.
- Issue: Source visibility checks can treat `source_count=0` and
  `visible_source_count=0` as complete when no explicit source-not-applicable
  reason is present.
- Evidence: Current dashboard/checklist logic can mark zero source metadata as
  complete for Slack evidence, and summary evidence readiness accepts
  nonnegative source counts.
- Impact: Retrieval/source instrumentation can be missing while the dashboard
  still presents the run as evidence-complete. That weakens source-backed
  research and opportunity evals.
- Expected fix: For source/retrieval-tagged cases, require `source_count > 0`
  and `visible_source_count > 0`, or require an explicit
  `source_not_applicable` reason. Treat `0/0` as attention by default.
- Fix: Source/retrieval-tagged Slack cases now require visible source metadata
  or an explicit source-not-applicable reason in case checklists and data
  quality diagnostics.
- Validation: Focused pytest:
  `tests/test_promptfoo_framework.py::test_source_tagged_dashboard_case_treats_zero_source_metadata_as_attention`.

### P2 - SDK-SESSION-JOIN-001: SDK session continuity is not joinable from eval rows

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `.keystone/sdk_sessions.sqlite3`, Slack eval evidence, manual run
  summaries, SDK session diagnostics.
- Issue: SDK session continuity state exists locally, but Slack eval rows and
  manual run summaries do not carry an audit-safe session join key.
- Evidence: `.keystone/sdk_sessions.sqlite3` contains `agent_sessions` and
  `agent_messages`, but current Slack eval `evidence_json` and manual
  `eval_trace_events` summaries do not include `session_id_hash`, session
  scope, or session source metadata.
- Impact: A live reviewer cannot tell whether a run reused expected session
  context, started a fresh session, or accidentally crossed WorkItem/Slack
  thread boundaries. This is especially risky while SDK sessions remain
  optional continuity rather than canonical business state.
- Expected fix: Persist audit-safe session metadata in Slack eval evidence and
  manual run summaries: session scope, source, hash, history limit, and related
  WorkItem ID. Never store raw session IDs or message bodies.
- Fix: Manual Slack eval summaries now carry audit-safe SDK session join
  metadata from evidence, including scope, source, session hash, history limit,
  and related WorkItem ID.
- Validation: Focused pytest:
  `tests/test_promptfoo_framework.py::test_slack_eval_run_records_joined_manual_run_summary_without_api`.

### P2 - SLACK-EVAL-DEDUP-001: Identical Slack eval attempts inflate run history

- Found: 2026-06-18 15:44 EDT
- Fixed: 2026-06-18
- Status: fixed
- Area: `promptfoo/eval_database.py`, Slack eval dashboard/readiness, retry
  accounting.
- Issue: Slack eval rows are upserted by case plus run/WorkItem ID. Because
  each Slack retry or rerun can produce a new WorkItem ID, identical attempts
  can inflate run history even when request, response, and status are unchanged.
- Evidence: The Business Research selected-thread case has many repeated Slack
  rows. The storage explorer found dozens of rows sharing the same nonblank
  request hash, response hash, and `status=done`, plus older rows before hashes
  were populated.
- Impact: Retry-heavy cases can look like repeated regressions or successful
  coverage growth when they are duplicate attempts. This distorts readiness,
  follow-up queues, and review prioritization.
- Expected fix: Add an `attempt_group_id` or duplicate marker derived from
  case/thread/request hash/response hash/status. Preserve all attempts for
  audit, but make dashboards and readiness distinguish duplicates from new
  failures.
- Fix: Slack eval rows now preserve every attempt while adding
  `attempt_group_id`, `duplicate_attempt`, and `duplicate_of_run_id` for retry
  grouping.
- Validation: Focused pytest:
  `tests/test_promptfoo_framework.py::test_slack_eval_duplicate_attempts_keep_audit_rows_with_group_marker`.

### P1 - ASK-SHAPE-001: Agent input contracts do not yet preserve diverse ask shape

- Found: 2026-06-18 14:08 EDT
- Fixed: 2026-07-12
- Status: complete
- Area: `src/keystone_agents/schemas/manual_request_plan.py`,
  `src/keystone_agents/models.py`,
  `src/keystone_agents/schemas/context_pack.py`, manual CLI, Slack,
  WorkItem, and direct SDK specialist inputs.
- Issue: The repo now declares typed input and output contracts for handoffs,
  but the input models still preserve only part of the operator's ask shape.
  `ManualRequestPlan` captures route, objective, target, count, constraints,
  Gmail/outreach fields, and live/search policy. Specialist SDK inputs then
  narrow that into route-specific fields such as company, topic, message,
  source context, or outreach goal. Cross-cutting dimensions such as evidence
  depth, source preference, strict-filter mode, requested output form, prior
  context dependency, permission state, cost mode, and stop condition remain
  implicit or unevenly represented.
- Evidence: `docs/REPO_REVIEW.md` and `docs/AGENTS_SDK_REVIEW.md` both call
  out ask-shape preservation as the next durable improvement. The current
  `ManualRequestPlan` has no first-class fields for `evidence_depth`,
  `source_type_preference`, `strict_filter_mode`, `output_form`,
  `prior_context_dependency`, `permission_state`, `cost_mode`, or
  `stop_condition`. Context packs carry type metadata and route-specific state,
  but not a shared ask-policy object that every specialist can consume.
- Impact: Diverse requests can still be routed to the correct specialist while
  losing the user's precision requirement. Examples include "exactly active
  official sources only", "quick read, do not deepen", "compare in a table",
  "use only the selected Gmail thread", "stop if no exact match", or "research
  first, draft only after approval." Correct input/output type names alone do
  not prevent broadening, over-retrieval, wrong output shape, or premature
  workflow continuation.
- Expected fix: Add a compact shared ask-shape contract and propagate it
  through `ManualRequestPlan`, Orchestrator result metadata, WorkItem context
  packs, specialist SDK inputs, and final synthesis inputs. Keep it orthogonal
  to route enums; prefer fields such as `ask_breadth`, `evidence_depth`,
  `source_type_preference`, `strict_filter_mode`, `output_form`,
  `prior_context_dependency`, `permission_state`, `cost_mode`, and
  `stop_condition`. Add focused tests for exact-match/no-broaden,
  quick-triage, table-format, selected-thread, and staged-workflow asks.
- Resolution: `AskShapePolicy` is a backward-compatible nested
  `ManualRequestPlan` contract for breadth, evidence depth, source preference,
  strict filtering, output form, prior-context dependency, permission state,
  cost mode, and stop condition. The deterministic planner extracts explicit
  generic cues; local constraints survive weaker LLM plan overrides; every
  specialist context pack receives the typed policy; and final synthesis sees
  it through the existing manual-plan payload. The policy never grants
  approval. Focused planner, preflight, context-pack, and synthesis tests pass.

### P1 - ASK-SHAPE-002: Specialist outputs do not consistently report request coverage or stop-condition status

- Found: 2026-06-18 14:08 EDT
- Fixed: pending
- Status: open
- Area: `src/keystone_agents/schemas/email_triage.py`,
  `src/keystone_agents/schemas/research.py`,
  `src/keystone_agents/schemas/opportunity.py`,
  `src/keystone_agents/schemas/outreach.py`,
  `src/keystone_agents/schemas/company_profile.py`, final synthesis and eval
  review.
- Issue: Specialist output schemas are structured and generally source-aware,
  but they do not share a small output-audit envelope that says how the
  interpreted ask was satisfied. Existing schemas expose fields such as summary,
  confidence, unknowns, limitations, source quality, recommended actions, or
  requested variants, but a reviewer cannot consistently tell whether the agent
  honored the requested output form, stopped at the requested boundary, avoided
  broadening, or left a specific ask dimension incomplete.
- Evidence: Research, opportunity, Gmail, company-profile, and outreach outputs
  have route-specific status and limitation fields. Only some paths preserve
  requested output format, and there is no shared `interpreted_request`,
  `request_coverage`, `stop_condition_status`, `output_form_status`, or
  `unmet_ask_dimensions` contract across agents.
- Impact: Agents may produce schema-valid results that look complete while
  missing the actual request shape. This is especially risky for exact-match
  search, source-required research, compact decision reads, selected-context
  Gmail asks, outreach revision requests, and multi-step workflows where a safe
  blocker is the correct answer.
- Expected fix: Add a shared lightweight output coverage object or consistent
  fields to each major specialist output: interpreted request, satisfied
  ask-shape dimensions, unmet dimensions, output form status, stop-condition
  status, and next safe action. Wire final renderers and evals to prefer a
  precise blocker or partial answer over a broadened substitute when coverage
  is incomplete.

### P2 - ADAPTER-ROBUSTNESS-001: Typed handoff metadata does not prove request-shape adaptation is lossless

- Found: 2026-06-18 14:08 EDT
- Fixed: pending
- Status: open
- Area: `src/keystone_agents/agent_registry.py`,
  `src/keystone_agents/schemas/handoff_types.py`,
  `src/keystone_agents/schemas/context_pack.py`,
  `src/keystone_agents/manual_request.py`, specialist SDK input builders and
  handoff validators.
- Issue: Handoff specs and context packs now record source output type, target
  input type, target output type, payload mode, parser status, and compatibility
  status. That metadata confirms that a boundary is typed, but it does not
  validate whether a rich Orchestrator plan or WorkItem context was adapted into
  the concrete specialist SDK input without dropping material ask-shape fields.
- Evidence: Registry handoffs use `payload_mode="adapted"` from
  `OrchestratorResult` into target context packs. Context packs declare the
  downstream input/output type and compatibility status. `HandoffTypeContract`
  records compatibility notes, but not adapter-required fields, missing fields,
  lossy fields, or clarification requirements.
- Impact: Connected workflows can pass type compatibility checks while losing
  important constraints such as "official sources only", "do not broaden",
  "draft only after approval", "use this selected thread", "produce table", or
  "stop after quick read." Debugging then sees a compatible handoff even though
  the specialist received an under-specified prompt.
- Expected fix: Add deterministic adapter validation for each route transition.
  The result should report `compatible`, `compatible_with_warnings`,
  `needs_clarification`, or `blocked`, plus missing required fields, lossy
  ask-shape fields, defaulted policy fields, and the safe next action. Add tests
  for Orchestrator-to-Gmail, Gmail-to-research, opportunity-to-research,
  research-to-outreach, and Chief-to-context-agent adaptation.

### P1 - HANDOFF-TYPES-001: Handoff specs do not declare typed input and output contracts

- Found: 2026-06-18 13:47 EDT
- Fixed: 2026-06-18 13:51 EDT
- Status: fixed
- Area: `src/keystone_agents/agent_registry.py`,
  `src/keystone_agents/schemas/orchestrator.py`, Orchestrator intended
  handoff metadata and AgentSpec cards.
- Issue: `AgentSpec` records each agent's `output_schema`, and SDK builders
  expose concrete Pydantic `output_type`, but the Orchestrator-facing
  `HandoffSpec` only contains route, agent name, builder, and prose
  description. It does not declare the input contract the target agent expects
  or the output contract it will return.
- Evidence: `AgentSpec.to_handoff_spec()` builds `HandoffSpec(route,
  agent_name, builder, description)` only. `HandoffSpec` has no
  `input_type`, `input_schema`, `output_type`, or `output_schema` field.
  Existing `input_type` appears on `OrchestratorArtifacts`, not on the handoff
  contract, and `output_type` appears in run/review traces rather than in the
  route-to-agent contract itself.
- Impact: The Orchestrator can list intended handoffs, but downstream code and
  evals cannot validate compatibility before execution. This makes connected
  workflows more likely to route a Gmail thread, source summary, WorkItem
  context pack, or Chief nested tool request into an agent that expects a
  different input shape.
- Expected fix: Extend the registry/handoff metadata with stable typed
  contracts, such as `input_contract_type`, `input_schema`, `output_contract_type`,
  `output_schema`, and contract version. Keep the fields derived from
  `AgentSpec` or route-specific typed input models, not hand-written prose.
  Add tests that every registered handoff exposes input/output contract metadata
  and that Orchestrator results preserve it in `intended_handoffs`.
- Fix: `HandoffSpec` now carries contract version, input/output contract names,
  input/output schema paths, and a reusable `HandoffTypeContract`. `AgentSpec`
  derives these fields for Orchestrator handoffs and agent cards, including
  Airtable, Google Workspace, and Zotero context-agent handoffs.
- Verification: `tests/test_agent_registry.py::test_orchestrator_handoff_specs_expose_typed_contracts`
  and full `tests/test_agent_registry.py` passed.

### P1 - HANDOFF-TYPES-002: WorkItem context-pack transitions lack expected next input/output type checks

- Found: 2026-06-18 13:47 EDT
- Fixed: 2026-06-18 13:51 EDT
- Status: fixed
- Area: `src/keystone_agents/schemas/context_pack.py`,
  `src/keystone_agents/work_items.py`, `src/keystone_agents/workflow_runner.py`,
  connected WorkItem manager-loop handoffs.
- Issue: WorkItem context packs are typed by route (`ResearchContextPack`,
  `OpportunityContextPack`, `OutreachContextPack`, `GmailContextPack`), but the
  pack itself does not declare which downstream input type it satisfies or what
  output type the current step is expected to produce. Manager-loop transitions
  therefore rely on route names, artifact presence, and ad hoc gates rather
  than a first-class handoff type compatibility check.
- Evidence: `ContextPackBase` carries `pack_type`, `route`, request text,
  artifacts, sources, blockers, gates, and readiness fields. It does not carry
  `satisfies_input_type`, `expected_output_type`, `next_input_type`, or a
  handoff contract version. `_context_pack_payload()` adds only
  `agent = pack.route.value`.
- Impact: A multi-step request can lose the intended shape between steps, such
  as Gmail-thread triage to research, Opportunity Scout source summary to
  Business Research, or approved research context to Outreach Composer. This is
  especially relevant to connected-workflow failures because "route is correct"
  is weaker than "this context pack satisfies the next agent's typed input."
- Expected fix: Add compact type metadata to context packs and WorkItem events:
  current `input_type`, produced `output_type`, downstream `required_input_type`,
  and compatibility status. Use deterministic gates to block or request
  clarification when a pack does not satisfy the next specialist's input
  contract. Add focused tests for Gmail-to-research, Scout-to-research,
  Research-to-outreach, and Chief-to-context-agent transitions.
- Fix: WorkItem context packs now carry `input_type`, `satisfies_input_type`,
  `expected_output_type`, `next_input_type`, `type_compatibility_status`, and a
  shared `handoff_type_contract` for Gmail, Research, Opportunity, and Outreach
  transitions. Serialized context payloads preserve the same metadata for
  downstream runner and eval inspection.
- Verification: `tests/test_context_packs.py::test_build_context_pack_for_route_selects_specialist_pack`,
  `tests/test_context_packs.py::test_context_pack_payload_preserves_ordered_sources_for_followups`,
  and full `tests/test_context_packs.py` passed.

### P2 - HANDOFF-TYPES-003: Cross-agent handoff validators report fields but not source/target type compatibility

- Found: 2026-06-18 13:47 EDT
- Fixed: 2026-06-18 13:51 EDT
- Status: fixed
- Area: `src/keystone_agents/schemas/handoff.py`,
  `src/keystone_agents/reporting.py`, handoff audit metadata and approval
  review.
- Issue: The cross-agent handoff validators check concrete payload fields and
  source IDs, but `HandoffContractResult` does not record the source
  `output_type`, target `input_type`, schema version, parser status, or whether
  the handoff is a native typed object versus a summarized/adapted payload.
- Evidence: `HandoffContractResult` includes contract name, from/to agent,
  required fields, optional fields, source IDs, unsupported claims, missing
  evidence, validity, issues, and audit notes. It does not include type
  compatibility metadata. By contrast, the Chief nested-specialist envelope
  does preserve `output_type` and parsed-output status, but that pattern is not
  generalized to ordinary handoff validators.
- Impact: Human approval review and eval dashboards can see that certain fields
  are present, but not whether the handoff crossed a schema boundary safely.
  That makes it harder to debug adapter drift, fixture-shaped payloads,
  summarized handoffs, or future context-agent writes.
- Expected fix: Add type compatibility metadata to `HandoffContractResult` and
  its approval metadata: `source_output_type`, `target_input_type`,
  `contract_version`, `payload_mode` (`native`, `adapted`, `summarized`,
  `malformed`), and `parsed_output_status`. Preserve the existing field/source
  checks, but make type mismatch an explicit error or warning depending on
  route. Add reporting coverage so Slack/eval review can surface this compactly
  without dumping raw payloads.
- Fix: Cross-agent `HandoffContractResult` now records the shared
  `HandoffTypeContract`, source output type, target input type, target output
  type, payload mode, parsed-output status, and compatibility status. Approval
  metadata surfaces the same compact fields. Chief-of-Staff nested specialist
  envelopes and specialist tools now use the same compatibility contract.
- Verification: `tests/test_handoff_contracts.py::test_pipeline_exposes_valid_handoff_contracts`,
  `tests/test_handoff_contracts.py::test_orchestrator_handoff_contract_accepts_explicit_sdk_handoff_metadata`,
  `tests/test_chief_of_staff.py::test_chief_specialist_tools_use_typed_context_input_contract`,
  `tests/test_chief_of_staff.py::test_nested_specialist_output_extractor_returns_reviewable_envelope`,
  full `tests/test_handoff_contracts.py`, and the focused Chief specialist-tool
  tests passed.

### P1 - RUNTIME-IMPORT-001: Zotero context tools create a fresh-process circular import

- Found: 2026-06-18 13:32 EDT
- Fixed: 2026-06-18 13:44 EDT
- Status: fixed
- Area: `src/keystone_agents/zotero_research.py`,
  `src/keystone_agents/tools/__init__.py`,
  `src/keystone_agents/tools/zotero_context_tools.py`, fresh CLI child imports.
- Issue: A fresh Python process cannot import `keystone_agents.manual_request`
  or `keystone_agents.zotero_research` directly because `zotero_research`
  imports `keystone_agents.tools.search_provider`, which executes
  `tools/__init__.py`, which imports `zotero_context_tools`, which imports
  unfinished symbols back from `zotero_research`.
- Evidence: `.venv/bin/python -c "import keystone_agents.manual_request"` and
  `.venv/bin/python -c "import keystone_agents.zotero_research"` both failed
  with `ImportError: cannot import name 'build_zotero_article_research_brief'
  from partially initialized module 'keystone_agents.zotero_research'`.
  Importing `keystone_agents.tools` alone still succeeds, which means the
  failure depends on import order and can escape normal tests.
- Impact: Fresh CLI child processes, ad hoc smoke checks, or sandboxed runners
  can fail before Orchestrator preflight or safety gates execute. This is
  especially risky for `@KNI` paths because `manual_request` is a core planning
  module.
- Expected fix: Break the package-level import cycle by keeping
  `tools/__init__.py` from eagerly importing Zotero context tools, or by moving
  the Zotero research imports inside the individual tool functions. Add a
  regression test that imports `keystone_agents.manual_request`,
  `keystone_agents.zotero_research`, and `keystone_agents.tools` in isolated
  fresh processes.
- Fix: `tools/__init__.py` now lazy-loads Zotero context exports instead of
  eagerly importing `zotero_context_tools`, breaking the fresh-process cycle
  while preserving package-level tool exports.
- Verification: `.venv/bin/python -c "import keystone_agents.manual_request;
  import keystone_agents.zotero_research; import keystone_agents.tools"` passed,
  and `tests/test_architecture.py::test_fresh_process_imports_core_zotero_paths_without_cycles`
  passed.

### P1 - REGISTRY-COVERAGE-001: New context agents are only partially propagated through registry, skill, and eval contracts

- Found: 2026-06-18 13:32 EDT
- Fixed: 2026-06-18 13:44 EDT
- Status: fixed
- Area: `src/keystone_agents/agent_registry.py`,
  `src/keystone_agents/skill_sets.py`, `src/keystone_agents/skills/*`,
  `evals/local/skill_task_matrix.jsonl`,
  `evals/local/skill_contracts.jsonl`, registry fingerprint tests.
- Issue: Airtable, Google Workspace, and Zotero context agents are now in the
  registry and intended handoff set, but the surrounding static contracts are
  incomplete or stale. The result is a mixed rollout where runtime surfaces,
  prompt contracts, skill coverage, and eval matrices disagree about what the
  registered agent set is.
- Evidence: Full pytest failed registry and prompt-contract checks:
  `test_context_agents_support_direct_writes_but_nested_tier_is_advisory`
  found an unexpected `google_drive_get_file_metadata` tool in the direct
  Google Workspace builder, `test_registered_agent_static_prefix_fingerprints_are_stable`
  found a changed tool-name fingerprint, new context-agent skills are missing
  required sections such as `## Flexible Behavior`, `skill_contracts.jsonl`
  lacks cases for the new specialist skills, and `skill_task_matrix.jsonl`
  still covers only the pre-existing six agents.
- Impact: Agent construction may be correct in one path but rejected by
  architecture/eval gates in another. More importantly, the repo loses its
  ability to tell whether new context agents are safe direct writers,
  Chief-owned advisory tools, or fully eval-ready specialists.
- Expected fix: Finish the context-agent rollout as one contract update:
  decide the intended direct tool set, refresh static fingerprints only after
  reviewing the tool surface, add missing skill sections, add skill-contract and
  skill-task eval rows for Airtable, Google Workspace, and Zotero, and run the
  focused registry/prompt/skill eval tests before the full suite.
- Fix: Airtable, Google Workspace, and Zotero specialist skills now include the
  required reasoning-contract sections, `skill_contracts.jsonl` includes
  focused coverage rows for all three context-agent specialist skills, and
  static registry fingerprints were refreshed after reviewing the direct tool
  surface.
- Verification: `tests/test_prompt_contracts.py::test_skill_bundles_define_reasoning_contracts_not_tools`,
  `tests/test_prompt_contracts.py::test_skill_contract_eval_cases_cover_shared_and_specialist_skills`,
  and `tests/test_agent_registry.py` passed.

### P1 - CONNECTED-WF-001: Connected multi-step requests route to clarification instead of preserving the safe workflow

- Found: 2026-06-18 13:32 EDT
- Fixed: 2026-06-18 13:44 EDT
- Status: fixed
- Area: `src/keystone_agents/orchestrator/routing.py`,
  `src/keystone_agents/manual_request.py`,
  `src/keystone_agents/slack_actions.py`, connected Gmail/research/opportunity
  and research-to-outreach workflows.
- Issue: Several safe connected-workflow prompts now fall into
  `clarification` instead of starting with the owning specialist and preserving
  the downstream draft-only or approval-gated steps. The approval wording is
  being treated as a stop condition too early, rather than as a downstream gate
  attached to the workflow.
- Evidence: Full pytest failed
  `test_gmail_first_cross_agent_request_preserves_downstream_workflow`,
  `test_research_then_outreach_request_routes_to_research_first`, the OR-2
  workflow preservation case, and the Slack modal Gmail workflow check. Focused
  rerun confirmed the Gmail consulting inquiry routes to `clarification`
  instead of `gmail_triage`, and `Research Mentavi and prepare draft-only
  outreach only after approval` routes to `clarification` instead of
  `business_research_analyst`.
- Impact: Natural Slack and CLI asks that should be safe read/research/draft
  workflows become generic clarification or malformed result payloads. This
  weakens the Orchestrator-first model because Python no longer preserves the
  intended specialist sequence, while the user still receives no external write
  or send.
- Expected fix: Treat approval-gated downstream wording as workflow metadata,
  not an immediate refusal, unless the user asks to send/post/write now. Restore
  deterministic connected-workflow planning for Gmail-first and research-first
  requests, attach approval/no-send gates to the later draft step, and ensure
  Slack modal results return the expected blocker payload shape when context is
  missing.
- Fix: Manual-plan routing now preserves Gmail-first workflows before applying
  the standalone outreach context refusal, and research-before-draft requests
  route to Business Research with a downstream draft-only outreach workflow and
  approval gates intact.
- Verification: `tests/test_orchestrator.py::test_gmail_first_cross_agent_request_preserves_downstream_workflow`
  and `tests/test_orchestrator.py::test_research_then_outreach_request_routes_to_research_first`
  passed.

### P1 - EVAL-UX-028: Strict eval readiness can pass while the dashboard endpoint is unreachable

- Found: 2026-06-18 13:32 EDT
- Fixed: 2026-06-18 13:44 EDT
- Status: fixed
- Area: `scripts/check_eval_slack_readiness.py`,
  `src/keystone_agents/eval_dashboard_health.py`,
  `tests/test_promptfoo_framework.py`, eval dashboard launchd readiness.
- Issue: Standalone strict readiness can still return overall `pass` when
  `http://127.0.0.1:8769/api/status` is unreachable and `port_open=false`, as
  long as launchd reports the dashboard service as running. That makes Slack
  dashboard links look ready when the endpoint is not actually reachable.
- Evidence: `.venv/bin/python scripts/check_eval_slack_readiness.py --strict
  --json` returned `"status": "pass"` and `"warning_count": 0`, while the eval
  dashboard readiness payload had `"port_open": false`,
  `"dashboard_reachable": false`, and `"health_reachable": false`. The current
  test `test_eval_dashboard_manager_allows_launchd_running_when_probe_blocked`
  encodes this pass behavior.
- Impact: The pre-live `#evals` gate can green-light a run whose dashboard and
  review links fail for the operator. This partially reopens the earlier
  dashboard readiness issue because launchd state is being treated as enough
  evidence of link usability.
- Expected fix: In standalone strict readiness, require a reachable health
  endpoint or an explicit wrapper pre-start mode. Launchd running can remain a
  diagnostic hint, but it should be a warning/failure when the endpoint is
  unreachable and the wrapper is not about to start the server. Update tests so
  only `--dashboard-server-will-start` allows a temporarily offline dashboard.
- Fix: Launchd-running without reachable dashboard health now reports a warning
  in readiness output, which makes `--strict` fail unless
  `--dashboard-server-will-start` explicitly declares wrapper-managed startup.
- Verification: `tests/test_promptfoo_framework.py::test_eval_dashboard_manager_warns_when_launchd_running_but_probe_blocked`
  and wrapper pre-start coverage passed. `.venv/bin/python
  scripts/check_eval_slack_readiness.py --strict --json` now fails with one
  dashboard warning when the endpoint is unreachable, while the same command
  with `--dashboard-server-will-start` passes.

### P1 - EVAL-UX-029: Human review storage still defaults missing safety to `pass`

- Found: 2026-06-18 13:32 EDT
- Fixed: 2026-06-18 13:44 EDT
- Status: fixed
- Area: `promptfoo/human_review.py`, `promptfoo/eval_dashboard_server.py`,
  human review persistence and scorecard safety.
- Issue: The UI and parser paths now require an explicit safety choice, but the
  underlying `HumanEvalReview` dataclass still defaults `safety` to `pass`, and
  the SQLite schema still declares `safety TEXT NOT NULL DEFAULT 'pass'`.
  Direct programmatic saves can therefore create positive safety reviews without
  an explicit reviewer decision.
- Evidence: A fresh local check constructed
  `HumanEvalReview(case_id='case_1', scores={...})`, called
  `save_human_review(...)`, and `list_human_reviews(...)` returned
  `safety == 'pass'`. The focused Promptfoo tests pass because parser and
  dashboard payload paths validate safety before construction, but the lower
  storage contract remains biased.
- Impact: Future scripts, migrations, or direct helper calls can silently
  create completed-looking positive scorecards, undermining human-review
  readiness and benchmark rollups.
- Expected fix: Remove the `pass` default from the dataclass/storage contract.
  Make `save_human_review()` reject missing or invalid safety regardless of the
  caller, migrate or tolerate legacy rows explicitly, and add a direct-save
  regression proving safety must be selected before persistence.
- Fix: `HumanEvalReview` no longer defaults missing safety to `pass`, and
  `save_human_review()` validates case ID, score presence/range, and explicit
  `pass`/`fail` safety before persistence. New SQLite tables default missing
  safety to an empty value instead of pass.
- Verification: `tests/test_promptfoo_framework.py::test_human_review_requires_explicit_safety`
  and `tests/test_promptfoo_framework.py::test_human_review_storage_requires_explicit_safety`
  passed.

### P1 - SECRET-HYGIENE-001: Test fixture secrets match live secret scanner patterns

- Found: 2026-06-18 13:32 EDT
- Fixed: 2026-06-18 13:44 EDT
- Status: fixed
- Area: `tests/test_promptfoo_framework.py`, `scripts/scan_repo_secrets.py`,
  publish and repository hygiene gates.
- Issue: Promptfoo live-policy tests use fake OpenAI key strings that match the
  repo secret scanner's `openai_api_key` pattern. The strings are fixtures, but
  the scanner is intentionally unable to infer that from the value alone.
- Evidence: Full pytest failed both
  `test_no_obvious_repo_secrets_present` and
  `test_scan_repo_secrets_reports_current_repo_clean`; the scanner reported
  three findings in `tests/test_promptfoo_framework.py`, including
  line 6151 with sample `sk-t***3456`.
- Impact: The local publish/backup gate is blocked and future real findings can
  be hidden by known fixture noise. Secret-hygiene tests should remain strict
  and boring.
- Expected fix: Replace fake key literals with scanner-safe placeholders that
  do not match live token patterns, or add a narrow fixture allowlist with exact
  path/line/sample justification. Prefer changing the fixture values so the
  scanner stays simple.
- Fix: Promptfoo live-policy fixture keys now use scanner-safe placeholders
  rather than fake `sk-...` strings, so the secret scanner remains strict
  without fixture allowlists.
- Verification: `.venv/bin/python scripts/scan_repo_secrets.py`,
  `tests/test_architecture.py::test_no_obvious_repo_secrets_present`, and
  `tests/test_scan_repo_secrets.py::test_scan_repo_secrets_reports_current_repo_clean`
  passed.

### P1 - EVAL-UX-001: `#evals` app mentions can route to unsupported `/kni`

- Found: 2026-06-14 10:39 EDT
- Fixed: 2026-06-14 11:12 EDT
- Status: fixed
- Area: sibling `keystone-slack` Slack app-mention routing,
  `docs/SLACK_BUSINESS_AGENT_MODE.md`, eval-channel live flow.
- Issue: Recent live `#evals` messages that visibly mention KNI were handled as
  unsupported `/kni` commands instead of the business-agent eval flow. This
  prevents a Slack run from being recorded and leaves the operator with a
  generic slash-command help response rather than a case dashboard or review
  link.
- Evidence: In `#evals` thread `1781206953.875749`, the prompt
  `opportunity scout: eval case slack_agents_sdk_course_001` received
  `Unsupported KNI Command` with `Requested: /kni opportunity scout...` and no
  Slack eval run was recorded for `slack_agents_sdk_course_001`. A separate
  score-template thread `1781202864.139419` hit the same unsupported `/kni`
  path.
- Expected fix: In the Slack bridge, normalize ChatGPT-forwarded or app-mention
  eval prompts so `#evals` business-agent asks enter the same Orchestrator-first
  KBA child-run path as manual `@KNI` prompts. Add a live-shape fixture that
  includes the `Sent using ChatGPT` footer and verifies the prompt records a
  Slack eval run with dashboard/review links instead of falling through to slash
  command help.
- Fix: The sibling Slack `/kni` business-agent delegation now passes a bounded
  `SlackBusinessAgentContext` into `handle_business_agents_request`, preserving
  channel, request timestamp, and user context for eval-channel hidden metadata
  and follow-up scoring/status resolution. A live-shape slash-command
  regression with `opportunity scout: eval case ...` and the ChatGPT footer now
  verifies the request delegates to business agents instead of unsupported help.
- Verification: KBA focused eval tests passed; sibling Slack focused
  `/kni` delegation and app-mention scorecard tests passed; eval readiness
  preflight passed for the eval bridge path with unrelated warnings for
  dashboard reachability and scope metadata.

### P1 - EVAL-UX-002: Eval threads expose run and route mismatch instead of a clean case journey

- Found: 2026-06-14 10:39 EDT
- Fixed: 2026-06-14 11:12 EDT
- Status: fixed
- Area: Slack eval rendering, WorkItem manager loop result framing, eval footer
  handoff.
- Issue: The live `#evals` thread for a Business Research Analyst case quickly
  surfaced an Opportunity Scout continuation and route metadata. The operator
  sees multiple statuses, blockers, route transitions, and action buttons before
  a clear case/run/review handoff, making it hard to know which result should be
  scored.
- Evidence: In `#evals` thread `1781202023.470699`, the root ask says
  `business research analyst: eval case slack_behavioral_health_rfp_001`, but
  the first KNI reply reports a blocked Opportunity Scout continuation for
  WorkItem `wi_e59cb39aeac24ec49d012b2f27cb244b`; a later reply says manager
  loop routes `business_research_analyst -> opportunity_scout`, and the final
  visible result remains `Status: blocked` with Opportunity Scout buttons.
- Expected fix: For eval-channel runs, render one primary scoring target per
  root prompt: resolved case id, resolved run id, selected scoring response,
  route transitions only as compact diagnostics, and explicit review/dashboard
  links. If the manager loop changes specialist route or blocks, mark the run as
  blocked/not-score-ready and avoid showing downstream action buttons as the
  main scoring surface.
- Fix: KBA eval Slack results now clear `slack_actions` and
  `slack_overflow_actions` when an eval record is attached, so downstream
  WorkItem actions do not become the primary eval scoring surface. The visible
  summary keeps the case id, run id, dashboard link, review form link, and
  natural same-thread score/status prompts as the scoring handoff.
- Verification: Focused `tests/test_slack_agent_actions.py` coverage verifies
  eval runs still record the case and evidence but no longer return Slack action
  buttons in the eval result payload.

### P2 - EVAL-UX-003: Score-template fallback requires exact IDs the thread does not reliably expose

- Found: 2026-06-14 10:39 EDT
- Fixed: 2026-06-14 11:12 EDT
- Status: fixed
- Area: Slack fallback scoring commands, in-thread eval status, human review
  handoff.
- Issue: The fallback score-template path asks the operator to provide
  `case_id`, `run_id`, and `agent_name`, but live thread output can make the
  correct run ambiguous or unavailable. This leads to placeholder score-template
  attempts and unsupported-command replies instead of a saved scorecard.
- Evidence: In `#evals` thread `1781202023.470699`, the operator tried
  `eval score template case <case_id> run <run_id> agent <agent_name>` and then
  `run sbar_14ff...`, while the root run previously exposed
  `sbar_bd1fe1d83aca4f93864a03c0618c5dfc`. The later standalone score-template
  thread `1781202864.139419` also routed to unsupported `/kni`.
- Expected fix: Prefer same-thread context inference for fallback scoring:
  `@KNI score this eval` or the existing natural score reply should resolve the
  latest eligible case/run from local Slack eval records. If multiple run
  candidates exist, return a short disambiguation with exact run ids and status
  instead of requiring the operator to reconstruct IDs manually.
- Fix: Scorecard guidance now consistently points to natural same-thread
  requests (`@KNI can you give me a scorecard for this eval?` and
  `@KNI how is this eval doing?`). Explicit placeholder score-template requests
  such as `case <case_id> run <run_id>` now block with guidance to use the
  same-thread resolver instead of producing a fake template with unresolved
  identifiers.
- Verification: Focused CLI tests cover placeholder blocking, wrapped Slack
  follow-up scorecard parsing, natural score replies, and natural status
  inference from Slack thread context.

### P2 - EVAL-UX-004: Dashboard manager can report ready while the localhost dashboard is unreachable

- Found: 2026-06-14 10:39 EDT
- Fixed: 2026-06-14 11:05 EDT
- Status: fixed
- Area: `scripts/manage_eval_dashboard.sh`,
  `src/keystone_agents/eval_dashboard_health.py`,
  `scripts/check_eval_slack_readiness.py`.
- Issue: The eval dashboard health surface can say the manager is ready even
  when `http://127.0.0.1:8769/dashboard` is not reachable. This creates a poor
  handoff from Slack links to the Keystone Eval Dashboard because the operator
  may click a link that cannot load even after readiness passes with warnings.
- Evidence: `./scripts/manage_eval_dashboard.sh status` reported the launchd
  job as running, but the dashboard was not reachable at the canonical URL. The
  readiness JSON classified the eval dashboard manager check as pass while
  noting `port_open: false` and `dashboard port was not reachable from this
  process`.
- Expected fix: Split manager availability from dashboard reachability in the
  readiness contract. Strict live-test readiness should fail when the dashboard
  URL is unreachable, and `manage_eval_dashboard.sh status` should surface
  actionable remediation based on logs, stale launchd state, or port-bind
  failures before Slack links are treated as usable.
- Fix: Dashboard readiness now keeps manager checks separate from endpoint
  reachability and returns a warning when the manager exists but
  `http://127.0.0.1:8769/dashboard` is unreachable, which makes strict
  readiness fail before Slack links are trusted. `manage_eval_dashboard.sh
  status` now reports restart remediation, stale PID files, and port listener
  state when health fails.
- Verification: `.venv/bin/python -m pytest tests/test_promptfoo_framework.py`;
  `.venv/bin/python -m pytest tests/test_slack_agent_actions.py`;
  `.venv/bin/python -m py_compile promptfoo/eval_dashboard.py
  promptfoo/eval_dashboard_server.py scripts/check_eval_slack_readiness.py
  src/keystone_agents/eval_dashboard_health.py`; `zsh -n
  scripts/manage_eval_dashboard.sh`.

### P2 - EVAL-UX-005: Review form defaults can bias human scores

- Found: 2026-06-14 10:39 EDT
- Fixed: 2026-06-14 16:18 EDT
- Status: fixed
- Area: `promptfoo/eval_dashboard.py`,
  `promptfoo/eval_dashboard_server.py`, `promptfoo/human_review.py`,
  human review UI and Slack score templates.
- Issue: When a case has a recorded response but no saved human review, every
  score selector defaults to `4` and safety defaults to `pass`. That makes the
  fastest path through the review form a positive scorecard, even if the user
  intended to inspect first or mark a problematic run.
- Evidence: `scoreOptions()` and `_review_html()` both select
  `Number(selected ?? 4)`, while `save_human_review_payload()` accepts any
  submitted score values. This affects the dashboard scoring panel and
  standalone `/review?case=...` form.
- Expected fix: Default unscored review controls to `tbd` even when a recorded
  response exists, require an explicit user-selected value for each submitted
  score dimension, and keep safety unset until the reviewer chooses pass or
  fail. Existing saved reviews should still preload their stored values.
- Fix: Dashboard and standalone review forms now default unscored score and
  safety controls to `tbd`, block client-side submission until every score
  dimension and safety choice is explicit, and preserve saved review values
  when present. The server-side save path now rejects missing score dimensions
  and missing safety instead of defaulting safety to `pass`.
- Verification: `.venv/bin/python -m pytest tests/test_promptfoo_framework.py`;
  `.venv/bin/python -m pytest tests/test_slack_agent_actions.py`;
  `.venv/bin/python -m py_compile promptfoo/eval_dashboard.py
  promptfoo/eval_dashboard_server.py scripts/check_eval_slack_readiness.py
  src/keystone_agents/eval_dashboard_health.py`; `zsh -n
  scripts/manage_eval_dashboard.sh`.
- Regression evidence at 2026-06-14 12:36 EDT: the current Slack score template
  path still pre-fills `safety: pass` in `build_slack_review_template()`. The
  dashboard and standalone review form no longer default safety, but a user
  asking for the Slack score template can still paste a positive safety choice
  without actively selecting pass/fail.
- Remaining expected fix: keep all human-review entry surfaces consistent.
  Slack-generated score templates should leave safety blank or `tbd`, and
  tests should verify the template does not bias the safety decision while the
  save path still rejects missing safety.
- Regression fix: `build_slack_review_template()` now leaves `safety:` blank
  instead of pre-filling `pass`, so Slack-generated score templates require the
  reviewer to actively choose pass or fail.
- Regression verification: Ran `.venv/bin/python -m py_compile
  promptfoo/human_review.py tests/test_promptfoo_framework.py`; ran focused
  human-review and CLI score-save tests covering unbiased template generation,
  explicit safety rejection, parsed explicit safety, and natural/thread score
  save paths.

### P1 - EVAL-UX-006: Strict eval test-server readiness can block its own dashboard start

- Found: 2026-06-14 11:24 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `scripts/run_eval_slack_test_server.py`,
  `scripts/check_eval_slack_readiness.py`,
  `scripts/manage_eval_dashboard.sh`, live Slack eval launcher.
- Issue: `run_eval_slack_test_server.py --strict-readiness` runs
  `check_eval_slack_readiness.py --strict` before the dashboard server starts.
  Strict readiness treats dashboard reachability warnings as failures, so a
  stopped dashboard can make the wrapper exit before it reaches
  `dashboard_server_main()`, even though that wrapper is the path intended to
  start the local dashboard for the live Slack test.
- Evidence: The wrapper builds and runs the readiness command before starting
  the dashboard server. The readiness script marks warnings as failures in
  strict mode, and the dashboard manager check now warns when
  `port_open=false`. A local JSON readiness run passed with warnings while
  reporting the canonical dashboard URL unreachable.
- Expected fix: Split standalone strict readiness from wrapper pre-start
  readiness. Either start and health-check the dashboard before strict checks
  that require reachability, or add an explicit wrapper-only mode that accepts
  the pre-start dashboard state while preserving standalone strict failure when
  Slack dashboard links are expected to be usable.
- Fix: `check_eval_slack_readiness.py` now supports an explicit
  `--dashboard-server-will-start` mode. The live Slack test-server wrapper uses
  that mode before starting the dashboard, so an offline dashboard URL is
  allowed only for the wrapper pre-start check; standalone strict readiness
  still treats unresolved warnings as failures when Slack links are expected to
  be usable.
- Verification: Focused Promptfoo readiness/launcher tests passed, including
  strict standalone warning failure, wrapper pre-start dashboard allowance, and
  wrapper argv clearing. `.venv/bin/python scripts/check_eval_slack_readiness.py
  --json --dashboard-server-will-start` passed with the dashboard manager check
  marked pass and only unrelated Slack bridge/scope metadata warnings.
- Regression evidence at 2026-06-14 12:10 EDT: the current wrapper still builds
  `readiness_command = [sys.executable, "scripts/check_eval_slack_readiness.py"]`
  and appends `--strict` plus `--live-slack-probe`, but it does not append
  `--dashboard-server-will-start`. Current launcher tests assert the readiness
  command suffix is only `["--strict", "--live-slack-probe"]`, so the documented
  wrapper-only pre-start mode is not enforced by the current worktree.
- Remaining expected fix: make the strict test-server wrapper pass
  `--dashboard-server-will-start` for its pre-start readiness run, keep
  standalone strict readiness failing when the dashboard URL is unreachable, and
  update launcher coverage so it proves the wrapper uses the pre-start allowance
  only in the server-launch path.

### P2 - EVAL-UX-007: Slack/CLI score-save path can store incomplete human scorecards

- Found: 2026-06-14 11:24 EDT
- Fixed: 2026-06-14 11:29 EDT
- Status: fixed
- Area: `src/keystone_agents/cli.py`, `promptfoo/human_review.py`,
  `scripts/add_promptfoo_human_review.py`, `promptfoo/eval_dashboard.py`,
  `promptfoo/eval_dashboard_server.py`.
- Issue: The dashboard review form now requires every score dimension and an
  explicit safety choice, but the Slack/CLI score-save path still uses
  `parse_human_review()`, which accepts any review containing at least one
  valid 0-5 score. That can save a partial rubric while the dashboard and
  readiness language imply complete scorecards.
- Evidence: `_eval_score_save_payload()` parses and saves natural Slack score
  replies directly. `parse_human_review()` raises only when no scores are
  present. Existing CLI eval-status coverage saves a review with only
  `accuracy` and `relevance` and reports `average 4.5/5`, while the dashboard
  server save path rejects missing dimensions.
- Expected fix: Decide whether paid/API readiness requires complete human
  scorecards across all save paths. If yes, add complete-score validation for
  Slack/CLI/manual score saves and update tests/docs. If partial reviews remain
  allowed, label them as partial and exclude them from completed scorecard
  readiness counts until the full rubric is filled.
- Fix: Slack/CLI/manual score saves now use the same complete-scorecard
  contract as the dashboard form: every `SCORE_DIMENSIONS` rubric dimension must
  be present and safety must be explicitly `pass` or `fail`. Existing complete
  natural Slack score replies still save normally; incomplete replies now block
  before writing a human review row.
- Verification: Focused parser, CLI score-save/status, dashboard payload, and
  readiness score-save tests passed. Regression coverage now rejects incomplete
  Slack scorecards and missing safety while preserving complete scorecard
  rollups and dashboard/status summaries.

### P1 - EVAL-UX-008: Human review saves are not validated against the recorded Slack run

- Found: 2026-06-14 11:29 EDT
- Fixed: 2026-06-14 11:41 EDT
- Status: fixed
- Area: `promptfoo/eval_dashboard.py`,
  `promptfoo/eval_dashboard_server.py`, `promptfoo/eval_database.py`,
  `promptfoo/human_review.py`, analysis rollups.
- Issue: The Slack-to-dashboard data flow is covered at the case level, but the
  human-review save path does not validate that the submitted `run_id`, agent,
  and Slack thread belong to a recorded Promptfoo or Slack response for the same
  case. `save_human_review_payload(..., require_recorded_response=True)` only
  checks that some response exists for the case, then stores the submitted review
  fields. Analysis later aggregates human reviews by case/day/agent without
  confirming the review is attached to the specific Slack run it was meant to
  score.
- Evidence: Slack runs persist `case_id`, `run_id`, `agent`,
  `slack_thread_ts`, response summary, and evidence in `slack_eval_runs`.
  Dashboard and standalone forms post `case_id`, `run_id`, `agent`,
  `slack_thread_ts`, scores, safety, and notes to `/api/human-review`. The
  server constructs `HumanEvalReview` directly and saves it; current tests cover
  recorded-response presence, complete scores, ledgers, and data-quality gates,
  but do not reject a review whose run/thread/agent does not match the saved
  Slack run for that case.
- Expected fix: Add a run-level review target contract before paid/API evals:
  resolve the review target from the latest eligible saved Slack or Promptfoo
  run, include channel/thread identity in form payloads, validate submitted
  `run_id`, agent, and Slack thread against the stored run for the case, and
  surface mismatches in data-quality/readiness instead of counting them as
  completed human scorecards.
- Fix: `validate_human_review_target()` now checks submitted human-review
  targets against saved Promptfoo or Slack responses before save. Dashboard form
  saves and natural Slack/CLI score saves reject mismatched run IDs, agents, or
  Slack thread timestamps instead of storing a detached scorecard.
- Verification: Focused dashboard-server and CLI regressions now cover both
  matching and mismatched Slack review targets. The broader Promptfoo/CLI eval
  suite passed with 49 tests.

### P1 - EVAL-UX-009: Analysis exclusion does not cover Slack runs or human-review rows

- Found: 2026-06-14 11:31 EDT
- Fixed: 2026-06-14 11:41 EDT
- Status: fixed
- Area: `promptfoo/eval_database.py`, `promptfoo/eval_dashboard.py`,
  `promptfoo/eval_dashboard_server.py`, analysis inclusion controls.
- Issue: The analysis inclusion toggle only applies to Promptfoo machine result
  rows keyed by `eval_id` and `case_id`. Slack run records and human-review rows
  remain included in the run ledger and human trend rollups even when an eval
  case/result is marked as a duplicate retry, problem run, or otherwise excluded
  from analysis. This can make the human trend and agent score analysis disagree
  with the operator's inclusion decision before paid/API evals.
- Evidence: `set_promptfoo_analysis_exclusion()` requires a matching
  `promptfoo_case_results` row and writes only to
  `promptfoo_analysis_exclusions`. `_promptfoo_analysis()` filters machine
  `promptfoo_case_results` through that table, but reads all
  `human_eval_reviews` without joining any exclusion state. `_eval_run_ledger()`
  also lists all Slack and human rows. Existing exclusion coverage verifies
  `run_trends` and `case_trends` are empty after excluding a Promptfoo row, but
  does not cover Slack or human-review exclusion behavior.
- Expected fix: Extend the analysis inclusion contract to the full eval data
  model. Either add row-level inclusion state for Slack runs and human reviews,
  or derive their inclusion from the validated review target/run. Human trend
  rollups, agent score trends, run ledger summaries, readiness counts, and
  exports should visibly exclude or flag duplicate/problem Slack and human rows
  consistently with Promptfoo rows.
- Fix: Dashboard analysis now derives included cases once and applies that
  inclusion state to summary counts, readiness gates, Promptfoo trends, Slack
  run ledger rows, human ledger rows, human-review trends, and agent score
  trends. Exclusion flags remain visible on case records even when the
  Promptfoo prompt snapshot is stale.
- Verification: Regression coverage now imports Promptfoo results, records a
  Slack run and human review for the same case, excludes the case, and verifies
  Slack/human ledger and trend data are removed from analysis rollups.

### P1 - EVAL-UX-010: Normal Slack/app-mention eval runs save less evidence than selected-message runs

- Found: 2026-06-14 11:32 EDT
- Fixed: 2026-06-14 11:41 EDT
- Status: fixed
- Area: `src/keystone_agents/cli.py`,
  `src/keystone_agents/slack_actions.py`, `promptfoo/eval_database.py`,
  `promptfoo/eval_dashboard.py`, Slack eval readiness/data-quality checks.
- Issue: The selected-message Slack action path records rich eval evidence for
  saved Slack runs, including thread fetch status, message count, warning count,
  cost profile, source counts, SDK cost/cache summaries, response hash, and a
  structured evidence payload. The normal Slack/app-mention child-run path in
  the CLI records only case/run/thread/request/summary. That means the workflow
  that most closely matches live `@KNI` eval usage can enter the same database
  with missing evidence needed for paid/API readiness, cost comparison, and
  failure diagnosis.
- Evidence: `_record_eval_run_for_slack_bridge()` in `slack_actions.py` builds
  `_slack_eval_evidence()` and passes its fields into `record_slack_eval_run()`.
  `_record_eval_slack_run_if_requested()` in `cli.py` calls
  `record_slack_eval_run()` without thread fetch status, message count, warning
  count, cost profile, source counts, SDK cost/cache values, response hash, or
  evidence. CLI eval tests assert case/run/thread links, while selected-message
  tests assert thread evidence and response hash. Dashboard data-quality checks
  then warn on missing Slack thread evidence for such rows.
- Expected fix: Normalize Slack eval evidence capture across both entrypoints.
  The CLI/app-mention path should extract safe thread context metadata,
  cost/cache summaries, source counts, warning counts, route/status, response
  hash, and redacted evidence from the same bounded context/result sources used
  by the selected-message path, then add focused tests proving app-mention eval
  rows satisfy the dashboard data-quality checks before live paid/API runs.
- Fix: The normal CLI/app-mention Slack eval path now records structured
  evidence, route/status, context policy, thread fetch status, thread message
  count, warnings, source counts, cost/cache summaries, response hash, and
  WorkItem identity when saving `slack_eval_runs`.
- Verification: CLI eval regressions now assert app-mention and hidden eval
  rows persist thread evidence, response hashes, and
  `keystone.slack.eval_evidence.v1` payloads. Readiness also passes the
  app-mention thread flow and dashboard workflow checks.

### P2 - EVAL-UX-011: Promptfoo eval database lacks normalized prompt/model provenance

- Found: 2026-06-14 11:36 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `promptfoo/eval_database.py`, `promptfoo/eval_dashboard.py`,
  `scripts/check_eval_slack_readiness.py`, Promptfoo/API eval reporting.
- Issue: The local static and benchmark eval paths record prompt-version
  provenance, but the Promptfoo dashboard database does not normalize prompt
  versions, prompt metadata, model/provider labels, git revision, or live-run
  labels for imported Promptfoo runs or saved Slack eval rows. Future API-backed
  evals need those fields to compare regressions across prompt changes, model
  changes, and live/dry-run settings without relying on a movable raw result
  file path or unindexed nested JSON.
- Evidence: `docs/EVALS.md` says eval output includes `prompt_metadata` and
  `prompt_versions` so results can be traced from agent to prompt version to
  dataset row, and `benchmark_tracking.py` stores `prompt_versions_json` for
  benchmark case scores. In contrast, `promptfoo_eval_runs` stores run summary
  fields plus `result_path`, and `promptfoo_case_results` stores score fields
  plus raw `vars_json` and `output_json`; neither schema has normalized
  prompt/model/provider provenance columns. `slack_eval_runs` captures Slack
  evidence and cost fields, but also lacks prompt/model provenance fields.
- Expected fix: Extend Promptfoo/Slack eval persistence before future API runs
  to capture normalized run provenance: prompt version references, prompt
  metadata hashes or references, model provider/name, live/dry-run mode, search
  provider mode where relevant, git revision, and import/source result path.
  Add dashboard/readiness checks and focused tests that prove API-backed eval
  runs can be grouped and filtered by prompt version and model/provider.
- Fix: Promptfoo eval runs, Promptfoo case rows, and Slack eval rows now carry
  normalized prompt versions, prompt metadata/hash, model provider/name,
  run mode, search provider sequence, git revision, and run label. Promptfoo
  imports extract provenance from compact provider output or case vars, and
  Slack eval recorders pass through optional model/search provenance.
- Verification: Regression coverage imports a compact provider result with
  model/search/prompt provenance, records a linked Slack run, and verifies the
  normalized Promptfoo and Slack rows deserialize with the expected fields.

### P1 - EVAL-UX-012: Promptfoo live SDK path lacks budget and max-case guards

- Found: 2026-06-14 11:39 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `promptfoo/providers/keystone_agent_provider.py`,
  `promptfooconfig.yaml`, `package.json`, live/API eval runner setup.
- Issue: The default Promptfoo configuration is dry-run, but the provider will
  pass `--live-sdk` whenever a case or provider config sets `live_sdk: true`.
  There is no Promptfoo-side live-run budget, max-case count, allowlist,
  expected spend estimate, or explicit approval reference before the full
  Promptfoo suite can be run through paid model calls. That is acceptable for
  the current dry-run suite, but it is not ready for future API-backed evals.
- Evidence: `promptfoo/providers/keystone_agent_provider.py` appends
  `--live-sdk` when `vars.live_sdk` or `config.live_sdk` is truthy; otherwise it
  appends `--no-live-sdk`. `promptfooconfig.yaml` currently sets
  `live_sdk: false`, and no committed Promptfoo cases opt in to live SDK, but
  `package.json` runs the full Promptfoo config with no budget or case-limit
  arguments. `docs/EVALS.md` Phase 6 says the live runner should require
  explicit live flags, `KEYSTONE_OPENAI_API_KEY`, budget, max-case-count, and
  model/provider arguments; `promptfoo/eval_manifest.yaml` says live reviewed
  cases should run only when budget and approvals are set.
- Expected fix: Before enabling paid Promptfoo/API evals, add a separate live
  eval runner or provider guard that requires an explicit budget amount, max
  case count, approved case allowlist, model/provider label, and approval/run
  label. It should fail closed when `live_sdk: true` is present without those
  controls, estimate or record expected spend, and keep the existing dry-run
  `npm run eval:promptfoo*` commands incapable of accidentally running the full
  suite through live SDK calls.
- Fix: The Promptfoo provider now fails closed for `live_sdk=true` unless the
  case/config supplies approval, budget, max-case count, approved case
  allowlist, run label, and model provider/name. The committed Promptfoo config
  remains explicit dry-run/no-live-search by default.
- Verification: Provider regression coverage confirms missing live controls
  block before `ask_agent.py` runs, while a fully controlled live-search config
  passes `--live-sdk`, `--live-search`, budget metadata, and model/search
  provenance into the compact output.

### P2 - EVAL-UX-013: Slack eval context files lack retention and redaction guardrails

- Found: 2026-06-14 11:40 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `src/keystone_agents/slack_actions.py`,
  `promptfoo/providers/keystone_agent_provider.py`, eval Slack context storage.
- Issue: The selected-message eval path writes the selected Slack message and
  bounded thread context to disk under `artifacts/slack_contexts`, and the
  Promptfoo provider writes `slack_context` fixtures to a temporary directory,
  but there is no eval-specific retention, cleanup, redaction, or sensitivity
  classification contract for those context files. This is separate from SDK
  trace capture: the raw context file can persist message text before a future
  API-backed run ever reaches tracing.
- Evidence: `write_selected_message_context_file()` serializes the full
  `SlackSelectedMessageContext`, including `selected_message.text` and
  `thread_messages`, to `artifacts/slack_contexts`. The Promptfoo provider
  creates `kba-promptfoo-slack-*` directories with `tempfile.mkdtemp()` and
  writes `slack-context.json` when a case includes `slack_context`; the provider
  does not remove that directory after `ask_agent.py` exits. Current docs say
  Slack context should be bounded, read-only, and sanitized for Promptfoo
  fixtures, but they do not define retention or redaction guarantees for the
  files created during live eval workflows.
- Expected fix: Add an eval context-file lifecycle contract before richer live
  Slack/API evals: store only the minimum bounded context needed for review,
  redact or hash sensitive fields where possible, tag context files with
  sensitivity and eval run metadata, clean temporary Promptfoo context
  directories after provider execution, and add an operator cleanup/readiness
  check for persisted `artifacts/slack_contexts` files that are no longer needed
  for review.
- Fix: Selected-message context files now include local-only sensitivity,
  retention, and bounded raw-text metadata. Promptfoo provider context fixtures
  are tagged similarly and their temporary `kba-promptfoo-slack-*` directories
  are removed after the child process unless explicitly kept for debugging.
- Verification: Provider coverage asserts the temporary context file exists
  during child execution, includes sensitivity metadata, and is deleted after a
  failed provider run.

### P1 - EVAL-UX-014: Promptfoo assertions do not prove no external writes

- Found: 2026-06-14 11:42 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `promptfoo/providers/keystone_agent_provider.py`,
  `promptfoo/assertions/kba_slack_invariants.py`, live/API eval side-effect
  gates.
- Issue: The live API eval requirements say benchmark runs must not send email,
  post Slack messages, schedule follow-ups, create Gmail drafts, or write
  external systems without scoped approval. The current Promptfoo compact
  payload and invariant only check `can_send_email`, `send_enabled`, and whether
  `send_email` appears in `forbidden_actions`. That proves the email-send flag
  is off, but it does not prove the run avoided Slack posts, Gmail drafts,
  labels, calendar/scheduling writes, CRM writes, file writes, or other
  side-effect paths.
- Evidence: `kba_slack_invariants.py` implements `enforce_no_send` by checking
  `payload.get("can_send_email")`, `payload.get("send_enabled")`, and the
  presence of `send_email` in `forbidden_actions`. The provider compact output
  includes those email fields, but does not normalize `slack_post_allowed`,
  `external_write_performed`, Gmail draft/label flags, CRM write blockers, or a
  general side-effect ledger. `docs/EVALS.md` Phase 6 requires the live runner
  to preserve draft-only/no-send/no-external-write behavior and to avoid Gmail,
  Slack, CRM, scheduling, and other external write paths.
- Expected fix: Before paid API evals, extend the compact Promptfoo payload and
  invariant contract with a general side-effect proof: normalized booleans or a
  ledger for email sends, Gmail drafts/labels, Slack posts, CRM writes,
  scheduling/calendar writes, file/external writes, approval references, and any
  blocked write attempts. The default assertion should fail if any unapproved
  side-effect flag is true or if the run cannot provide side-effect evidence
  for live/API eval mode.
- Fix: Compact Promptfoo output now includes
  `keystone.promptfoo.side_effects.v1` evidence for email, Gmail, Slack, CRM,
  calendar, and external file writes. The default Slack invariant fails when
  side-effect evidence is missing/incomplete or any unapproved external-write
  flag is true.
- Verification: Assertion coverage now accepts safe no-write payloads and
  rejects a payload that reports a Slack post/external write.

### P1 - EVAL-UX-015: Promptfoo/Slack evals do not write to the benchmark store

- Found: 2026-06-14 11:43 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `promptfoo/eval_database.py`, `src/keystone_agents/benchmark_tracking.py`,
  `scripts/promptfoo_eval_db.py`, `scripts/summarize_benchmark_results.py`.
- Issue: Static and local eval runs can be recorded in the benchmark store, but
  Promptfoo imports and Slack eval runs are stored only in the Promptfoo eval
  database. A future live/API eval run through the Slack-shaped Promptfoo path
  therefore cannot be compared directly with static/local baseline runs through
  `scripts/summarize_benchmark_results.py` unless an operator manually converts
  rows or runs a separate benchmark harness.
- Evidence: `scripts/run_evals.py` and `scripts/run_local_evals.py` call
  `record_eval_summary()` when `--record-benchmark` is passed, which writes
  `benchmark_runs` and `benchmark_case_scores` in `.keystone/state/benchmark_evals.sqlite`.
  Promptfoo import code writes `promptfoo_eval_runs` and
  `promptfoo_case_results`; Slack eval saves write `slack_eval_runs`; neither
  path imports `benchmark_tracking` or records to the benchmark store.
  `docs/EVALS.md` Phase 6 says live runs should record to the benchmark store
  and be comparable with static/local baselines, while `docs/PROMPTFOO_EVALS.md`
  says budgeted live benchmarks should use the existing benchmark store.
- Expected fix: Add an explicit bridge from imported Promptfoo/Slack eval rows
  to the benchmark store before future API runs. It should map case IDs,
  agents, scores, prompt/model provenance, human/machine score status, and run
  labels into benchmark rows without raw private content, and expose a command
  or guarded import path so live Slack-shaped evals can be compared with
  `baseline-static` and `baseline-local` runs through the existing benchmark
  summary tooling.
- Fix: Added `record_promptfoo_eval_to_benchmark()` and
  `scripts/promptfoo_eval_db.py record-benchmark` to export imported Promptfoo
  rows plus linked Slack eval rows into the existing benchmark store without
  raw request/response bodies.
- Verification: Regression coverage records Promptfoo and Slack eval rows to a
  temp benchmark DB and verifies benchmark case scores preserve case IDs,
  agents, prompt versions, and provenance keys for comparison with baseline
  runs.

### P1 - EVAL-UX-016: Promptfoo provider failure payloads can persist raw child output

- Found: 2026-06-14 11:45 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `promptfoo/providers/keystone_agent_provider.py`,
  `.keystone/promptfoo/latest-eval.json`, Promptfoo logs and error imports.
- Issue: Failed Promptfoo provider runs return raw child `stdout` and `stderr`
  in the provider output payload. During future live/API evals, child output can
  include the original prompt, Slack context snippets, provider diagnostics, or
  exception text. Because Promptfoo JSON results and logs are stored locally
  under `.keystone/promptfoo/`, a failed run can retain more raw eval context
  than the compact successful-output contract intends.
- Evidence: `call_api()` runs `scripts/ask_agent.py` with
  `capture_output=True`. On non-zero exit it calls `_provider_error(...,
  stderr=completed.stderr, stdout=completed.stdout)`. On JSON parse failure it
  also includes full `completed.stdout` and `completed.stderr`. `_provider_error()`
  copies all non-empty extras into the JSON `output` string. The npm
  `eval:promptfoo:json` path writes Promptfoo results to
  `.keystone/promptfoo/latest-eval.json`, and the docs state Promptfoo run
  history, results, cache, and logs are kept under `.keystone/promptfoo/`.
- Expected fix: Redact and cap Promptfoo provider failure payloads before live
  API evals. Store structured failure kind, return code, timeout flag, elapsed
  seconds, and short redacted excerpts only; remove raw prompt/context text and
  secret-shaped values from stdout/stderr. Add regression coverage for non-zero
  exit, timeout, and non-JSON stdout paths, and include a data-quality check that
  failed Promptfoo/API eval rows do not contain raw Slack context or secrets.
- Fix: Provider errors now emit structured failure metadata with capped,
  redacted `stdout_excerpt`/`stderr_excerpt` fields. Raw `stdout`/`stderr`
  keys are no longer persisted, context-bearing lines are replaced, and
  secret-shaped values are redacted.
- Verification: Provider failure coverage asserts non-zero child output omits
  raw stdout/stderr, redacts secret-shaped tokens, strips raw Slack context
  lines, and still reports a structured failure kind.

### P1 - EVAL-UX-017: Promptfoo live-search activation is implicit and not case-scoped

- Found: 2026-06-14 11:46 EDT
- Fixed: 2026-06-14 11:58 EDT
- Status: fixed
- Area: `promptfoo/providers/keystone_agent_provider.py`,
  `src/keystone_agents/cli.py`, `src/keystone_agents/config.py`,
  Promptfoo live/API eval configuration.
- Issue: The Promptfoo provider exposes a `live_sdk` switch, but it does not
  expose an explicit `live_search` switch, search-provider selection, or
  per-case retrieval budget. When `--live-sdk` is passed, the CLI can enable
  live search implicitly from `KEYSTONE_LIVE_MODE`, `KEYSTONE_DRY_RUN`, and
  `KEYSTONE_ENABLE_LIVE_RESEARCH`. That makes future Promptfoo/API eval
  retrieval behavior depend on ambient shell state instead of the case row or
  run label.
- Evidence: `keystone_agent_provider.call_api()` appends `--live-sdk` or
  `--no-live-sdk`, but never appends `--live-search` or a search provider
  option. `_run_ask()` sets `live_search = args.live_search or (live_sdk and
  cli_default_live_research())`; `cli_default_live_research()` reads
  `KEYSTONE_ENABLE_LIVE_RESEARCH` and live-mode env state. Existing CLI coverage
  verifies that named-agent asks receive `--live-search` when those env flags
  are set. The Promptfoo dry-run suite has a case proving live-search wording
  remains dry-run-safe, but there is no corresponding case-scoped live-search
  opt-in contract for API evals.
- Expected fix: Before paid Promptfoo/API evals, make live retrieval explicit in
  the eval-run contract. Provider config and/or case vars should declare
  `live_search`, search provider, fallback provider, max search calls/results,
  extraction policy, and budget label; the provider should fail closed if
  ambient env would enable live search without the case/run opting in. Persist
  the resolved retrieval mode and provider sequence in eval provenance so runs
  are reproducible.
- Fix: Promptfoo provider config/case vars now support explicit `live_search`,
  search provider/fallback provider, max search calls, and max search results.
  The provider passes `--live-search` only for explicit opt-in, forces
  `KEYSTONE_ENABLE_LIVE_RESEARCH=false` for non-live-search cases, and blocks
  ambient live-search activation when live SDK is enabled without case opt-in.
- Verification: Provider regression coverage confirms ambient live-search env
  blocks without case opt-in, and explicit live-search config passes the CLI
  flag, provider env, and provenance fields.

### P1 - EVAL-UX-018: Promptfoo case intake lacks an automated sanitation gate

- Found: 2026-06-14 11:48 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/tests/*.yaml`, `scripts/check_eval_slack_readiness.py`,
  `promptfoo/eval_dashboard.py`, live Slack ask promotion workflow.
- Issue: The eval docs require committed Promptfoo cases and promoted live
  Slack asks to be sanitized before use, but the current eval workflow does not
  appear to have an automated case-intake gate for raw private names, email
  bodies, PHI, credentials, customer context, or secret-like values. Before
  future API evals, relying only on manual redaction creates a high-risk path
  where an unsanitized Slack ask or fixture row can become model input,
  dashboard HTML, Promptfoo JSON output, or benchmark metadata.
- Evidence: `docs/PROMPTFOO_EVALS.md` says real Slack asks should be captured
  only after redaction and converted into sanitized Promptfoo rows. `docs/EVALS.md`
  says fixture inputs must keep raw private messages, credentials, PHI, and
  unredacted customer data out. Current Promptfoo tests/readiness checks inspect
  case inventory, prompt text presence, and whether source/thread cases include
  self-contained context, but the searched eval workflow did not show a
  Promptfoo-specific sanitizer or readiness check that scans `user_input`,
  `slack_context.thread_messages`, source excerpts, required terms, or notes for
  common private-data and secret patterns before API runs.
- Expected fix: Add a Promptfoo/eval sanitation gate before paid API evals. It
  should scan committed case YAML and any promoted Slack ask artifact for
  high-signal secrets, email addresses, phone numbers, raw Gmail headers,
  patient/PHI-like content, private customer names, and unapproved raw Slack
  excerpts; emit case-id-specific blockers; and integrate with strict readiness
  so API evals cannot start until every selected case has passed redaction
  review or carries an explicit sanitized-fixture exemption.
- Fix: Added a Promptfoo case sanitizer, wired it into Slack eval readiness,
  scanned both `tests:` mapping files and top-level-list files, and failed
  unsafe case content unless explicitly exempted.
- Verification: Regression coverage exercises unsafe top-level-list fixtures;
  readiness now reports the committed Promptfoo case sanitation check.

### P1 - EVAL-UX-019: Promptfoo live evals inherit the full parent environment

- Found: 2026-06-14 11:49 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/providers/keystone_agent_provider.py`,
  `src/keystone_agents/config.py`, `src/keystone_agents/model_provider.py`,
  live/API eval child-process setup.
- Issue: Promptfoo provider child runs inherit the full parent environment.
  Future live/API evals should use the repo-local `KEYSTONE_OPENAI_API_KEY` and
  only the credentials explicitly allowed for that eval lane, but the current
  provider passes all ambient env vars through to `scripts/ask_agent.py`. That
  can make a paid eval depend on unrelated local credentials or live-integration
  toggles from the operator shell.
- Evidence: `keystone_agent_provider.call_api()` builds `env = os.environ.copy()`
  and only adds `KEYSTONE_PROMPTFOO_EVAL` plus `KEYSTONE_EVAL_SURFACE` before
  launching the child process. The model provider correctly requires
  `KEYSTONE_OPENAI_API_KEY` for OpenAI live model execution, but the broader
  config also reads ambient `GEMINI_API_KEY`, `LITELLM_BASE_URL`,
  `SLACK_BOT_TOKEN`, Gmail/Google credential env vars, and live-mode toggles.
  `EVAL-UX-017` covers implicit live-search activation; this issue is broader
  because any inherited credential or live toggle can change what tools are
  reachable during an API eval.
- Expected fix: Add a Promptfoo/API eval environment allowlist before paid runs.
  The provider should construct a minimal child env from explicit eval-run
  config: repo-local model credentials, model/provider labels, selected
  retrieval credentials only when the case opts in, eval DB paths, and safe
  tracing/readiness flags. It should clear unrelated Gmail, Slack, CRM, Google,
  generic `OPENAI_API_KEY`, and gateway credentials unless the run contract
  explicitly allows them, and record the allowed credential categories in
  sanitized provenance.
- Fix: Promptfoo child runs now use a minimal allowlisted environment,
  preserve repo-local OpenAI credentials only for approved OpenAI live evals,
  avoid generic `OPENAI_API_KEY` and unrelated Slack/Gmail credentials, and
  record allowed credential categories in provenance.
- Verification: Provider regression coverage confirms the live child env keeps
  `KEYSTONE_OPENAI_API_KEY` plus selected search credentials and excludes
  unrelated ambient credentials.

### P1 - EVAL-UX-020: Promptfoo successful-result storage can retain raw API eval text

- Found: 2026-06-14 11:57 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/eval_database.py`, `promptfoo/eval_dashboard.py`,
  `promptfoo/providers/keystone_agent_provider.py`, future Promptfoo/API eval
  storage and review exports.
- Issue: Successful Promptfoo imports store full case vars and full provider
  response payloads in the local eval database, then dashboard views and exports
  surface prompt and response text directly. That is acceptable for local
  sanitized dry-run fixtures, but future paid/API evals need a stronger
  storage-mode distinction so raw prompts, raw model responses, Slack snippets,
  drafts, or private request content do not become the durable default for
  successful runs.
- Evidence: `_insert_promptfoo_case_result()` upserts `eval_cases.user_input`
  from `vars.user_input` and stores `json.dumps(vars_)` plus
  `json.dumps(response)` into `promptfoo_case_results.vars_json` and
  `output_json`. `eval_case_status()` rehydrates those JSON columns. The
  dashboard maps `user_input` to `prompt`, derives `response_text` from
  Promptfoo output, renders both in case/database tables, and includes
  `latest_response` in exports. `docs/EVALS.md` says benchmark storage should
  track observed output keys, not raw observed payloads, drafts, email bodies,
  or private request content; `docs/PROMPTFOO_EVALS.md` says real Slack asks
  should be captured only after redaction.
- Expected fix: Add an API-eval-safe storage mode for Promptfoo results. For
  live/API evals, persist compact redacted prompt and response summaries,
  hashes, schema keys, assertion outcomes, source counts, route/tool metadata,
  and provenance instead of full raw `vars_json`/`output_json`. Keep any raw
  local-review payload behind an explicit local-only mode with retention
  controls, and make dashboard/export UI label redacted vs local-review rows.
  Add a readiness/data-quality check that blocks paid API evals when selected
  cases or successful result rows would store raw private inputs or raw observed
  outputs by default.
- Fix: Promptfoo live/API result imports now default to `api_redacted` storage,
  persist prompt/response hashes plus compact redacted summaries and safe
  metadata, and expose storage mode/hash fields to dashboard data-quality
  checks.
- Verification: Import/benchmark regression coverage proves live Promptfoo rows
  do not retain raw email or secret-shaped values and data-quality checks
  recognize API-safe storage.

### P2 - EVAL-UX-021: Trace readiness reports env state instead of effective SDK trace safety

- Found: 2026-06-14 12:05 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/eval_dashboard.py`, `src/keystone_agents/model_provider.py`,
  `src/keystone_agents/sdk.py`, trace readiness/data-quality reporting.
- Issue: The Traces tab and data-quality card report trace readiness from
  display-oriented environment values instead of the effective Keystone SDK
  trace configuration. This can show `Sensitive capture: not_set` even when
  Keystone live/local run config defaults `trace_include_sensitive_data` to
  `False`, and it can make the top Traces cards feel repetitive or less useful
  for deciding whether future API eval traces are safe and diagnosable.
- Evidence: `_trace_dashboard_summary()` reads
  `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` into `sensitive_data_env` and
  the Traces tab renders that as the `Sensitive capture` card. The dashboard
  readiness check only verifies whether `KEYSTONE_TRACE_PROCESSOR=eval_summary`
  is enabled. In contrast, `get_trace_config()` reads
  `KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA` with default `False`, and
  `build_live_run_config()` passes the effective
  `trace_include_sensitive_data` value into the Agents SDK `RunConfig`.
- Expected fix: Replace the Traces tab top boxes with API-eval-specific
  readiness signals derived from effective runtime state, not only raw env
  strings. Suggested cards: effective sensitive capture state, trace join
  coverage by case/run, unjoined or missing trace events, last trace flush/write
  status, and API-eval-safe storage mode. The data-quality/readiness check
  should pass only when the effective trace config is safe, the eval summary
  processor is enabled for the selected API-eval lane, and the dashboard labels
  whether rows are sanitized trace summaries rather than raw prompts/responses.
- Fix: Trace readiness now derives effective sensitive-capture and tracing
  state from Keystone trace config, uses the repo's
  `KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA` default, and adds a data-quality gate
  for effective sensitive capture.
- Verification: Dashboard regression coverage asserts effective sensitive
  capture is disabled by default and the data-quality check passes on that
  effective state.

### P1 - EVAL-UX-022: Slack benchmark bridge can mark rows passing from run status alone

- Found: 2026-06-14 12:16 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/eval_database.py`, `scripts/promptfoo_eval_db.py`,
  `src/keystone_agents/benchmark_tracking.py`, Promptfoo/Slack benchmark
  scoring.
- Issue: The Promptfoo-to-benchmark bridge can record linked Slack eval rows as
  passing based only on a saved run status, and an empty status currently counts
  as pass. Future API eval trend data should not treat a Slack run as a passed
  benchmark case unless the required safety invariants, source/thread evidence,
  and quality or human-review thresholds are present and passing.
- Evidence: `_benchmark_slack_result()` sets `passed = status in {"done",
  "complete", "completed"} or not status` and writes a single
  `slack_run_status` check. It does not join the latest human review, safety
  pass/fail, source visibility thresholds, side-effect evidence, or Promptfoo
  assertion outcome before assigning `score=1.0`. Existing benchmark bridge
  coverage verifies that Promptfoo and Slack rows are written with provenance,
  but it does not assert that Slack benchmark rows require explicit safety and
  quality gates. `docs/EVALS.md` says a benchmark case passes only when all
  required safety invariants pass and the quality score meets the threshold.
- Expected fix: Make Slack benchmark rows non-passing until they have explicit
  evidence for the selected scoring contract: linked run id, thread/source
  evidence, side-effect/no-write proof, human safety, required quality scores
  or machine assertions, and any case-specific thresholds. Empty status should
  not pass. Add tests for a done Slack run without human safety/evidence
  remaining non-passing, and for a fully reviewed Slack/API eval row mapping
  safety, rubric subscores, and observed-key-only provenance into the benchmark
  store.
- Fix: Slack benchmark rows now require explicit completed status, thread
  evidence, source/response evidence, side-effect/no-write evidence, passing
  human safety, and a human quality threshold before passing.
- Verification: Benchmark bridge regression coverage records one incomplete
  Slack row as failed and one fully reviewed Slack row as passed.

### P1 - EVAL-UX-023: Slack and human-review eval rows keep raw text in the review database and exports

- Found: 2026-06-14 12:28 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/eval_database.py`, `promptfoo/human_review.py`,
  `promptfoo/eval_dashboard.py`, Slack eval persistence and dashboard exports.
- Issue: Slack eval runs and human scorecards persist raw operator-facing text
  separately from the Promptfoo result payload path. Before future API evals,
  the local review database and CSV/dashboard exports need the same
  storage-mode distinction as Promptfoo results so raw Slack requests, raw agent
  summaries, full score replies, or free-form review notes do not become durable
  or exportable by default.
- Evidence: `record_slack_eval_run()` writes `request_text` to both
  `eval_cases.user_input` and `slack_eval_runs.request_text`, and writes
  `result_summary` to `slack_eval_runs.result_summary`. `parse_human_review()`
  stores the complete score reply as `HumanEvalReview.raw_text`, and
  `save_human_review()` persists it in `human_eval_reviews.raw_text` while also
  saving free-form `notes`. The dashboard uses `latest_slack_summary` and
  `human_notes` in case views, and `dashboard_case_export_csv()` exports
  `prompt`, `latest_response`, and `human_notes`.
- Expected fix: Add an eval DB storage policy for Slack run and human-review
  text before paid/API evals. Store hashes, compact redacted summaries, schema
  keys, review scores, safety, and join IDs by default; keep raw request,
  response, raw score reply, and notes only in an explicit local-review mode
  with retention/redaction controls. Dashboard tables and CSV exports should
  label redacted rows, avoid exporting raw prompt/response/notes in API-eval
  safe mode, and add readiness checks that fail paid runs when raw Slack or
  review text would be persisted/exported by default.
- Fix: Slack live eval rows and opt-in human reviews now support
  `api_redacted` storage with redacted text plus request/result/raw-review/note
  hashes; live SDK Slack rows choose API-safe storage automatically.
- Verification: Regression coverage proves Slack run text and human-review
  notes/raw replies do not retain raw email or secret-shaped values in
  API-redacted mode.

### P1 - EVAL-UX-024: Promptfoo live max-case guard is declared but not enforced against selected cases

- Found: 2026-06-14 12:33 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/providers/keystone_agent_provider.py`,
  `scripts/check_eval_slack_readiness.py`, `promptfoo/tests/*.yaml`,
  Promptfoo live/API eval runner setup.
- Issue: The current live guard requires a positive `max_cases`, but it does
  not enforce that cap against the approved case allowlist, the selected
  Promptfoo test set, or any Promptfoo matrix expansion. A paid eval can
  therefore look budget-controlled because `max_cases` is present while still
  running more live cases than the operator intended.
- Evidence: `_live_eval_guard()` parses `max_cases` and only checks that it is
  present and greater than zero. It verifies that the current `case_id` is in
  `case_allowlist`, but it does not compare `len(case_allowlist)` to
  `max_cases`, does not track how many cases have already run in the current
  Promptfoo invocation, and does not validate the actual expanded Promptfoo case
  count before model calls. The docs warn that list-valued `vars` can trigger
  Promptfoo matrix expansion, while current readiness coverage counts committed
  prompts by scanning `agent_under_test:` lines rather than validating the
  selected live run cardinality.
- Expected fix: Move max-case enforcement to an eval-run preflight or shared
  live-run state, not only a per-case provider check. The live runner should
  resolve the exact selected/expanded case IDs before calling the model, fail if
  that count exceeds `max_cases` or the approved allowlist, reject unintended
  matrix expansion unless explicitly requested, and persist selected-case count
  plus estimated spend in run provenance/readiness output.
- Fix: Promptfoo live guard now rejects allowlists, selected case lists, and
  selected-case counts that exceed `max_cases`, and rejects selected cases not
  present in the approved allowlist.
- Verification: Provider regression coverage blocks allowlists and
  selected-case counts larger than the approved `max_cases` cap.

### P1 - EVAL-UX-025: Benchmark failure and check messages can persist raw observed values

- Found: 2026-06-14 12:40 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `src/keystone_agents/benchmark_tracking.py`,
  `scripts/run_local_evals.py`, local/static benchmark storage.
- Issue: The benchmark tracker intentionally stores only observed output keys,
  but failure strings and check messages are stored verbatim. That leaves a
  second path for raw observed values, draft text, email snippets, Slack text,
  or other private fixture content to enter `.keystone/state/benchmark_evals.sqlite`
  when a case fails or a grader includes observed values in its message.
- Evidence: `record_eval_summary()` writes `failures_json` directly from
  `item.get("failures")` and `_compact_checks()` stores
  `str(item.get("message") or "")[:240]`. Local eval helpers such as `_fail()`
  append messages shaped like `field: observed {observed!r}, expected
  {expected!r}`, and other local graders append missing/forbidden text details.
  `docs/EVALS.md` says benchmark storage should keep observed output keys only,
  not raw observed payloads, drafts, email bodies, or private request content.
- Expected fix: Add a benchmark-safe failure-message contract. Store structured
  failure codes, field names, expectation labels, and short redacted summaries
  instead of raw observed values; cap and redact all check messages before
  writing `checks_json` or `failures_json`; and add regression coverage with a
  failing case containing secret-shaped, email-body, and draft-like observed
  text to prove benchmark rows do not retain the raw payload.
- Fix: Benchmark storage now redacts and caps check messages and failure
  strings, including secret-shaped values, emails, bearer tokens, and long
  observed/actual/got payloads.
- Verification: Benchmark regression coverage stores a failing row containing
  raw email and secret-shaped observed values and confirms only redacted
  summaries persist.

### P1 - EVAL-UX-026: Promptfoo sanitizer ignores the current top-level-list case files

- Found: 2026-06-14 12:51 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `promptfoo/eval_sanitizer.py`,
  `scripts/check_eval_slack_readiness.py`, `promptfoo/eval_dashboard.py`,
  `promptfoo/tests/*.yaml`, API eval sanitation readiness.
- Issue: The eval sanitizer exists, but it only scans Promptfoo files shaped as
  a mapping with a `tests:` key. The committed Promptfoo case files are
  top-level YAML lists, so the sanitizer can report `status=pass` after
  scanning zero cases. That leaves the Slack-to-evals case-intake path without
  an effective automated sanitation gate before future API runs.
- Evidence: `scan_promptfoo_case_files()` sets `cases = data.get("tests") if
  isinstance(data, dict) else []`. The committed Promptfoo files such as
  `promptfoo/tests/slack_research.yaml`,
  `promptfoo/tests/slack_retrieval_synthesis.yaml`,
  `promptfoo/tests/slack_tool_safety.yaml`,
  `promptfoo/tests/slack_agent_coverage.yaml`, and
  `promptfoo/tests/slack_agent_expansion_15.yaml` all begin with top-level
  `- description:` entries. A local scanner check returned
  `{"case_count": 0, "issue_count": 0, "status": "pass"}`, and the strict Slack
  eval readiness script/dashboard data-quality checks do not currently call the
  sanitizer.
- Expected fix: Support both Promptfoo root shapes (`tests:` mappings and
  top-level lists), fail closed when expected case files produce zero scanned
  cases, wire the sanitizer into strict Slack/API eval readiness, and add
  regression coverage proving the current committed cases are scanned and an
  unsafe top-level-list fixture fails without an explicit reviewed exemption.
- Fix: Promptfoo sanitation scanning now supports top-level-list case files,
  fails closed on empty expected files, and is part of strict readiness.
- Verification: Sanitizer regression coverage proves unsafe top-level-list
  fixtures fail and the committed case scan covers the current Promptfoo files.

### P2 - EVAL-UX-027: Trace summary scalar fields are not sanitized like trace metadata

- Found: 2026-06-14 12:58 EDT
- Fixed: 2026-06-14 15:08 EDT
- Status: fixed
- Area: `src/keystone_agents/trace_processor.py`,
  `src/keystone_agents/model_provider.py`, `promptfoo/eval_database.py`,
  trace summary storage and dashboard rendering.
- Issue: The eval trace processor rejects unsafe `metadata`, but it persists
  trace scalar fields such as workflow/span name, group id, parent id, trace id,
  and span id after only newline removal and truncation. Future API eval traces
  should treat those fields as join keys and labels, not as a place where raw
  Slack text, prompts, customer names, or secret-shaped values can survive.
- Evidence: `KeystoneEvalTraceProcessor._metadata()` calls
  `sanitize_trace_metadata()` and replaces unsafe metadata with
  `metadata_status=rejected_unsafe`. In contrast, `_safe_trace_payload()` writes
  `trace_id`, `span_id`, `parent_id`, `name`, and `group_id` from SDK objects
  through `_clean_scalar()`, which only strips whitespace, removes newlines, and
  truncates to 160 characters. `TraceConfig.__post_init__()` sanitizes
  `trace_metadata`, but `workflow_name` and `group_id` are only stripped and can
  also come from `KEYSTONE_TRACE_WORKFLOW_NAME` and `KEYSTONE_TRACE_GROUP_ID`.
  `record_eval_trace_event()` then persists those scalar fields directly into
  `eval_trace_events`.
- Expected fix: Add a trace-summary scalar-field safety contract before enabling
  API eval trace capture. Restrict names and group IDs to approved labels or
  join-key patterns, hash or reject unsafe/free-form values, and add tests where
  workflow name, group id, span name, or trace ids contain email, secret-like,
  PHI-like, or Slack-message text to prove the local trace DB and dashboard do
  not retain the raw value.
- Fix: Trace summary scalar fields now use identifier-safe join keys or
  redacted hash labels, and workflow/span names redact or hash unsafe text
  before persistence.
- Verification: Trace processor regression coverage proves raw email and
  secret-shaped scalar values are absent from persisted trace rows.

### P1 - SDK-SESSION-001: SDK session history is reused without a retrieval cap or compaction policy

- Found: 2026-06-14 15:31 EDT
- Fixed: 2026-06-14 15:59 EDT
- Status: fixed
- Area: `src/keystone_agents/sdk_sessions.py`, `src/keystone_agents/sdk.py`,
  `src/keystone_agents/workflow_runner.py`, Slack/WorkItem live SDK continuity
  and cost controls.
- Issue: Local SDK sessions improve follow-up context by reusing a stable
  SQLite-backed conversation for Slack threads and WorkItems, but the current
  session builder retrieves the full stored history by default. A long Slack
  thread or repeated WorkItem follow-up can therefore keep appending old user
  inputs, assistant outputs, tool calls, and final synthesis turns into every
  future model call. That improves continuity, but it can also drive up input
  tokens, increase stale-context risk, and dilute the carefully bounded
  WorkItem/context-pack state that should remain canonical.
- Evidence: `build_sdk_session()` delegates to `build_sqlite_session()` with
  only a derived session id and database path. `build_sqlite_session()` creates
  the Agents SDK `SQLiteSession(session_id, db_path)` without
  `SessionSettings(limit=...)`, a `RunConfig.session_settings` override, a
  `session_input_callback`, or a compaction wrapper. The installed Agents SDK
  default is `SessionSettings(limit=None)`, which retrieves all available
  session items. The repo already documents that WorkItems remain canonical
  state and that thread-specific sessions are intended for follow-up wording and
  cache grouping, not unlimited business-state replay.
- Expected fix: Add a Keystone SDK session retention policy before broad live
  Slack/API use. Keep WorkItems, context packs, approvals, audit rows, and
  artifacts canonical, while using SDK sessions as bounded conversation
  continuity. Configure per-scope defaults such as recent-item limits,
  session-input filtering, or Responses compaction for long sessions; exclude
  final-response-only or intermediate repair turns when they would pollute
  specialist context; expose env/CLI overrides; and record audit-safe
  `session_item_count`, `session_items_sent`, limit/compaction mode, and
  truncation status in request-cache metadata.
- Fix: Added the centralized `KEYSTONE_SDK_SESSION_HISTORY_LIMIT` policy with a
  default recent-item cap, passed it into Agents SDK `SQLiteSession` through
  `SessionSettings(limit=...)`, exposed `--sdk-session-history-limit`, carried
  the value through `WorkflowRunRequest`, and recorded audit-safe session
  history mode/limit/truncation metadata on SDK request-cache and WorkItem
  events.
- Verification: Ran `.venv/bin/python -m py_compile ...` across the changed SDK
  session, CLI, run, workflow, schema, and test files; ran `.venv/bin/python -m
  pytest tests/test_sdk_sessions.py tests/test_sdk_execution.py
  tests/test_model_provider.py
  tests/test_workflow_runner.py::test_manager_loop_final_synthesis_uses_configured_sdk_session`
  with 108 passing tests.

### P1 - SDK-TURNS-001: Specialist SDK turn limits bypass the repo's quality budgets

- Found: 2026-06-14 15:31 EDT
- Fixed: 2026-06-14 15:51 EDT
- Status: fixed
- Area: `src/keystone_agents/agents/business_research_analyst.py`,
  `src/keystone_agents/agents/opportunity_scout.py`,
  `src/keystone_agents/agents/gmail_triage.py`,
  `src/keystone_agents/agents/outreach_composer.py`,
  `src/keystone_agents/quality_budget.py`, centralized SDK run loop policy.
- Issue: Keystone has explicit quality budgets for cost/performance tradeoffs,
  but most direct specialist SDK wrappers do not pass a `max_turns` value into
  the centralized runner. They silently inherit the OpenAI Agents SDK default of
  `max_turns=10`, regardless of whether the task is a fast Slack follow-up,
  balanced research, deep research, simple Gmail triage, or draft-only outreach.
  That makes cost and tool-loop behavior less predictable than the repo's
  quality-mode policy suggests.
- Evidence: `run_typed_sdk_agent()` and `run_typed_sdk_sync()` accept
  `max_turns`, and the installed Agents SDK `Runner.run`/`run_sync` default is
  `max_turns=10`. `run_chief_of_staff_sdk()` already passes
  `budget.max_turns` from `chief_of_staff_quality_budget()` with fast,
  balanced, and deep values of 4/8/14. Orchestrator specialist-as-tool calls use
  `max_turns=6`, and final response synthesis uses `max_turns=2`. In contrast,
  Business Research Analyst, Opportunity Scout, Gmail Triage, and Outreach
  Composer direct SDK wrappers pass no `max_turns`, even though
  `quality_budget.py` defines search-heavy budgets of 4/8/12 turns and the docs
  say quality budgets map fast/balanced/deep modes to model settings, max
  turns, retrieval caps, and hosted-search caps.
- Expected fix: Centralize per-agent SDK turn policy and apply it consistently
  across direct wrappers, WorkItem manager-loop calls, CLI child paths,
  Promptfoo/API eval paths, and Orchestrator-as-tool execution. Suggested
  defaults: small caps for Gmail triage and final synthesis, balanced caps for
  ordinary Business Research/Opportunity Scout, deep caps only when the request
  explicitly asks for deep/more research or formal evidence, and conservative
  caps for Outreach Composer because it is draft-only and approval-gated. Add
  tests that each live/local specialist wrapper passes the resolved
  `max_turns`, records it in trace/request-cache metadata, and preserves an
  explicit override for experiments.
- Fix: Added a centralized direct-wrapper SDK turn policy and wired Business
  Research Analyst, Opportunity Scout, Gmail Triage, Gmail priority grouping,
  and Outreach Composer SDK wrappers to pass explicit `max_turns` instead of
  inheriting the SDK default of 10. Search-heavy agents now use the existing
  fast/balanced/deep quality budgets; Gmail and Outreach use conservative
  fixed defaults; every wrapper preserves an explicit `max_turns` override.
  Request-cache metadata now records the applied cap.
- Verification: Focused wrapper tests prove resolved defaults, live balanced
  quality-budget behavior, explicit overrides, and request-cache max-turn
  metadata. SDK/model regression suite passed.

### P1 - TRACE-OBS-001: Local trace summaries lack a run-level SDK execution record

- Found: 2026-06-14 16:14 EDT
- Fixed: 2026-06-14 16:59 EDT
- Status: fixed
- Area: `src/keystone_agents/trace_processor.py`, `src/keystone_agents/run.py`,
  `src/keystone_agents/workflow_runner.py`, `promptfoo/eval_database.py`,
  `promptfoo/eval_dashboard.py`, local eval trace processor and dashboard.
- Issue: The local eval trace processor stores sanitized trace/span lifecycle
  events, and WorkItem events can store SDK usage/request-cache metadata, but
  there is not one durable, sanitized run-summary trace row that explains what
  happened in an agent run. That makes it hard to inspect a future API eval run
  and answer basic questions such as which agent/route ran, which WorkItem or
  eval case it belonged to, which model/provider and session scope were used,
  how many turns actually ran, how many tool calls happened, whether retries or
  repair loops occurred, and whether the run ended cleanly.
- Evidence: `_trace_dashboard_summary()` lists stored trace fields as
  `event_type`, trace/span IDs, parent ID, name, group ID, sanitized metadata,
  duration, and created_at. `_safe_trace_payload()` records SDK trace/span
  identifiers and sanitized SDK metadata only. `_record_workflow_sdk_cost_event()`
  separately records token usage, cost, and request-cache hashes in WorkItem
  events, including configured request-cache metadata, but those fields are not
  joined into `eval_trace_events`. Direct SDK request-cache metadata now records
  the configured `max_turns`, but the local trace store does not record
  observed `turns_used`, tool-call counts, handoff counts, retry counts,
  output-validation or repair status, or normalized failure kind for each run.
- Expected fix: Add a sanitized `sdk_run_summary` trace event or equivalent
  run-level trace contract emitted by the centralized SDK runner and WorkItem
  manager loop. It should include safe join keys (`case_id`, `eval_id`,
  `work_item_id`, `run_id`, Slack channel/thread IDs where available), agent,
  route, run stage, live/dry mode, model provider/model, session scope/source
  and session hash, configured `max_turns`, observed `turns_used` with source,
  SDK request count, tool-call count by tool name, handoff count, retry count,
  repair-loop count, retrieval provider summary, prompt/cache hashes, token/cost
  summary, status, failure kind, and duration. Keep raw prompts, responses, tool
  inputs/outputs, Slack text, secrets, and PHI out of trace metadata, and add
  dashboard/readiness coverage proving the run summary joins to eval cases and
  WorkItems.
- Fix: Added `keystone.sdk_run_summary.v1` run-level trace events emitted by the
  centralized SDK runner when eval trace summaries are enabled. The summary
  persists safe join keys, agent/route/stage, live/run mode, provider/model,
  session scope/source/hash, max-turn metadata, observed turns source, SDK
  request count, observed tool-call counts by tool name, handoff count, retry
  count, retrieval-provider summary, prompt/cache hashes, token/cost summary,
  status/failure kind, duration, and explicit redaction provenance. WorkItem
  live Outreach SDK synthesis now passes WorkItem/Slack join metadata through
  the existing safe trace metadata path.
- Verification: `.venv/bin/python -m pytest tests/test_trace_processor.py
  tests/test_sdk_execution.py::test_run_typed_sdk_agent_records_sdk_run_summary_trace
  tests/test_promptfoo_framework.py::test_eval_dashboard_renders_promptfoo_slack_and_human_state`
  passed.

### P1 - LOG-OBS-001: Runtime logs are not consistently structured or correlated with trace/run IDs

- Found: 2026-06-14 16:14 EDT
- Fixed: 2026-06-14 16:26 EDT
- Status: fixed
- Area: `scripts/manage_eval_dashboard.sh`,
  `scripts/run_eval_slack_test_server.py`,
  `promptfoo/providers/keystone_agent_provider.py`,
  `scripts/handle_slack_agent_action.py`, child-process logging, dashboard
  logs, Slack feedback JSONL, and WorkItem event logs.
- Issue: Keystone has multiple local log surfaces: dashboard stdout/stderr log
  files, Promptfoo child stdout/stderr capture, Slack feedback JSONL on stderr,
  WorkItem event rows, and eval database trace rows. Several raw-output leakage
  issues have been fixed, but these surfaces still do not share a consistent
  structured log envelope or correlation contract. When an agent run behaves
  badly, the operator has to stitch together process logs, WorkItem events,
  eval rows, Slack run IDs, and trace rows manually, and some logs remain plain
  stdout/stderr lines without severity, component, stage, run ID, trace ID, or
  redaction provenance.
- Evidence: `manage_eval_dashboard.sh` writes dashboard stdout/stderr to
  `.keystone/promptfoo/logs/*.log` and its `logs` command tails plain files.
  `run_eval_slack_test_server.py` prints readiness and starter-prompt guidance
  to stdout before serving the dashboard. Promptfoo provider failure handling
  now stores redacted excerpts instead of raw stdout/stderr, and the backlog
  already tracks older stderr/feedback issues, but there is no shared local log
  schema tying log lines to `trace_id`, `run_id`, `case_id`, `work_item_id`,
  child process ID, route, agent, status, or failure kind.
- Expected fix: Define a local structured logging contract for eval and
  business-agent runs. Use JSONL or another machine-readable envelope with
  timestamp, level, component, event name, agent, route, run stage, process ID,
  child command kind, case/run/work_item/slack/thread/trace correlation IDs,
  duration, status, failure kind, and redaction status. Keep human-readable
  stdout for operator prompts when needed, but mirror diagnostics to structured
  redacted logs with retention rules. Add tests that failed child runs,
  dashboard startup failures, Slack feedback events, and SDK trace summaries can
  be joined by correlation IDs without storing raw prompts, responses, Slack
  message text, secrets, PHI, or tool I/O.
- Fix: Added the shared `keystone.structured_log.v1` local log envelope and
  documentation, with redaction of raw body-like fields and obvious secrets.
  Slack action feedback JSONL now carries a structured log envelope, Promptfoo
  provider failures attach redacted structured diagnostics with case/run/Slack
  correlation where available, the strict Slack eval test server emits
  structured readiness/starter-prompt failures to stderr, and
  `manage_eval_dashboard.sh` writes a dashboard manager JSONL sidecar at
  `.keystone/promptfoo/logs/eval-dashboard.structured.jsonl` while keeping
  human-readable stdout/stderr.
- Verification: `zsh -n scripts/manage_eval_dashboard.sh`;
  `.venv/bin/python -m py_compile src/keystone_agents/structured_logging.py
  scripts/handle_slack_agent_action.py scripts/run_eval_slack_test_server.py
  promptfoo/providers/keystone_agent_provider.py
  tests/test_slack_agent_actions.py tests/test_promptfoo_framework.py`;
  `.venv/bin/python -m pytest
  tests/test_slack_agent_actions.py::test_structured_log_event_redacts_secrets_and_body_text
  tests/test_slack_agent_actions.py::test_slack_agent_action_cli_streams_feedback_jsonl
  tests/test_slack_agent_actions.py::test_slack_agent_action_cli_emits_structured_error_feedback
  tests/test_promptfoo_framework.py::test_promptfoo_provider_cleans_temp_context_and_redacts_failure_payload
  tests/test_promptfoo_framework.py::test_eval_slack_strict_test_server_stops_before_dashboard
  tests/test_promptfoo_framework.py::test_eval_dashboard_manager_declares_structured_log_contract`
  passed.

### P1 - APP-DATA-001: RSS and preprint announcement items are not persisted as queryable application data

- Found: 2026-06-14 15:57 EDT
- Fixed: 2026-06-14 16:15 EDT
- Status: fixed
- Area: `src/keystone_agents/multi_agent_automations.py`,
  `scripts/run_multi_agent_automation.py`,
  `src/keystone_agents/storage/sqlite_store.py`,
  `src/keystone_agents/automation_inventory.py`, weekly announcements and
  preprint/article review.
- Issue: The announcements research workflow can select RSS/channel links,
  attach search or article evidence, and synthesize summaries, but it returns
  Slack text or JSON for the current run instead of saving selected feed items,
  preprints, article reads, relevance decisions, and source evidence as durable
  queryable application records. Future agents therefore cannot reliably answer
  what was already seen, dedupe repeated RSS/preprint links, compare relevance
  decisions over time, or reuse prior article evidence without re-ingesting the
  same material.
- Evidence: `run_announcements_research_synthesis()` builds an
  `AnnouncementResearchAutomationResult` from supplied links and optional live
  search/article evidence, then returns the result. `run_multi_agent_automation.py`
  prints that result and has no database/store argument or `SQLiteStore` save
  path. The generic automation controller persists coarse `AutomationRun`
  summaries for supported controller commands, but the multi-agent
  announcements helper does not write normalized feed, preprint, article, or
  evidence rows. The current SQLite store has generic automation and source
  tables, but no dedicated RSS/preprint/article-review model with canonical
  identifiers and dedupe keys.
- Expected fix: Add a durable research-feed application data model before
  making announcements/preprint review a long-running agent memory source. Use
  structured SQLite tables or equivalent local application state as the
  canonical store for feed item URL, canonical URL, DOI/arXiv/bioRxiv/medRxiv
  identifier when available, title, source/feed/channel, authors, published date,
  tags, relevance status, selected/not-selected status, summary, evidence
  snippets, extraction status, content hash, automation run id, Slack link, and
  review metadata. Add dry-run-safe save/list/query helpers and tests proving
  repeated RSS/preprint items dedupe by canonical URL or stable publication id.
- Fix: Added `AnnouncementFeedItem`/`AnnouncementFeedEvidence`, SQLite schema
  version 13 tables for canonical announcement feed items and evidence rows,
  save/list/get helpers with dedupe by DOI/arXiv/bioRxiv/medRxiv/canonical URL,
  and opt-in announcements persistence through `run_announcements_research_synthesis()`
  and `scripts/run_multi_agent_automation.py --database-url`.
- Verification: `tests/test_storage.py` covers migration/table creation,
  canonical DOI dedupe, evidence reload, and query; `tests/test_multi_agent_automations.py`
  covers persisted announcement runs and duplicate feed rows.

### P1 - APP-DATA-002: RSS and preprint semantic retrieval lacks a canonical index policy

- Found: 2026-06-14 15:57 EDT
- Fixed: 2026-06-14 16:15 EDT
- Status: fixed
- Area: `src/keystone_agents/file_search.py`,
  `src/keystone_agents/file_search_corpus.py`,
  `docs/corpus/resources/file_search_corpus_policy.md`,
  `src/keystone_agents/context_sources.py`, announcement/preprint retrieval.
- Issue: Keystone has hosted FileSearch/vector-store support, but its current
  policy is for approved durable reference material, not raw runtime RSS,
  Slack, Gmail, or unreviewed article data. That is the right safety boundary,
  but it leaves no explicit architecture for making public RSS/preprint/article
  history semantically searchable by future agents. Without a policy, the team
  risks either losing useful prior-review context or uploading raw runtime feed
  payloads into a hosted vector store without canonical provenance, sensitivity,
  retention, and approval controls.
- Evidence: The FileSearch corpus policy says hosted FileSearch is for durable,
  approved reference material and explicitly warns not to use it as a dumping
  ground for raw runtime data. `file_search_corpus.py` ingests reviewed manifest
  files into vector stores, and `file_search.py` attaches configured vector
  stores to selected agents. The announcements synthesis path instead builds
  transient `source_context` for a single run and does not emit approved
  feed/preprint chunks into a local or hosted semantic index.
- Expected fix: Define a two-layer retrieval policy for RSS/preprint review.
  Keep the structured application database from `APP-DATA-001` as the source of
  truth, then build a derived semantic index only from public/sanitized and
  approved feed-item chunks. Start with local FTS or embeddings where feasible;
  allow hosted FileSearch/vector-store upload only behind an explicit live flag,
  approval metadata, sensitivity checks, source provenance, and retention rules.
  Retrieval tools should return feed item ids, stable publication ids, source
  URLs, dates, extraction status, and snippets so Business Research Analyst,
  Chief of Staff, and Orchestrator can reuse prior reviews without treating the
  vector store as canonical state.
- Fix: Added a derived local `announcement_feed_fts` index maintained from the
  canonical SQLite feed records, plus `retrieve_announcement_feed_items()` for
  prior-review retrieval. Updated the FileSearch corpus policy to make
  structured DB state canonical, local SQLite retrieval derived, and hosted
  vector upload approval-gated for public/sanitized chunks only. Registered
  `announcement_feed_history` as a local context source.
- Verification: Storage tests assert local retrieval returns canonical feed item
  ids from the derived index; context-source architecture tests and
  `tests/test_local_context_tool.py` pass with the new source registered.

### P2 - APP-DATA-003: Announcements research is inventoried but not controller-persisted

- Found: 2026-06-14 15:57 EDT
- Fixed: 2026-06-14 16:15 EDT
- Status: fixed
- Area: `src/keystone_agents/automation_inventory.py`,
  `scripts/run_keystone_automation.py`, `scripts/run_multi_agent_automation.py`,
  `src/keystone_agents/tools/automation_inventory_tool.py`, scheduled
  announcements operations.
- Issue: The automation inventory defines a weekly announcements research
  synthesis, and the standalone multi-agent helper can run
  `announcements-research`, but the controller path that records
  `AutomationRun` state for Chief of Staff review does not expose that workflow.
  That means scheduled RSS/preprint review can be run as a helper, but it is not
  consistently visible in the same persisted automation ledger, health surface,
  and failure-review tools as the other managed automations.
- Evidence: `automation_inventory.py` includes
  `auto_announcements_weekly_research_synthesis` with workflow
  `announcements-weekly-research-synthesis`. `run_multi_agent_automation.py`
  supports `--kind announcements-research` and prints the result. The managed
  controller in `run_keystone_automation.py` records `AutomationRun` summaries
  through `save_automation_run()`, but its supported subcommands are the
  controller workflows, not the announcements multi-agent helper.
- Expected fix: Add a managed controller path for announcements research that
  preserves dry-run defaults, live SDK/search flags, locks, preflight checks,
  failure recording, and `AutomationRun` persistence. It should call the
  application-data persistence from `APP-DATA-001` when enabled, record compact
  run provenance for Chief of Staff tools, and avoid storing raw feed or article
  text outside the approved storage policy.
- Fix: Added `scripts/run_keystone_automation.py announcements-research`, which
  delegates to the multi-agent announcements helper, preserves dry-run/live
  research gates, lock/preflight behavior, failure handling, database handoff,
  and records `AutomationRun` rows against the existing
  `auto_announcements_weekly_research_synthesis` inventory spec.
- Verification: `tests/test_automation_control.py` covers the managed
  announcements command, child command construction, dry-run env flags, and
  persisted `AutomationRun` ledger entry.

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
- Fixed: 2026-06-14 16:34 EDT
- Status: fixed
- Area: retrieval policy, Orchestrator memo, specialist runtime budget.
- Issue: Requests with `current`, `latest`, `2026`, `recent`, or similar wording
  imply deeper reasoning and fresher source requirements, but the depth policy is
  not yet uniformly represented across all agents.
- Expected fix: Make temporal intent a shared transient policy: request recent
  sources, independent validation when available, source extraction/read-through
  before synthesis, and explicit “not enough evidence yet” output when the tool
  budget is exhausted.
- Fix: Added a shared `keystone.temporal_depth_policy.v1` transient policy helper
  and attached it to both WorkItem specialist Orchestrator memos and compact
  Orchestrator preflight memos. Current/recent/latest/year-sensitive requests now
  carry an explicit recency requirement, independent-validation preference,
  source read-through instruction, and the required “not enough evidence yet”
  behavior when fresh evidence cannot be obtained within the available tool
  budget.
- Verification: `.venv/bin/python -m pytest tests/test_temporal_policy.py
  tests/test_workflow_runner.py::test_orchestrator_memo_asks_specialist_to_reason_about_success_criteria
  tests/test_orchestrator_preflight_context.py::test_preflight_memo_includes_temporal_depth_policy`
  passed.

### P2 - Unknown temporary model aliases should fail early or have pricing metadata

- Found: 2026-05-25 19:27 EDT
- Fixed: 2026-06-14 16:31 EDT
- Status: fixed
- Area: runtime model policy, manual planner cost guardrails.
- Issue: Temporarily setting agents to `gpt-5.5` caused the live manual planner
  to fall back because local pricing metadata had no matching model entry. That
  did not appear to be the direct timeout crash, but it weakened early planning
  and target extraction for the Slack test.
- Expected fix: Either add reviewed pricing metadata before temporary model
  tests, or make unknown live model aliases fail before planner execution with a
  precise configuration error and rollback instruction.
- Fix: Live `ModelConfig.require_live_execution_ready()` now checks the
  checked-in pricing table before credentialed execution for supported
  providers. Priced base models and dated prefix variants remain allowed, but an
  unpriced alias such as `openai/gpt-5.5` raises
  `ModelProviderConfigurationError` with instructions to add a reviewed pricing
  row or roll back the `KEYSTONE_*_MODEL`/`OPENAI_MODEL` override to a priced
  model such as `gpt-5.4-mini`.
- Verification: `.venv/bin/python -m pytest tests/test_costing.py
  tests/test_model_provider.py` passed.

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

### P1 - COS-TOOLS-001: Chief specialist tools lack typed context-pack input

- Found: 2026-06-15 17:03 EDT
- Fixed: 2026-06-15 17:45 EDT
- Status: fixed
- Area: `src/keystone_agents/specialist_agent_tools.py`,
  `src/keystone_agents/agents/chief_of_staff.py`, Chief of Staff
  specialists-as-tools workflow.
- Issue: `build_specialist_agent_tools()` exposes registered specialists with
  SDK `Agent.as_tool()` using the default single-string input contract. The
  nested specialist call does not receive a typed Chief-to-specialist payload
  that preserves the raw operator request, selected Slack/Gmail/WorkItem
  context, source refs, approval context, requested source-layer manifest, and
  side-effect boundaries as structured fields.
- Evidence: The only `as_tool()` construction passes `tool_name`,
  `tool_description`, and `max_turns`; there is no `parameters`,
  `include_input_schema`, or `input_builder`. Existing focused tests verify
  tool inclusion and nested tool filtering, but do not exercise a nested Chief
  call with structured Slack, WorkItem, source, or approval context.
- Impact: The outer Chief model may summarize context into a free-form tool
  input, but Python cannot guarantee that the nested specialist sees the same
  context-pack contract used by direct WorkItem specialist runs. This can make
  cross-agent Chief requests drop selected thread context, source scope,
  approval blockers, or requested-system manifests before the specialist
  reasons.
- Expected fix: Add a bounded Pydantic input schema for Chief specialist-tool
  calls, with an `input_builder` that carries the raw request plus compact
  context/approval/source manifests into the nested agent. Add regression
  coverage that invokes at least Gmail Triage, Business Research, Airtable
  Context, and Google Workspace Context through Chief specialist tools and
  verifies the nested input contains the expected structured context and
  no-send/no-write policy.
- Fix evidence: `ChiefSpecialistToolInput` now provides a strict Pydantic
  context contract for specialist tools, including raw operator request,
  specialist task, Slack/Gmail/WorkItem context entries, source-layer manifest,
  source refs, approval context, and side-effect boundaries.
  `build_specialist_agent_tools()` passes that schema plus
  `build_chief_specialist_tool_input()` into every generated SDK
  `Agent.as_tool()` call.
- Verification: `.venv/bin/python -m pytest tests/test_chief_of_staff.py
  tests/test_agent_registry.py`.

### P1 - COS-TOOLS-002: Nested specialist outputs are not extracted into a reviewable contract

- Found: 2026-06-15 17:03 EDT
- Fixed: 2026-06-15 17:45 EDT
- Status: fixed
- Area: `src/keystone_agents/specialist_agent_tools.py`,
  `src/keystone_agents/schemas/chief_of_staff.py`, Chief of Staff final
  synthesis and trace review.
- Issue: Chief specialist tools use the SDK default nested output behavior and
  do not provide a `custom_output_extractor` or repo-local adapter that
  normalizes each specialist result into a reviewable Chief-owned contract.
  The final `ChiefOfStaffResult` has no explicit field for nested specialist
  calls, source mappings, blockers, or validation status.
- Evidence: `build_specialist_agent_tools()` attaches metadata such as
  `specialist_route_name` and `nested_tool_names`, but the tool output path is
  otherwise the SDK default. `ChiefOfStaffResult` stores final sources,
  actions, write requests, diagnostics, and audit notes, but not nested
  specialist-result provenance. Focused tests cover tool availability and
  write-tool filtering, not whether nested outputs are parsed, attributed,
  reviewed, or surfaced in traces/artifacts.
- Impact: When the Chief integrates a specialist result, source attribution,
  blocker preservation, human-work context, and advisory-vs-authoritative
  status depend mostly on prompt compliance. Another session reviewing a Slack
  or CLI run may not be able to distinguish which conclusions came from which
  nested specialist, whether nested structured output parsed cleanly, or which
  nested blockers were intentionally carried into the final recommendation.
- Expected fix: Introduce a small nested-specialist result envelope or
  extractor that records route, tool name, parsed output status, source refs,
  blockers, approval gates, human-work context, and summary. Carry that
  envelope into Chief audit/retrieval diagnostics or a dedicated structured
  field, and add tests that malformed or missing nested output is visible as a
  blocker rather than silently folded into the final prose.
- Fix evidence: `ChiefNestedSpecialistResult` and
  `ChiefNestedSpecialistSourceRef` now provide the reviewable envelope, and
  `extract_nested_specialist_result_json()` is registered as the SDK
  `custom_output_extractor` for generated specialist tools. `ChiefOfStaffResult`
  now includes `nested_specialist_results` so CoS can preserve source IDs,
  blockers, approval needs, human-work context, and validation status from
  nested calls.
- Verification: `.venv/bin/python -m pytest tests/test_chief_of_staff.py
  tests/test_agent_registry.py`.
