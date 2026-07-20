# KBA Live SDK Smoke And Cost Flow

Load this reference when running or debugging live SDK smoke tests, model
configuration, traces, cost, rate limits, or request-cache behavior in
`<repo>`.

## Credential Rules

- Use `KEYSTONE_OPENAI_API_KEY` for this repo.
- Do not assume generic `OPENAI_API_KEY` belongs to this project.
- If a raw SDK snippet requires `OPENAI_API_KEY`, map
  `KEYSTONE_OPENAI_API_KEY` to `OPENAI_API_KEY` only for that process.
- Never print, log, commit, or echo secrets.

## Live Mode Rules

- Live SDK execution is separate from agent construction.
- Tests should build agents without credentials.
- Missing live credentials should fail clearly before model calls.
- Live model execution does not authorize Slack posts, Gmail drafts, sends,
  CRM writes, scheduling, publication, or external side effects.
- Prefer `--no-live-sdk` when the user asks for dry-run/fixture behavior.

## Useful Entry Points

Explicit named-agent ask:

```bash
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health"
```

Force dry-run for named-agent handling:

```bash
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health" --no-live-sdk
```

Representative specialist scripts:

```bash
.venv/bin/python scripts/run_company_research.py --company Curebase --fixture tests/fixtures/sample_company_curebase.json --markdown
.venv/bin/python scripts/run_opportunity_scout.py --topic "behavioral health AI" --markdown
.venv/bin/python scripts/run_gmail_triage.py --fixture tests/fixtures/sample_email_vendor.txt --json
.venv/bin/python scripts/run_outreach_draft.py --fixture sample_company_curebase --opportunity-fixture sample_lead_curebase
```

Cost comparison and benchmark helpers:

```bash
.venv/bin/python scripts/compare_agent_run_costs.py --help
.venv/bin/python scripts/summarize_benchmark_results.py
```

## Cost And Trace Evidence

When claiming live behavior was measured, inspect the relevant evidence:

- SDK payload `usage`, `cost`, `budget_guard`, and `request_cache`.
- WorkItem `workflow_sdk_usage` events.
- `keystone.sdk_run_summary.v1` trace events.
- Local benchmark rows when `--record-benchmark` was used.
- Operator-provided platform actual cost only when the operator supplies it for
  the matching run/window.

Always report three request values separately:

- Ceiling: the configured maximum allowed for the command.
- Estimate: conservative preflight minimum/maximum before execution.
- Actual: requests recorded by the completed or blocked run.

A route estimated above its ceiling may block before any model call and report
`actual=0`.

## Rate Limit Handling

- Treat HTTP 429 as recoverable throttle.
- Wait for provider `retry-after` or reset timing when available.
- Do not hot-loop retries; failed requests may still count against limits.
- Reduce prompt and output budget before retrying large evals.
- Prefer compact source facts, capped source bundles, and smaller approved
  models for smoke runs when quality allows.

## Prior Failure Anchors

- Raw SDK snippets failed or hit the wrong project when they expected generic
  `OPENAI_API_KEY`; map `KEYSTONE_OPENAI_API_KEY` only inside the child process.
- Architecture guidance drifted when SDK sessions were treated as canonical
  business state; use WorkItems/local storage/context packs/approvals/artifacts
  as the state model.
- Slack cost investigations missed preflight/final-synthesis cost when they
  inspected only `agent_runs`; include WorkItem-level `workflow_sdk_usage`.
- Live SDK rate-limit retries can worsen TPM pressure if they hot-loop.

## Focused Tests

```bash
.venv/bin/python -m pytest tests/test_model_provider.py tests/test_sdk_execution.py tests/test_sdk_sessions.py
.venv/bin/python -m pytest tests/test_costing.py tests/test_cost_tracking.py tests/test_cost_experiments.py
.venv/bin/python -m pytest tests/test_trace_processor.py tests/test_benchmark_tracking.py
```

## Stop Conditions

Stop and ask or report a blocker when:

- The user did not request live model execution and fixture validation can answer
  the question.
- Required credentials or gateway configuration are missing.
- The live run would create external side effects.
- Exact cost cannot be known from provider usage, maintained pricing, or
  operator-supplied platform data.
