# KBA Architecture Reorganization

## Immutable baseline

The architecture branch starts at the annotated tag
`kba-baseline-pre-reorganization-2026-07-25`.

- Baseline commit: `b111f9b27459938879cb47344784dbeb14554173`
- Baseline tree: `83f20bbf4760e185064bc4bf1de76cdb6312a20e`
- Baseline merge on `main`: `506f39717253496b093ed61b02bdefb1241613b1`
- Baseline publication: GitHub PR 20
- Offline baseline: 4,548 tests passed, one existing skip; 7/7 graph
  scenarios and 36/36 Slack expansion cases passed with no live calls.

The private dirty-state capsule and complete-history bundle remain outside the
repository. Runtime databases, credentials, logs, caches, attachments, and
ignored local state are intentionally not part of the source baseline.

## Completeness boundary

Reorganization is an incremental extraction from the tagged tree, not a
replacement implementation. Every slice must preserve:

- `keystone_agents.cli:main`, current CLI commands, and operational script
  entrypoints;
- existing public Python import paths through compatibility facades;
- agent registry cards, builders, prompts, skills, schemas, tool policies,
  live flags, and dry-run behavior;
- direct, Slack, WorkItem, manager-loop, and LangGraph route and stage
  semantics;
- provider operation ceilings, approvals, no-send behavior, receipts,
  read-back verification, recovery state, and public results;
- serialized request, plan, WorkItem, result, and receipt compatibility.

A removed or relocated public symbol requires an explicit destination,
compatibility facade, parity test, and independently revertible commit.

## Target ownership

```text
authority/       semantic and stage-output authority
planning/        planning, completeness, bounded compatibility parsing
capabilities/    request tool scope plus model, retrieval, write, and cost admission
receipts/        mutation classification, normalization, idempotency, recovery
operations/      provider-neutral application operations using existing tools
orchestration/   WorkItem steps, manager loop, and graph coordination
runtime/         request composition, decision validation, tool execution,
                 provider context, execution attempts, and telemetry
presentation/    canonical public-result assembly and pure renderers
```

Existing `schemas/`, `tools/`, `adapters/`, `interfaces/`, and `storage/`
remain the authorities for their current concerns. No competing `contracts/`
or `integrations/` package will be introduced.

Dependency direction:

```text
entry adapters and scripts
  -> runtime
  -> planning, capabilities, orchestration, operations, receipts, presentation
  -> tools, adapters, interfaces, storage
  -> schemas
```

Orchestration must not import entry adapters. Provider modules must not import
presentation. LangGraph must consume a public stage-runner interface rather
than private `workflow_runner` helpers. Runtime is the composition root and
must not be imported by lower layers.

## Migration sequence

1. Capture completeness, parity, and latency baselines.
2. Consolidate mutation classification, receipt normalization, idempotency,
   and recovery behind current imports.
3. Introduce one request-scoped runtime and compile capabilities once.
4. Establish one canonical public-result assembler and pure renderers.
5. Separate semantic authority from bounded compatibility parsing.
6. Introduce one executable-stage runner.
7. Converge direct, Slack, WorkItem, manager-loop, and LangGraph execution.
8. Thin entry adapters and scripts.
9. Add lazy imports, adaptive retrieval, and bounded read-only concurrency.
10. Pass offline completeness, parity, safety, and latency gates before any
    separately authorized live acceptance.

Each slice is a move-behind-facade commit. Old and new execution kernels must
not run in parallel in production.

## Performance contract

Run the offline benchmark with:

```bash
PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python \
scripts/benchmark_architecture_latency.py \
  --iterations 30 \
  --json-output docs/architecture/pre_reorganization_latency.json
```

First-tranche targets, measured against the same-machine tagged baseline:

- reduce median cold CLI help time by at least 40%;
- reduce p95 cold CLI import and help time by at least 40%;
- plan once and compile capabilities once for a new request;
- perform zero planner calls for an unchanged literal continuation with a
  valid persisted plan;
- initialize one store per request-scoped runtime;
- keep equivalent model, tool, retrieval, and write ceilings unchanged or
  lower;
