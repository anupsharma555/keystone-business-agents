# Slack Message Actions

Keystone supports a Slack message shortcut named `Run Keystone Agent`.

The Slack runtime should route `message_action` payloads with callback id
`keystone_run_agent_message` to:

```bash
.venv/bin/python scripts/handle_slack_agent_action.py --payload-file payload.json --json
```

That command writes a bounded selected-message context file under
`artifacts/slack_contexts/` and returns a Slack modal view. On modal
submission, route the `view_submission` payload back to the same script. The
handler starts a local WorkItem with `WorkflowRunRequest.context_file_path` and
runs Orchestrator preflight before specialist execution. Use `--feedback-jsonl`
when the Slack bridge should stream Orchestrator preflight, manager-loop review,
repair/deepening, and completion feedback events while the run is executing.

Manifest fragment:

```yaml
features:
  shortcuts:
    - name: Run Keystone Agent
      type: message
      callback_id: keystone_run_agent_message
      description: Run a Keystone business agent using this message as context.
settings:
  interactivity:
    is_enabled: true
```

Only the selected message and explicitly supplied thread replies are stored.
If thread retrieval fails, the WorkItem still starts with selected-message
metadata and records a warning. Slack actions do not approve, send, post,
schedule, or publish anything.

Direct WorkItem actions launched from Slack, including continue, run again, more
research, find contact, and revise draft, keep their deterministic WorkItem
route but attach compact Orchestrator preflight context before advancing. After
execution they record an `orchestrator_action_review` event and compact review
metadata so later runs can see whether the result answered the request, stayed
in lane, and respected approval boundaries.

## Slack Bridge Readiness Backlog Checks

Use these local backlog checks before treating the live Slack bridge as fully
compatible with the Orchestrator-first architecture. They are implementation
readiness checks, not external test-pack cases.

### Transport + Runtime

| Check | Expected evidence | Status |
| --- | --- | --- |
| Bridge acknowledges Slack interactions quickly. | Sibling `keystone-slack` unit test `test_business_agents_run_agent_submission_acks_and_starts_background_thread` verifies modal submission returns an immediate running view and starts background work. | covered locally |
| Bridge invokes the handler with `--feedback-jsonl` for modal submissions. | Covered by sibling `keystone-slack` unit test `test_business_agents_run_agent_submission_streams_feedback_jsonl`, which asserts the flag, stderr callback, and captured `keystone.slack.agent_feedback_event.v1` event. | covered locally |
| Bridge streams feedback events back to Slack while the run executes. | Sibling `keystone-slack` unit test `test_business_agents_run_agent_background_updates_modal_from_feedback` covers feedback callback propagation into modal updates. Live Slack proof still pending. | covered locally |
| Bridge posts progress updates to the correct Slack surface. | Sibling `keystone-slack` unit test `test_business_agents_run_agent_background_updates_modal_from_feedback` verifies progress is rendered through `views.update`, not lost in stderr/stdout. Live Slack proof still pending. | covered locally |
| Bridge posts the final rendered result after agent completion. | Sibling `keystone-slack` tests `test_business_agents_run_agent_submission_streams_feedback_jsonl`, `test_business_agents_run_agent_submission_blocked_preflight_renders_blocked`, and `test_business_agents_run_agent_background_updates_modal_from_feedback` verify final modal updates and source-thread result metadata after feedback events, with no raw internal JSON exposed. Live Slack proof still pending. | covered locally |

### Context Passing

| Check | Expected evidence | Status |
| --- | --- | --- |
| Bridge passes selected thread replies into Keystone. | `tests/test_slack_agent_actions.py` verifies supplied `thread_messages` reach WorkItem Slack context and Chief of Staff live-SDK input context. | covered locally |
| Bridge preserves context across modal open and submission. | `tests/test_slack_agent_actions.py` verifies modal private metadata can preserve either `context_file_path` or embedded `selected_context`. | covered locally |
| Bridge preserves hidden eval metadata and reply guidance. | `test_slack_eval_message_action_records_run_and_reply_guidance` verifies hidden Slack eval metadata survives modal submission, records a local Slack eval run, and appends case id, run id, case dashboard link, and human review form link to the reply payload. The score-text parser remains a deterministic fallback when the form is unavailable. | covered locally |
| Orchestrator receives selected Slack context before routing. | `tests/test_slack_agent_actions.py` and `tests/test_orchestrator.py` verify selected Slack metadata and a compact thread summary enter `workflow_state_summary` during Orchestrator preflight, even when database workflow state is loaded. | covered locally |
| Direct Slack WorkItem actions attach preflight context. | `tests/test_slack_tool.py` verifies more-research/revision and continue actions pass compact Orchestrator preflight into `WorkflowRunRequest`. | covered locally |

