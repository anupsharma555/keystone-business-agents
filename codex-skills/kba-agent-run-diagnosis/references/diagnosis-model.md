# KBA Run Diagnosis Model

Use this evidence model only for `<repo>`. It defines the common run layers and
human-facing report contract; provider-specific expectations remain inputs to
the diagnosis rather than hard-coded answers.

## Evidence Priority

Use the strongest available evidence for each claim:

1. Provider verification/read-back receipt or source record.
2. Tool completion/postcondition event tied to the run.
3. Canonical WorkItem, context pack, approval, audit, or graph node event.
4. Persisted agent run output and structured trace metadata.
5. Slack bridge run record and delivery evidence.
6. Visible Slack response.
7. Slack process-start/loaded-revision evidence and runtime health.
8. Current code and tests as evidence of intended behavior only.

Current code proves what should happen, not what happened in an earlier run.

## Evidence Terms

| Term | Meaning | Does not prove |
|---|---|---|
| Registered | Listed in the agent card or tool registry | Attached to this request |
| Candidate | Available before request scoping | Exposed to the model |
| Attached | Included in this model request | Called or completed |
| Model-called | Selected through an SDK function call | Provider success |
| Workflow-called | Invoked deterministically outside model tool selection | Model selected it |
| Provider receipt | Bounded provider result with operation/status/verification | Good synthesis or delivery |
| Rendered result | User-facing text produced | Correct route or provider execution |

## Run Matrix

Record every row as `PROVEN`, `INFERRED`, `MISSING`, `INCONSISTENT`, or
`NOT_APPLICABLE`.

| Layer | Expected evidence | Typical local evidence |
|---|---|---|
| Input | Exact raw ask and selected thread context | Slack root/reply, execution context |
| Ownership | Requested and selected agent | preflight, route result, registry |
| Semantic state | Intent, provider, operation, permission, target | canonical manual plan, action plan |
| Durable state | WorkItem, context pack, approval, blockers | SQLite rows, events, artifacts |
| Tool admission | Candidate and attached names with scope reason | request tool-scope receipt |
| Tool execution | Called/completed names and postconditions | SDK trace, tool events, helper result |
| Provider | Identity, operation, status, verification | provider receipt or read-back |
| Reasoning | Specialist schema and bounded synthesis | persisted output, review events |
| Gate | Exact pass/block and authority | block kind, approval/safety event |
| Presentation | Renderer branch and public output | public result, Slack record/thread |
| Continuity | Per-attempt identity and retained evidence | run rows, retry IDs, follow-up envelope |
| Runtime alignment | Worker health and loaded-code freshness | health record, process start, source mtimes, loaded revision/fingerprint, restart evidence |
| Consumption | Requests, tokens, cost, latency | usage/cost/telemetry summaries |

## Diagnostic Decision Rules

- If an expected tool is absent from request scope, classify tool admission.
- If attached but not called and no workflow helper ran, classify tool selection
  or required-tool enforcement.
- If a helper/tool ran but no provider result exists, classify execution or
  provider failure.
- If a provider result exists but is not journaled through validation/fallback,
  classify receipt capture/recovery.
- If verified evidence exists but structured output is invalid, classify
  synthesis/schema validation.
- If valid structured output exists but Slack shows generic or misleading text,
  classify renderer/bridge behavior.
- If a follow-up cannot recover the prior failure evidence or overwrites it,
  classify continuation/persistence.
- If local tests pass but relevant Slack source is newer than the long-lived
  worker, classify runtime deployment alignment before blaming current routing.
  Healthy websocket transport proves connectivity, not code reload.
- If execution may have succeeded but no durable evidence distinguishes the
  possibilities, classify observability as a confirmed defect and state that
  the execution outcome is unknowable.

## Human-Facing Report Template

### Outcome

State `PASS`, `PARTIAL`, `FAIL`, or `BLOCKED`, the first failed layer, and the
plain-language consequence in two or three sentences.

### Run details

- Request, timestamps, Slack permalink/thread, run IDs, WorkItem ID
- Entrypoint and direct/WorkItem/LangGraph execution shape
- Slack runtime health, freshness, restart, and post-restart proof status
- Model, requests, tokens, cost, latency when available

### Agents and states

List intended owner, actual owner, specialists/handoffs, canonical semantic
state, durable state, approvals, blockers, and continuation state.

### Tools and providers

Use one compact table:

| Evidence | Expected | Actual | Status |
|---|---|---|---|
| Tool registration | | | |
| Request attachment | | | |
| Model calls | | | |
| Workflow helper calls | | | |
| Provider receipts | | | |
| Fallback/retry | | | |

### What happened

Explain the chronological causal chain. Separate the root cause from safe
fallbacks and downstream presentation symptoms.

### Ranked causes and next diagnostic check

Start with the most likely explanation and why it ranks first. Then use a small
table when alternatives remain:

| Rank | Possible cause | Supporting evidence | Contrary or missing evidence | Confidence | Next distinguishing check |
|---|---|---|---|---|---|

Use evidence-based high/medium/low confidence with a short justification, not
invented numerical probabilities. Separate a proven mechanism from a plausible
contributor and a still-unresolved hypothesis. Several causes may interact.
Do not fill rows with unsupported possibilities or call a symptom its own cause.
When the evidence supports only one cause, a short explanation is sufficient.

Choose the cheapest useful check that would change the ranking: inspection,
replay or a focused offline control before a separately authorized live test.
State what each possible test outcome would support. For a model comparison,
freeze the same input, prompt, evidence and execution scope and change one model
stage at a time. Record whether the run is planned, performed, or reviewed;
changing both the prompt and model does not isolate either cause.

### What was missed in diagnosis

List absent, overwritten, uncorrelated, or non-durable evidence. Say which
questions therefore remain unanswerable.

### Next steps

Prioritize shared-contract fixes, durable evidence, focused regression tests,
and one bounded live reproof. Avoid prompt-specific fixes or broad live suites.
