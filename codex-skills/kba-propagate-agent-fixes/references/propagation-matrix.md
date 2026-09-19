# KBA Fix Propagation Matrix

Use this reference only for `<repo>`. Fill only rows applicable to the diagnosed
contract; do not create changes merely to make every row non-empty.

| Seam | Inspect | Core question |
| --- | --- | --- |
| Canonical semantics | `src/keystone_agents/authority/semantic.py`, `src/keystone_agents/planning/compatibility.py` | Does the same request retain owner, operation, target, permission, and prohibitions? |
| Capability scope | `src/keystone_agents/capabilities/`, `src/keystone_agents/agent_tool_policy.py` | Do direct, CoS, and orchestrated paths attach the same bounded capability? |
| Agent composition | `src/keystone_agents/agents/`, `src/keystone_agents/agent_registry.py` | Which builders consume the shared seam, and which are intentionally narrower? |
| Direct runtime | `src/keystone_agents/entrypoints/cli_impl.py`, `src/keystone_agents/sdk_run_policy.py` | Are there enough bounded turns for reads, mutation, verification, and final synthesis? |
| WorkItem and graph | `src/keystone_agents/workflow_runner.py`, `src/keystone_agents/work_items.py`, `src/keystone_agents/langgraph_workflow.py` | Does state preserve the same plan, approval, evidence, and completed stages? |
| Provider tool | `src/keystone_agents/tools/` | Is schema inspected, identity exact, mutation scoped, and read-back verified at the provider boundary? |
| Receipt and recovery | `src/keystone_agents/receipts/`, `src/keystone_agents/tool_receipt_journal.py` | Can completed side effects survive synthesis, repair, timeout, or max-turn failure without unsafe repetition? |
| Decision validation | `src/keystone_agents/runtime/decision_validation.py`, `src/keystone_agents/presentation/consistency.py` | Is model-owned reasoning checked against the complete visible evidence without deterministic substitution? |
| Public result | `src/keystone_agents/presentation/`, Slack bridge contracts | Does the visible status match provider truth and state the next safe action? |
| Persistence and trace | `src/keystone_agents/storage/`, `src/keystone_agents/execution_telemetry.py` | Can a later diagnosis correlate attempts, tools, receipts, usage, latency, and loaded runtime? |
| Tests and evals | `tests/`, `evals/` | Do two distinct consumers and one failure edge prove the shared contract? |

## Structured-System Ownership Check

For Airtable, Google Workspace, Gmail, Calendar, CRM, or another structured
provider, classify both tool capability and mutation ownership. Shared tools do
not imply that two agents should both execute the same side effect.

| Path | Expected ownership |
| --- | --- |
| Direct provider specialist | The specialist performs schema/read-before-write, the one approved mutation, and provider verification. |
| Unqualified provider-local request | Route to the provider specialist rather than adding a manager hop. |
| Explicit direct manager request | The manager may use the same shared bounded provider tool when its canonical plan and exact write scope admit it. |
| Manager calling a nested context specialist | The nested specialist is read/plan-only unless a dedicated mutation-handoff contract explicitly transfers ownership; exactly one layer may mutate. |
| Multi-provider or graph workflow | The manager coordinates evidence and stages, while the provider mutation remains assigned to one named owner and one receipt lineage. |

Read-before-write is a provider safety contract, not a reason by itself to add a
second agent. Use a manager hop only when it contributes cross-system context,
sequencing, or decision coordination.

## Stop Rules

- Stop if the root cause is still `INFERRED` or contradicted by provider proof.
- Stop before any live retry when a write may already have succeeded; reconcile
  the exact provider object first.
- Stop rather than widen a tool, approval, or provider scope to make a test pass.
- Stop after one conclusive live reproof unless the operator explicitly approves
  another materially different row.
- Leave `UNPROVEN` rows visible instead of converting them into speculative code.

## Propagation Report

Return a compact report with:

1. diagnosed contract and first failed layer;
2. shared repair boundary;
3. matrix rows and classifications;
4. focused offline evidence;
5. live proof or explicit no-live boundary;
6. remaining risks and the next smallest test.
