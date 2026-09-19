# Structured Runtime Logging

Keystone local runtime logs that are intended for machine review should include
a `keystone.structured_log.v1` envelope.

Required fields:

- `schema`: `keystone.structured_log.v1`
- `timestamp`: UTC ISO-8601 timestamp
- `level`: `debug`, `info`, `warning`, or `error`
- `component`: emitting process or module
- `event`: concise event name
- `pid`: local process id
- `correlation`: safe join keys such as `run_id`, `case_id`, `eval_id`,
  `work_item_id`, `trace_id`, `agent`, `route`, `stage`, `status`,
  `failure_kind`, `slack_channel_id`, and `slack_thread_ts`
- `redaction`: redaction status and `raw_payload_included=false`
- `payload`: bounded redacted metadata

Do not place raw prompts, model responses, Slack/Gmail message bodies, tool
inputs/outputs, child stdout/stderr, secrets, credentials, PHI, or
patient-specific content in structured logs. Body-like fields should be stored
only as bounded summaries and lengths. Use durable stores such as WorkItems,
eval trace events, human-review rows, and approved artifacts for canonical
state; structured logs are operational diagnostics.

## Decision, Tool, And Receipt Observability

Logs may reference the corresponding trace-safe runtime records, but should not
flatten them into one ambiguous `tools_used` field:

- decision owner, decision stage, attempt ordinal, validator status, reason
  code, and whether bounded repair was requested;
- selected/excluded candidate fingerprints, never raw private candidate
  payloads or unbounded identifiers;
- request-scoped attached tools separately from model-called tools,
  workflow-called tools, workflow helpers, and pre-acquired context tools;
- model requests made separately from actual SDK tool-call items and returned
  tool outputs. A decision event whose compatibility `tool_mode` says
  `model_called` is not sufficient tool-call proof by itself;
- the hard request-local model ledger separately from the semantic request
  estimate. Preserve its `correlation_id`, `limit`, `consumed`, `remaining`,
  `exhausted`, `exhaustion_stage`, and pre-model-invocation enforcement status
  at terminal success, block, or failure;
- route-sensitive semantic stage rows for the initial model loop, conditional
  required-tool correction, and conditional decision/validator repair.
  Transport and structured-output retries are not semantic stage rows, but any
  model request they start is still counted by the hard ledger;
- provider request attempts separately from successful provider requests and
  durable provider receipts;
- tool postcondition status, completed-stage receipt count, and safe retry
  point;
- external-write state as `performed`, `not_performed`, or `unknown`. Missing
  evidence must remain `unknown`, not be converted to `false`.

`keystone.execution_telemetry.v1` stage spans, SDK run summaries, WorkItem
events, provider receipts, and backend decision traces are different evidence
surfaces intended to be joined by safe run/trace/WorkItem identifiers. Structured logs may
summarize them for operations, but they are not the canonical decision,
receipt, approval, or workflow state.

Validated Chief child decisions are retained through `ToolContext._custom_data`
and `ToolCallOutputItem.custom_data`. They stay out of the model-visible and
public result envelopes and are ingested into the unified privacy-safe trace.
Nested read-enforcement evidence records effective `live=true`, and a live
parent-supplied `run_config` is labeled `live_sdk`.

The production trace compiler now hydrates nested decision attempts, links
manager and specialist run IDs, preserves actual model/tool/context/provider
origins, writes a privacy-safe trace join pointer, and leaves unknown historical
evidence unknown. The declarative trace harness and symbol-resolution matrix are
still not production captures. Correlated compiler/storage/CLI hydration is
stable. Calendar lookup decision telemetry is preserved in the CLI payload and
correlated trace.
