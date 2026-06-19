# KBA Eval Readiness Flow

Load this reference when diagnosing Keystone Promptfoo, Slack eval readiness,
dashboard, or trace issues in `<repo>`.

## Offline Promptfoo Flow

Use this path for most harness regressions:

```bash
npm run eval:promptfoo:validate
npm run eval:promptfoo:json
npm run eval:promptfoo:db
npm run eval:promptfoo:list
npm run eval:promptfoo:dashboard
```

Key files:

- Config: `promptfooconfig.yaml`.
- Manifest: `promptfoo/eval_manifest.yaml`.
- Cases: `promptfoo/tests/*.yaml`.
- Provider: `promptfoo/providers/keystone_agent_provider.py`.
- Assertions: `promptfoo/assertions/kba_slack_invariants.py`.
- Local DB helper: `scripts/promptfoo_eval_db.py`.

Expected high-value output fields from provider compaction include route,
status, human summary, visible source URLs, safety flags, audit notes,
context-pack type, downstream next-action agent, and Slack context attachment.

## Slack Readiness Flow

Use strict readiness as the go/no-go check before live `#evals` testing:

```bash
npm run eval:slack:readiness
npm run eval:slack:strict-readiness
```

Use read-only live probes only when the user explicitly wants live readiness:

```bash
npm run eval:slack:strict-live-readiness
npm run eval:slack:strict-live-test-server
```

The live-readiness commands probe Slack configuration and history access. They
must not post messages.

## Trace Summary Flow

Key contract:

- Durable event: `keystone.sdk_run_summary.v1`.
- Core code: `src/keystone_agents/trace_processor.py`,
  `src/keystone_agents/run.py`.
- Test surface: `tests/test_trace_processor.py`.

Trace summaries should be redaction-first and persistence failure-safe. Store
run-level metadata, usage, route, join keys, and sanitized status. Do not store
raw private messages, secrets, or full prompt bodies.

## Known Failure Signatures

- Mixed planning/research asks blocked as outreach: inspect broad draft/compose
  gates before changing route logic.
- Blocked orchestrator cases show the wrong route: compact from inner
  `route_result` when the outer route is only `orchestrator`.
- Strict readiness fails after dashboard wrapper changes: verify
  `--dashboard-server-will-start` and current wrapper behavior before assuming
  Slack runtime failure.
- Historical backlog text overstates active work: inspect the active backlog
  section and confirm with code/tests before editing.
- Dashboard shows stale or missing labels: inspect import path, eval database,
  URL generation, and server route before changing Promptfoo cases.
- Strict readiness fails even though the repo-local eval path is sound: inspect
  Slack manifest assumptions, declared bot scopes, dashboard startup flags, and
  sibling bridge assumptions before changing agent behavior.
- Trace persistence failures should not crash runs: keep
  `keystone.sdk_run_summary.v1` emitted from the central SDK runner and
  failure-safe.

## Acceptance Signals

- Promptfoo config validates.
- The JSON run completes and records useful failing-case diagnostics.
- The local eval DB import succeeds when requested.
- Dashboard rendering shows current case/run/review state without raw trace
  noise.
- Strict readiness exits cleanly before live `#evals` testing.
- No command posts to Slack, sends email, creates Gmail drafts, or writes
  external systems unless the user separately approves that side effect.

## Focused Tests

```bash
.venv/bin/python -m pytest tests/test_promptfoo_framework.py
.venv/bin/python -m pytest tests/test_trace_processor.py
.venv/bin/python -m pytest tests/test_slack_action_contract.py tests/test_slack_agent_actions.py
```

Run full Promptfoo JSON after provider or assertion changes:

```bash
npm run eval:promptfoo:json
```
