---
name: kba-agent-run-diagnosis
description: Codex-side run evidence and root-cause diagnosis for the keystone-business-agents repo. Use only in this repo when a KBA run failed, blocked, lost state, used an unexpected route/tool/provider, or has unclear receipts or runtime freshness. This diagnoses recorded execution; it does not certify end-to-end business quality or operational acceptance.
---

# KBA Agent Run Diagnosis

Use this skill only for `<repo>`. A selected isolated worktree of this same
project is also valid when the task explicitly scopes work there. If the working directory is
not this repo or one of those selected worktrees, stop and switch to the correct checkout before continuing. Keep
diagnosis read-only unless the user separately asks for a fix, retry, restart,
provider call, or tracker update.

This skill reconstructs and labels Codex-visible run evidence. Its `PASS`,
`PARTIAL`, and `FAIL` verdicts describe the selected evidence packet, not the
business answer's usefulness or end-to-end operational acceptance. Use
`$kba-operational-validation` for that acceptance decision.

## First Reads

1. Preserve the exact operator request, Slack channel/thread/request timestamp,
   KBA run ID, WorkItem ID, and follow-up wording that are available.
2. Read `AGENTS.md`, current `git status --short`, and
   `references/diagnosis-model.md`.
3. Read the narrow companion skill when the run involves WorkItems,
   search/retrieval, live model cost, or operational acceptance.
4. Treat the current checkout, provider receipts, local state, and visible Slack
   output as separate evidence surfaces. Do not infer one from another.
5. For Slack-backed runs, distinguish repository readiness from loaded-runtime
   readiness. Inspect the socket health record, long-lived worker status, relevant
   source modification times, and restart/reproof status before attributing the
   run to current code. When present, compare the privacy-safe KBA child
   `runtime_fingerprint` recorded in both the persisted agent run and Slack attempt;
   a matching fingerprint is stronger loaded-code evidence than file mtimes, while
   a mismatch is a runtime-deployment failure.

For a Slack-backed run, collect the local evidence packet first:

```bash
.venv/bin/python codex-skills/kba-agent-run-diagnosis/scripts/collect_run_evidence.py \
  --channel-id <channel> --thread-ts <thread> --format markdown
```

Use `--slack-run-id`, `--request-ts`, `--work-item-id`, or `--agent-run-id` when
those are the stronger identity. Add expected evidence only after deriving it
from the request and current code:

```bash
.venv/bin/python codex-skills/kba-agent-run-diagnosis/scripts/collect_run_evidence.py \
  --channel-id <channel> --thread-ts <thread> \
  --expect-agent <agent> \
  --expect-provider <provider> \
  --expect-attached-tool <model-visible-tool> \
  --expect-called-tool <model-tool-or-workflow-helper> \
  --expect-receipt-operation <provider-operation> \
  --format markdown
```

The collector reads the Slack bridge state, KBA state, and configured local
trace-summary database. It intentionally summarizes rather than dumping raw
provider values, document bodies, message contents, tokens, or credentials.
Its verdict is terminal-aware: the current Slack projection is authoritative
when present, otherwise the newest selected Slack attempt, agent run, or WorkItem
is used. A current failure returns `FAIL`; blocked, pending, or unknown
non-success cannot return `PASS`; a latest completed attempt can supersede a
historical failed attempt. Heuristic silence alone is not success.
For a direct CLI diagnosis selected with `--agent-run-id`, it prioritizes the
terminal direct-agent envelope over nested Orchestrator preflight evidence.
Historical receipts that omitted provider or operation identity remain
incomplete evidence; do not infer those fields from a successful tool result.

## Request Shapes

- A failed, partial, blocked, malformed, or unexpectedly tool-free agent run.
- A suspected wrong-agent, wrong-provider, missing-tool, or fallback defect.
- A Slack continuation or retry that may have lost or overwritten prior state.
- A request to reconstruct agents, states, tool calls, receipts, validation,
  rendering, cost, and missing evidence without changing external systems.

## Workflow

## Reconstruct The Run

Build one chronological chain:

1. **Entry:** original request, selected Slack context, entrypoint, flags, and
   whether this was a root request, continuation, retry, or action.
2. **Interpretation:** requested agent, selected owner, intent, provider,
   operation, permission state, target, desired count, and constraints.
3. **State:** direct request state, WorkItem/context pack, LangGraph nodes,
   approvals, blockers, and prior-thread evidence actually admitted.
4. **Tool admission:** registry-declared tools, builder candidates,
   request-scoped attached tools, omitted tools, and why the scope was chosen.
5. **Tool execution:** model-called functions, workflow-managed helpers,
   provider calls, retries/fallbacks, and postconditions. Keep these categories
   separate; zero model tool calls can be correct when a deterministic workflow
   helper executed and produced a receipt. Repeated invocations count
   separately even when their tool name is the same.
6. **Provider proof:** provider identity, operation, bounded target, status,
   verification/read-back, source or record IDs, and durable receipt location.
7. **Reasoning and validation:** specialist result schema, Orchestrator review,
   deterministic gates, repair/fallback, block kind, and validation exception.
8. **Presentation:** renderer selection, public result, Slack delivery/thread,
   duplication, truncation, generic fallthroughs, and visible limitations.
9. **Continuity:** per-attempt persistence, retry identity, follow-up authority,
   overwritten state, and whether an explanation request used the failed run's
   actual evidence.