- complete two independent fake read stages at least 25% faster while keeping
  mutations, approvals, receipts, and provider verification serialized.

The p95 fake direct/continuation overhead target remains deferred until those
benchmark surfaces are added and recorded. Its acceptance threshold will be at
least a 25% reduction from the tagged baseline. It is not represented by the
current CLI-only post-tranche artifact.

Latency improvements must not skip Orchestrator interpretation for new asks,
deterministic safety gates, approvals, provider read-back, receipt
checkpointing, recovery, source attribution, or result validation.

## Implemented first tranche

The first behavior-preserving tranche now establishes these canonical
boundaries:

- `authority/semantic.py` owns semantic execution authority;
- `planning/compatibility.py` owns the existing bounded compatibility planner;
- `capabilities/profile.py` owns capability-profile compilation;
- `receipts/` owns mutation classification, journaling, normalization, and
  provider recovery;
- `orchestration/stages.py` exposes the one existing executable-stage kernel;
- `runtime/request.py` composes request-scoped stores and session services;
- `presentation/` owns public-result assembly, terminal consistency, and
  renderers;
- `entrypoints/cli_impl.py` owns the existing CLI command implementation while
  `keystone_agents.cli` remains a lightweight public entry facade.

The current working architecture extends those boundaries without creating a
second execution kernel:

- `agent_decision_policy.py` states which semantic choices belong to each
  registered agent and which checks remain deterministic;
- `agent_decision_contracts.py` binds structured decisions to the exact
  candidate/evidence universe for each supported stage;
- `runtime/decision_validation.py` validates model-visible evidence and returns
  fail-closed or bounded-repair outcomes without replacing the agent's choice;
- `runtime/tool_execution.py` separates attachment, model calls, workflow
  calls, helpers, pre-acquired context, provider attempts, and receipts while
  enforcing required/optional/forbidden tool postconditions;
- `runtime/provider_context.py` validates read-only cross-provider selections
  and typed downstream handoffs;
- `runtime/execution_attempt.py`, `execution_telemetry.py`, and
  `runtime/decision_trace_harness.py` preserve attempt, stage, tool, decision,
  receipt, and external-write evidence without making telemetry authoritative.

`ManualRequestPlan` remains a supported schema and compatibility/constraint
envelope. The standalone model planner is optional; no planner artifact may
override an agent-owned substantive decision or deterministic authority gate.

These modules are shared contracts, not proof of a uniform dispatcher. In the
current snapshot, `WorkflowRunner` does not dispatch the direct live Airtable,
Workspace, or Zotero routes owned by `entrypoints/cli_impl.py`; RSS and
Preprints use the signal runtime; Chief nested context agents use validated
child wrappers over agent tools. Validation and recovery coverage must therefore
be claimed from the active call site and trace, not inferred from registry or
trace-harness membership.

Validated Chief child decision records are attached durably through SDK tool
custom data, deliberately outside the model-visible and public result
envelopes, and ingested into the unified privacy-safe trace. A live
parent-supplied `run_config` is labeled `live_sdk`; nested live read tools
enforce `live=true` on isolated copies, while mutation tools remain absent.

Observability coverage is also path-specific. The production compiler now
hydrates nested decision attempts, linked manager/specialist run IDs, actual
model/tool/context/provider origins, and a privacy-safe trace join pointer while
preserving unknown historical evidence as unknown. The declarative
one-wrapper-per-agent matrix remains contract/test metadata rather than a
production capture. Correlated compiler/storage/CLI hydration is now stable.

The shared runner and signal-runtime integration have advanced beyond the
original snapshot: completed evidence is retained across one correction;
completed reads/mutations are disabled before retry; failed/unknown mutation
completion blocks automatic retry; RSS/Preprints repair replays verified
candidates without another history read; Calendar lookup and verified Gmail
continuation have one tool-free semantic repair; A/W/Z have bounded missing-tool
and semantic repair; supplied-response admission is restricted to evidenced
provider-free synthesis; and Calendar decision telemetry reaches the CLI trace.
Live Research/Opportunity WorkItems now use agent-owned SDK retrieval,
selection, ranking, and handoff over model-visible stable provider candidate
IDs, with one no-reread semantic repair. Python validates identity, bounds, and
formal gates. A/W/Z remain outside first-class `WorkflowRunner` specialist
dispatch.

