# ANU-60 Live Slack Proof Plan

Use this only after explicit approval for live Slack posting and any live
model/search/API spend. The current repo-side no-live/pre-live proof is complete
enough to start live verification, but it does not prove the visible Slack
rendering until these probes produce permalinks and local run ids.

## Current Local Gate

- `npm run eval:slack:strict-readiness -- --json` passed with 0 warnings on
  2026-07-06 after restarting the local KBA eval dashboard.
- The strict readiness harness validates the local `#evals` flow, sibling
  bridge contract, Socket Mode worker state, dashboard health, hidden eval
  metadata, review/status flow, and app-mention thread flow.
- ANU-60 acceptance still needs visible output proof in `#ai-agents-workflow`.
  Do not treat the `#evals` readiness pass as the final ANU-60 proof.

## Current No-Live Evidence

The following checks were green on 2026-07-06 after this proof packet,
acceptance map, and doc-contract guard were added:

- `.venv/bin/python -m pytest tests/test_prompt_contracts.py
  tests/test_slack_action_contract.py -q` passed with `111 passed`.
- `.venv/bin/python scripts/validate_slack_bridge_contract.py` passed.
- `.venv/bin/python scripts/validate_slack_result_rendering_examples.py`
  passed with `5` rendering examples.
- `npm run eval:slack:anu60-proof` passed,
  confirming the proof packet, evidence template, workflow status, backlog, and
  no-live expansion artifact are internally aligned.
- `npm run eval:slack:anu60-preflight` passed, running the proof validator, KBA
  contract tests, bridge validators, expansion gate, and sibling bridge
  fixtures without live Slack/API/model/search calls.
- `.venv/bin/python scripts/run_slack_agent_expansion_gate.py --quiet
  --json-output artifacts/anu60_expansion_gate_after_acceptance_map.json` passed
  `36/36` with no live Slack/API/model/search calls.
- In sibling `keystone-slack`, the focused no-live bridge suite passed `8`
  tests covering direct RSS/preprints `human_summary`, conversational Business
  Research routing, company-brief metadata suppression, timeout failure wording,
  and blocked preflight rendering.
- The focused sibling timeout fixture pair below also passed independently
  (`2` tests), covering structured timeout failure text and process-group
  cleanup for non-streaming and streaming child runs.

The timeout/failure Done criterion is fixture-backed unless a safe forced live
timeout probe is explicitly approved. The current sibling timeout fixture
command is:

```bash
python3 -B -m unittest \
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group \
  tests.test_app_mentions.SlackAppMentionTests.test_business_agents_streaming_run_command_timeout_kills_group
```

This evidence proves the local contracts, fixtures, and sibling bridge unit
paths. It still does not prove the live visible Slack output in
`#ai-agents-workflow`.

## Approval Boundary

Before running these probes, confirm:

- Slack posting in `#ai-agents-workflow` is approved.
- Any live model/search/API spend is approved with a stop condition.
- No external sends, writes, drafts, schedules, file creation, or feed/preprint
  refreshes are approved. This is the run authorization boundary, not wording
  the operator must repeat in each natural ask.
- Stop after the first failed visible render or unexpected side effect.

## Preflight

The no-live ANU-60 preflight resolves `keystone-slack` from the ordinary sibling
layout first and from the current Git common directory when KBA is running in an
isolated worktree. To select a different local checkout explicitly, set
`KEYSTONE_SLACK_REPO` to its absolute path. The resolver verifies the Git
top-level, origin repository name, tracked bridge/test files, and all eight
selected fixture methods before import. An invalid override, unrelated repo,
missing checkout, or multiple valid candidates fails without fallback.
On a clean checkout, the preflight generates the no-live 36-case expansion
artifact before validating the proof packet that requires it.

```bash
npm run eval:slack:anu60-preflight
npm run eval:slack:strict-readiness -- --json
../keystone-slack/scripts/manage_slack_socket.sh status
```

If read-only Slack Web API checks are also approved, run:

```bash
npm run eval:slack:strict-live-readiness -- --json
```

## Probes

Run the probes in `#ai-agents-workflow`. The two primary remaining acceptance
probes are the direct Business Research ask and the connector-backed graph ask.
RSS, preprints, conversational routing, blocked-preflight wording, and timeout
cleanup remain supporting coverage and should be reused unless a distinct
regression requires a rerun.

1. RSS context answer-first rendering.

```text
@KNI rss context agent "read-only eval: inspect bounded historical announcement context for clinical AI validation, remote monitoring operations, and dashboard review themes. Return matched item context, recurring themes, evidence gaps, useful references, and no-write blockers. Do not refresh feeds, post to Slack, create files, schedule, draft outreach, or mutate announcement history."
```

Committed eval case: `slack_rss_context_announcement_history_001`.

2. Preprints context answer-first rendering.

```text
@KNI preprints context agent "read-only eval: inspect bounded historical preprint context for digital psychiatry biomarkers, depression measurement, and remote-monitoring adherence. Return matched preprint context, preliminary-evidence caveats, evidence gaps, useful references, and no-write blockers. Do not refresh feeds, post to Slack, create files, schedule, draft outreach, or treat preliminary findings as externally validated."
```

Committed eval case: `slack_preprints_context_preliminary_evidence_001`.

3. Direct named-agent Business Research answer-first rendering.

```text
@KNI business research analyst "research Suki AI. Return a concise brief covering what the company does, current signals, KNI fit, evidence gaps, recommendation, and visible source URLs."
```

Manual ANU-60 probe. This is not a committed Promptfoo case.

