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
