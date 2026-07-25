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
capabilities/    tool, model, retrieval, write, and cost admission
receipts/        mutation classification, normalization, idempotency, recovery
operations/      provider-neutral application operations using existing tools
orchestration/   WorkItem steps, manager loop, and graph coordination
runtime/         one request-scoped composition root
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
/Users/anup/gitProjects/keystone-business-agents/.venv/bin/python \
scripts/benchmark_architecture_latency.py \
  --iterations 30 \
  --json-output docs/architecture/pre_reorganization_latency.json
```

Initial targets, measured against the same-machine tagged baseline:

- reduce median cold CLI help time by at least 40%;
- reduce p95 fake direct and continuation framework overhead by at least 25%;
- plan once and compile capabilities once for a new request;
- perform zero planner calls for an unchanged literal continuation with a
  valid persisted plan;
- initialize one store per request-scoped runtime;
- keep equivalent model, tool, retrieval, and write ceilings unchanged or
  lower;
- complete two independent fake read stages at least 25% faster while keeping
  mutations, approvals, receipts, and provider verification serialized.

Latency improvements must not skip Orchestrator interpretation for new asks,
deterministic safety gates, approvals, provider read-back, receipt
checkpointing, recovery, source attribution, or result validation.