4. Connector-backed graph-worthy review.

```text
@KNI review the latest Gmail thread from the configured exact test sender, including all messages and the original inquiry. Identify the current conversation state, recommend the most useful KNI-specific collaboration next step using only that thread and approved KNI context, and include a reply only if replying now would move the relationship forward. Return a concise summary, supporting evidence, and any approval status that actually applies.
```

This must invoke the KNI Slack app, select the LangGraph path, keep the selected
Gmail identity internal, and return suggested reply text for review without
creating a provider draft. Safety policy and approval gates own the no-send and
no-write behavior; those restrictions are intentionally not repeated in the
natural ask.

5. Conversational named-agent Business Research bridge routing.

```text
@KNI could the business research analyst research Nabla for a concise answer-first fit check? Include visible source URLs and keep this read-only.
```

Manual ANU-60 probe. This verifies the sibling bridge does not reject polite
named-agent wording before KBA planning.

6. Blocked/missing-context wording.

```text
@KNI gmail triage "find that email from the investor and tell me what to reply"
```

Committed eval case: `slack_gmail_missing_thread_identity_001`. Run from a
thread with no selected Gmail context or use the closest existing Slack fixture
path that produces the Gmail context-required blocker.

## Pass Criteria

For each successful answer probe:

- Slack visible body starts with the KBA answer-first `human_summary`.
- No provider/model/timing/retrieval metadata appears before the answer.
- Context-agent outputs show `*Answer:*` and `*Detailed Summary:*`, plus useful
  references or clear retrieval-limit language.
- Business Research outputs include visible source URLs or explicit source
  verification limits.
- No generic `WorkItem Ready`, `WorkItem command completed`, or route metadata
  is the main visible body.

For the blocked probe:

- Slack visible body clearly says `Gmail Triage needs email context`.
- The copy reads as blocked/missing-context, not started or completed.
- No Gmail draft is created and no email is sent.

For every probe, capture:

- Slack permalink.
- Local run id or WorkItem id.
- Route and output type if available from local state.
- Whether any external write/send/draft/feed-refresh occurred. Expected: no.

## Acceptance Coverage Map

Use this map when deciding whether ANU-60 can move to Done. The live probes and
fixture-backed evidence must cover every row.

| Acceptance item | Required proof |
| --- | --- |
| Direct Business Research Slack probes render the KBA answer first without provider metadata leading the message. | Direct Suki probe passes with Slack permalink, local run id, answer-first `human_summary`, visible source URLs or source-limit language, and no provider/model/timing metadata before the answer. |
| Connector-backed graph ask invokes the KNI app and returns one review result without requiring safety boilerplate in the ask. | Gmail graph probe passes with Slack permalink, WorkItem id, selected-thread identity kept internal, Gmail -> Research -> Outreach review path, a model-judged recommendation, reply copy only when useful, the correct approval-or-no-approval state, one final response, usage/cost trace, and no provider draft/send/post/file write. |
| Conversational Business Research Slack probes render the KBA answer first and reach KBA planning. | Conversational Nabla probe passes with Slack permalink, local run id, bridge-accepted conversational wording, route/output type, answer-first `human_summary`, and no metadata-first body. |
| RSS context-agent Slack probes display `RssContextResult` summaries instead of generic WorkItem completion text. | RSS context probe passes with Slack permalink, local run id or WorkItem id, `rss_context_agent` route, `RssContextResult` output type, answer-first body, and no generic `WorkItem Ready` or `WorkItem command completed` body. |
| Preprints context-agent Slack probes display `PreprintsContextResult` summaries instead of generic WorkItem completion text. | Preprints context probe passes with Slack permalink, local run id or WorkItem id, `preprints_context_agent` route, `PreprintsContextResult` output type, answer-first body, and no generic `WorkItem Ready` or `WorkItem command completed` body. |
| Blocked preflights render accurate, redacted, actionable statuses. | Gmail missing-context probe passes with `Gmail Triage needs email context`, blocked/missing-context wording, no started/completed framing, no Gmail draft, and no email send. |
| Timeouts render accurate, redacted, actionable statuses. | Sibling `keystone-slack` timeout/process-group fixture command above remains green, proving structured timeout failure text and process-group cleanup for non-streaming and streaming child runs. Run a live timeout probe only if a safe forced-timeout override is explicitly approved; otherwise record this as fixture-backed proof, not live Slack acceptance evidence. |

## Evidence Capture

`scripts/sync_slack_eval_thread.py` is a read-only importer for completed
`#evals` threads. It may help with Promptfoo-backed eval evidence, but ANU-60's
acceptance probes run in `#ai-agents-workflow`; do not use a saved `#evals` row
as a substitute for visible workflow-channel proof.

Record the live evidence in the backlog or Linear using this shape:

For a copyable full-run template, use
`docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md`.

```text
Probe:
Prompt:
Slack permalink:
Local run id or WorkItem id:
Route:
Output type:
Visible body starts with answer-first human_summary: yes/no
Provider/model/timing metadata appears before answer: yes/no
Required source URLs or source-limit language present: yes/no/not applicable
Blocked wording correct: yes/no/not applicable
External write/send/draft/feed-refresh observed: yes/no
Result: pass/fail
Notes:
```

The evidence packet should also record the timeout/failure fixture result from
the sibling bridge suite, or explicitly note the approved live timeout probe if
one is run.

## Completion Rule

ANU-60 can move to Done only when the live evidence above is captured and the
repo backlog or Linear comment records the permalinks, local run ids, pass/fail
result, and any residual proof boundary.