10. **Runtime alignment:** Slack worker health, process/reconnect time, relevant
    source freshness, loaded revision or fingerprint when recorded, whether a
    restart occurred after local edits, and whether the diagnosed behavior was
    re-proved after that restart. Healthy websocket transport is not proof that
    the worker loaded current dispatch, persistence, or rendering code.

Treat `business_agent_slack_runs` as the per-attempt evidence source and
`business_agent_slack_run_records` as the latest thread projection. If only the
projection exists for a multi-turn thread, report earlier attempt details as
`MISSING`; do not reconstruct them from the latest row.

At every step label the evidence `PROVEN`, `INFERRED`, `MISSING`,
`INCONSISTENT`, or `NOT_APPLICABLE`. Never convert an inference into a fact
because the expected code path exists.

## Derive Expected Agents, States, And Tools

Do not derive expectations from the failed output alone.

1. Map the raw ask to one owner and execution shape using `agent_registry.py`,
   the relevant agent builder, `agent_tool_policy.py`, request tool scoping, and
   the canonical plan/context schema.
2. Identify whether each expected operation is a model-visible function tool,
   a workflow-managed deterministic helper, a provider adapter, or a renderer.
3. Inspect the matching focused tests to determine the intended state and
   receipt contract across wording variants.
4. Compare expected versus candidate, attached, called, completed, and
   receipt-producing tools. Tool registration or attachment alone is not proof
   of execution.
5. Flag prompt-specific routing as a defect. Recommend a schema, context, tool,
   or shared admission-contract change instead of a phrase shortcut.

## Classify The First Failed Layer

Choose one primary layer and list downstream consequences separately:

- entry/context capture
- semantic interpretation or ownership
- route/backend selection
- state/context-pack/approval
- tool registration or registry drift
- request-scoped tool admission
- model tool selection
- workflow helper execution
- provider/authentication/fallback
- receipt capture or recovery
- synthesis/schema validation
- safety or deterministic gate
- renderer/delivery
- continuation/persistence
- runtime deployment alignment
- observability only

Stop at the earliest layer contradicted by evidence. A later safe block can be
correct even when an earlier functional layer failed.

## Rank Causes Before Recommending A Change

For every failed, partial, or blocked result, explain why it may have happened,
not only which symptom or layer failed. Use the ranked-cause table in
`references/diagnosis-model.md`; keep only plausible causes supported by the
available evidence, and do not invent alternatives when one cause is proved.

Lead with the best-supported explanation and a justified confidence level.
Separate the observed failure location from its underlying cause and possible
contributors. If the cause is unresolved, say so and rank the remaining
hypotheses rather than naming one as established. For each, identify supporting
and contrary evidence, what is still missing, and the smallest next check that
would distinguish it from the alternatives.

For model-related failures, inspect the actual admitted context/instructions and
relevant tool, constraint and output-processing evidence before attributing the
problem to model capacity. A prompt gap and variable model behavior may coexist.
Offline scripted responses prove wiring, not live model quality. A diagnostic
model comparison changes one stage at a time while holding the request,
evidence, instructions, tools and compatible limits fixed; label any unavoidable
confounder. One improved rerun is evidence of sensitivity, not proof that an
upgrade fixes the class of failures.

Even a short progress update should include the leading cause or hypothesis,
its evidence/uncertainty, and the next discriminating check. Scale the detail to
the failure; this reporting requirement does not authorize a retry or create a
new execution blocker.

## Report For A Human

Use the report structure in `references/diagnosis-model.md`. Lead with the
plain-language outcome, then include:

- run identity and exact request
- intended versus actual agent path
- state timeline
- expected, attached, called, and receipt-producing tools
- provider/model/request/cost evidence
- local-code versus loaded-Slack-runtime freshness and restart/reproof status
- first failed layer and downstream symptoms
- ranked plausible causes, leading explanation/confidence, and the next distinguishing check
- what worked, including safety behavior
- what evidence was missing or overwritten
- generalized corrective actions and the smallest regression proof

End with prioritized next steps. Do not implement, rerun, restart, or update
Linear unless the user authorized that separate action.

## Verification

Before closing a diagnosis:

1. Re-read the cited local rows/artifacts and visible Slack thread.
2. Prove expected behavior with the smallest focused offline tests.
3. State what cannot be known because evidence was never recorded or was
   overwritten.
4. Distinguish the diagnosed root cause from proposed fixes and unrun live
   validation.
5. Confirm provider request attempts, successful responses, durable receipts,
   model-called tools, workflow-called tools, and workflow helpers are reported
   as separate counters rather than inferred from one another.
6. For Slack-backed runs, do not call a fix live-ready until the affected worker
   was restarted after relevant source edits and health was rechecked. Do not
   call it live-proven until a bounded post-restart run exercises the corrected
   path. A reconnect timestamp alone is insufficient proof of code reload.
7. After changing the collector, run
   `tests/test_kba_agent_run_diagnosis_skill.py` and regenerate one saved
   diagnosis packet to confirm direct and nested evidence are still separated.
8. Confirm the saved packet reports the KBA child runtime hash and process-start
   timestamp without source text, environment values, secrets, or credentials.

The focused local gate is:

```bash
.venv/bin/python -m pytest tests/test_kba_agent_run_diagnosis_skill.py -q
```

This proves collector behavior and report separation only. It does not prove a
useful answer, provider correctness, Slack presentation, or live readiness.

Known evidence boundary: a nonzero child process can still carry structured
tool and failure metadata in JSON stdout. Inspect that payload before treating
the process exit code as the only evidence. If a provider response is visible
but no durable receipt exists, report the provider outcome as unknowable rather
than silently converting it to either success or non-execution.
