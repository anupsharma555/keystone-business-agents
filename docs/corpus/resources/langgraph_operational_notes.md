# LangGraph Operational Notes For Keystone

Sources summarized:

- https://docs.langchain.com/oss/python/langgraph/overview
- https://docs.langchain.com/oss/python/langgraph/graph-api
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://docs.langchain.com/oss/python/langgraph/interrupts
- https://docs.langchain.com/oss/python/langgraph/durable-execution
- https://docs.langchain.com/oss/python/langgraph/fault-tolerance
- https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph
- https://docs.langchain.com/oss/python/langgraph/test

## Keystone Position

LangGraph is useful for durable orchestration, not as a replacement for the
OpenAI Agents SDK specialist layer. Keystone should keep specialist behavior in
SDK agents and use LangGraph around WorkItem advancement, approval checkpoints,
retries, resumption, and future multi-step routing.

## Concepts To Retrieve

- `StateGraph`: define typed graph state, nodes, and edges. Keystone graph state
  should remain serializable and derived from WorkItems, context packs, and
  structured agent outputs.
- `START` and `END`: explicit graph boundaries. Keystone should keep node paths
  auditable and avoid hidden prompt-driven routing in edge functions.
- Checkpointers and threads: persistence stores graph state snapshots by thread.
  Keystone should use stable WorkItem-derived thread ids and keep SQLite
  WorkItems as canonical audit state.
- Interrupts: pause execution for human review and resume with human input.
  Keystone approval checkpoints should be explicit before outbound drafts are
  used, sent, posted, scheduled, or handed to live integrations.
- Durable execution: persist progress so long-running or interrupted workflows
  can resume. Nodes must be idempotent around side effects.
- Fault tolerance: add retry, timeout, and error behavior per node only where a
  deterministic retry is safe.
- Subgraphs: compose reusable workflow sections when shared state contracts are
  clear. Keystone can later model Gmail triage, account research, opportunity
  scoring, and outreach drafting as subgraphs while still calling SDK agents
  inside nodes.
- Testing: validate nodes and graphs with controlled state, fake dependencies,
  and deterministic expectations before enabling live integrations.

## Operational Guidance

- Keep LangGraph as an optional `orchestration` extra.
- Do not require LangGraph for individual specialist CLI execution.
- Do not put business rules only in graph edges. Python gates remain
  authoritative for readiness, approvals, no-PHI behavior, and no-send rules.
- Do not persist raw Slack exports, Gmail bodies, credentials, PHI, or
  patient-specific data in checkpoints or traces.
- For Slack-triggered WorkItems, record graph runtime, node path, checkpoint
  requirement, and graph thread id in timeline metadata.
- Prefer one node per meaningful operational boundary: classify, retrieve,
  specialist call, deterministic gate, approval checkpoint, storage, report.
- Treat graph resumption as stateful execution, not permission to bypass human
  approval.

## Fit With Existing Keystone Work

The current thin wrapper around `advance_work_item()` is the right starting
shape. Future expansion should add nodes only when they improve observability,
resumption, approval checkpointing, or retry behavior without changing the
specialist agent contracts.