### Reusable Query Prompts + Multi-Target Research

| Check | Expected evidence | Status |
| --- | --- | --- |
| Slack-origin research requests attach a reusable query prompt. | `tests/test_slack_query_prompts.py`, `tests/test_slack_agent_actions.py`, `tests/test_slack_tool.py`, and `tests/test_workflow_runner.py` verify modal submissions, direct Slack WorkItem actions, and workflow auto-attach produce `slack_query_prompt` metadata with bounded task brief, context flags, cost profile, dynamic-content hash, and deterministic-route mismatch diagnostics. | covered locally |
| Prompt selection stays advisory and cannot authorize tools or side effects. | `tests/test_slack_query_prompts.py` verifies unsafe side-effect requests do not silently select a prompt, negated no-send/no-post constraints are preserved, and the rendered task brief states that deterministic gates remain authoritative. | covered locally |
| Common Slack source-read comparisons route to multi-target research instead of single-company retrieval. | `tests/test_workflow_runner.py` verifies a category comparison dispatches the multi-target branch before the single-company `retrieve_company_profile_live` lane. | covered locally |
| Multi-target search separates breadth from depth. | `tests/test_multi_target_research.py` verifies candidate discovery, one breadth repair pass, target selection, per-target depth, substitution, source sufficiency gates, and blocked depth gaps. | covered locally |
| Product comparisons do not promote source organizations into targets. | `tests/test_slack_query_prompts.py` verifies the reusable prompt says source organizations, reports, media, regulators, and listicles are evidence only. `tests/test_multi_target_research.py` verifies AP News, CNN, FTC, Facebook, Brookings, Common Sense Media, Core Ethos-style adjacent safety products, and generic contact pages do not count as product targets or sufficient product evidence. | covered locally |
| Multi-target runs are auditable after execution. | `tests/test_multi_target_research.py` verifies artifacts include `diagnostics.target_selection` with selected targets, top candidates, ranking scores, evidence counts, source sufficiency, and weak-target gaps. | covered locally |
| The Slack source-read path has a repeatable local eval. | `evals/local/slack_research_workflow.jsonl` runs through `scripts/run_local_evals.py --dataset slack_research_workflow --json` and verifies reusable prompt selection, multi-target pass types, selected product targets, visible source URLs, and no source-organization target promotion. | covered locally |
| Live Slack retest shows the same behavior in-channel. | Manual live proof should use a Slack `@KNI business research analyst` source-read comparison and inspect the WorkItem artifact/trace for `slack_query_prompt`, `multi_target_research.pass_types`, visible source URLs, and `diagnostics.target_selection`. This was intentionally paused after the latest repo-side patch. | pending live Slack proof |

### Safety Boundaries

| Check | Expected evidence | Status |
| --- | --- | --- |
| Bridge keeps feedback read-only and separate from approvals/writes. | `test_realtime_feedback_events_do_not_authorize_side_effects` verifies feedback events do not carry approval IDs or enable send/post/publish/schedule/CRM/write flags. | covered locally |

## Slack Memory + Automation Backlog Checks

Use these local backlog checks for bridge behavior that depends on prior Slack
requests, channel automations, WorkItem history, agent-run memory, or operator
corrections. They are separate from external test-pack cases.

### Prior Context + Relevance