For A/W/Z specifically, provider-authoritative candidate binding, semantic
repair, bounded missing-tool correction, cumulative telemetry, and mutation-safe
evidence are stable; only first-class `WorkflowRunner` dispatch parity remains
an architectural distinction.

Legacy module paths remain import-compatible facades, so operational scripts,
tests, direct calls, Slack, WorkItem, manager-loop, and LangGraph consumers can
migrate independently. The moved implementations preserve their original
behavior and structured schemas; no parallel planner, execution kernel,
provider adapter, result contract, or phrase-specific parser was introduced.

The current `tools/` package remains the provider integration boundary.
Creating a second provider abstraction or an empty `operations/` hierarchy
would add ambiguity without improving reliability, so provider-neutral
operation extraction is deferred until a concrete operation can move with
parity coverage. Likewise, adaptive retrieval and new read concurrency are
deferred until their ordering, budget, source-attribution, and cancellation
contracts can be proven independently.

The large compatibility implementations in `planning/compatibility.py` and
`entrypoints/cli_impl.py` are intentionally transitional. Later slices may
extract cohesive planner services and CLI command families from them, one
behavior-covered seam at a time. They must not be replaced wholesale.

## First-tranche evidence

The corrected isolated no-live gate completed with 4,599 tests passed, 15
skipped, and two worktree-host checks deselected. Those two checks require
worktree-local ignored dashboard/runtime paths and were run or assessed
separately; neither exercises live model or provider behavior. Additional
offline gates passed:

- 7/7 graph scenarios;
- 36/36 Slack expansion scenarios;
- 12/12 advanced manager acceptance scenarios;
- manager delegation and Slack entrypoint readiness;
- zero OpenAI API requests in the acceptance gates.

The post-tranche cold-process benchmark used the same Python version, machine,
30 samples, three warmups, and credential-free environment as the tagged
baseline. Results are recorded in
`docs/architecture/post_reorganization_latency.json`.

| Surface | Baseline median | Tranche median | Reduction |
| --- | ---: | ---: | ---: |
| `import keystone_agents.cli` | 1,427.809 ms | 20.868 ms | 98.54% |
| `python -m keystone_agents.cli --help` | 1,425.694 ms | 24.650 ms | 98.27% |

The corresponding p95 reductions are 98.22% for CLI import and 98.00% for CLI
help. The recorded-report test enforces at least a 40% reduction for both
median and p95 on both CLI surfaces.

This improvement comes from deferring the heavyweight CLI implementation until
a command actually needs it. It does not bypass planning, safety checks,
provider verification, receipts, or result validation during execution.

## Remaining acceptance boundary

The frozen baseline checkout and tag remain unchanged. Architecture work stays
on the sibling worktree and is not ready for publication solely because the
offline tranche passes. Before publication:

1. obtain independent read-only validation for every remaining slice;
2. rerun the complete no-live gate on the exact proposed head;
3. review the cumulative diff against the baseline for schema and safety
   drift;
4. publish only focused, reviewable commits or stacked pull requests;
5. run live direct, Slack, provider, and LangGraph acceptance only as a
   separately authorized, serial, cost-bounded step.

Before any live architecture acceptance, use an architecture-worktree virtual
environment rather than the frozen checkout's shared interpreter and run:

```bash
.venv/bin/python scripts/assert_runtime_checkout.py \
  --expected-root /path/to/keystone-business-agents \
  --require-worktree-venv \
  --json
```

The gate must show that `keystone_agents`, `keystone_agents.cli`, and
`keystone_agents.authority.semantic` all resolve under the intended architecture
checkout. A Slack worker must not be restarted for architecture acceptance until
this gate passes and its `KNI_BUSINESS_AGENTS_PYTHON` and
`KNI_BUSINESS_AGENTS_REPO` runtime overrides point to that environment and
checkout.