| Check | Expected evidence | Status |
| --- | --- | --- |
| Prior `@KNI` request lookup is bounded and relevant. | Sibling `keystone-slack` test `test_business_agents_message_action_includes_same_thread_prior_run_context` verifies only the selected Slack thread's prior run record is attached to the modal context. | covered locally |
| Prior request context is channel/thread scoped. | Sibling `keystone-slack` test `test_business_agents_message_action_includes_same_thread_prior_run_context` verifies a different channel with the same timestamp is excluded. | covered locally |
| Orchestrator receives compact prior-run summaries, not raw unrelated history. | `test_modal_submission_passes_prior_thread_runs_to_orchestrator_preflight` verifies compact prior run summaries enter `prior_agent_runs` in Orchestrator preflight. | covered locally |
| Wrong-lane regression cases use real prior request shapes. | `test_wrong_response_diagnostics_do_not_trigger_tax_payment_shortcut`, `test_orchestrator_review_flags_wrong_response_diagnostic_wrong_lane`, and selected-message prior-run/context tests cover architecture review, Slack-history review, state of KNI, tax/payment, automation audit, and follow-up request shapes. | covered locally |
| Slack history retrieval failure degrades cleanly. | `test_thread_fetch_failure_warns_but_run_proceeds` verifies failed thread retrieval records a bounded warning and still starts the WorkItem with selected-message context. | covered locally |

### Automations + Channel Defaults

| Check | Expected evidence | Status |
| --- | --- | --- |
| Automation runs are distinguishable from human `@KNI` asks. | `test_modal_submission_omits_channel_automation_context_for_unrelated_research` verifies channel automation inventory is not attached to ordinary research asks in an automation-bound channel. | covered locally |
| Channel automation inventory is explicit and relevant. | `test_modal_submission_passes_channel_automation_context_when_relevant` verifies automation/default-channel context is supplied for automation/channel-default asks. | covered locally |
| Channel-specific default behavior is explicit. | `test_modal_submission_passes_channel_automation_context_when_relevant` verifies channel bindings include automation id, workflow, schedule, route, purpose, and default channel in Orchestrator preflight context. | covered locally |

### Continuation + Stale Output Control

| Check | Expected evidence | Status |
| --- | --- | --- |
| Repeated `@KNI` asks are idempotent or clearly rerun. | Sibling `keystone-slack` test `test_repeated_same_slack_request_ts_reuses_existing_run_record` verifies duplicate Slack events reuse the existing run record, and `test_thread_show_continue_artifacts_and_approval_resolve_linked_work_item` verifies `run again` intentionally advances the linked WorkItem. | covered locally |
| Stale final responses are rejected. | `test_modal_submission_rejects_mismatched_final_result_context` verifies final WorkItem Slack context must match the selected Slack request before a result is returned to the bridge. | covered locally |
| WorkItem continuation resolves the intended WorkItem. | Sibling `keystone-slack` tests `test_thread_show_continue_artifacts_and_approval_resolve_linked_work_item`, `test_thread_run_again_without_linked_work_item_returns_missing_record`, and `test_thread_reply_continues_existing_business_agent_thread` verify `continue`, `run again`, and same-thread follow-ups bind to the intended WorkItem or stop with a missing-record guard. | covered locally |
| Direct Slack WorkItem actions record Orchestrator review. | `tests/test_slack_tool.py` verifies direct more-research and continue actions add `orchestrator_action_review` timeline/metadata after the WorkItem advance. | covered locally |

### Provenance + Learning

| Check | Expected evidence | Status |
| --- | --- | --- |
| Bridge records run provenance. | `test_modal_submission_starts_work_item_with_slack_metadata` verifies each Slack result includes bridge-checkable provenance with channel, selected message ts, thread ts, WorkItem id, route/status, request hash, and context fingerprint. | covered locally |
| User corrections become feedback/memory. | Sibling `keystone-slack` test `test_thread_reply_records_correction_feedback_for_prior_agent_run` records same-thread correction feedback, and `test_business_agents_message_action_includes_same_thread_prior_run_context` verifies it is included in future prior-run context for Orchestrator preflight. | covered locally |
| Cross-repo environment contract is verified. | Sibling `keystone-slack` tests `test_business_agents_run_agent_submission_streams_feedback_jsonl`, `test_business_agents_run_agent_submission_preserves_explicit_live_flags`, and `test_business_agents_child_env_lets_kba_repo_own_provider_config` verify KBA database URL, context dir, feedback JSONL, live-search/live-SDK flags, and KBA-owned provider env handling. | covered locally |
