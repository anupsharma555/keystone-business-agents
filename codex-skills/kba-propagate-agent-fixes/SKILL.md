---
name: kba-propagate-agent-fixes
description: Repo-local KBA fix-propagation workflow for the keystone-business-agents repo. Use only in this repo after one agent run or workflow defect has a supported root cause and the operator has authorized a fix, when Codex must inspect analogous agents, direct and orchestrated routes, shared tools, receipts, validation, recovery, and Slack presentation so one repair prevents the same failure across KBA rather than becoming a prompt-specific patch.
---

# KBA Propagate Agent Fixes

Use this skill only for `<repo>`. If the working directory is
not this repo, stop and switch to the correct checkout before continuing. This
skill authorizes no provider call, Slack retry, deployment, tracker write, or
unrelated refactor beyond the operator's existing scope.

## First Reads

1. Read `AGENTS.md`, current `git status --short`, and
   `references/propagation-matrix.md`.
2. Read the diagnosed run packet and the narrow source, tests, prompt, schema,
   tool, receipt, validation, renderer, and runtime files identified there.
3. If the first failed layer or root cause is not supported by current evidence,
   stop propagation and use `$kba-agent-run-diagnosis` first.
4. Preserve the dirty checkout. Identify which relevant changes predate this
   task before editing shared files.
5. Use `$kba-agent-contract-change` when the repair changes agent prompts,
   schemas, tools, context packs, builders, registry metadata, or handoffs.

## Request Shapes

- Apply one diagnosed agent fix across all analogous KBA agents or workflows.
- Determine whether CoS, a context agent, a direct named-agent run, WorkItem,
  LangGraph, or Slack can encounter the same defect.
- Replace repeated route-specific fixes with one shared semantic, tool,
  receipt, validation, recovery, or presentation contract.
- Add regression coverage that proves the shared repair without spending a
  broad set of live API calls.

## Workflow

### 1. Freeze The Diagnosed Contract

Write down the exact request shape, intended owner, first failed layer, root
cause, safety boundary, provider truth, visible symptom, and smallest acceptable
behavior. Separate a functional defect from an observability or presentation
defect. A successful provider mutation followed by a failed final synthesis is
both a completed side effect and a completion-reporting defect; never treat it
as proof that nothing happened or automatically repeat the mutation.

### 2. Build The Propagation Matrix

Inspect every applicable seam in `references/propagation-matrix.md`. Classify
each row as:

- `SAME_DEFECT`: the same root cause is reachable here.
- `SHARED_SEAM_COVERED`: repairing the shared owner closes this row.
- `ROUTE_SPECIFIC`: the route needs a narrowly justified adapter.
- `NOT_APPLICABLE`: the contract cannot occur on this path.
- `UNPROVEN`: evidence is missing; do not assume coverage.

Trace actual imports, builders, registry declarations, wrappers, and tests. Do
not infer propagation from similar names or prompts.

### 3. Choose The Durable Repair Boundary

Patch the earliest shared owner that can enforce the contract without moving
semantic decisions into Python:

- semantic authority or typed plan for ownership and permission
- capability profile or tool policy for request-scoped admission
- provider tool for schema, identity, arithmetic, mutation, and read-back
- shared receipt/recovery path for side-effect truth
- validator for evidence identity and measurable constraints
- renderer for public status and concise output
- runtime estimator for model/tool/final-synthesis capacity

Prefer one schema-first or tool-general correction over agent-name checks,
phrase matching, expected-answer branches, or duplicated prompt instructions.
Preserve exact approval, no-send, no-post, mutation, and provider-read gates.

### 4. Add Cross-Path Regression Proof

Start with the smallest offline proof, then expand only when the propagation
matrix justifies it:

1. unit test the shared helper or contract;
2. test at least two materially different consumers when two exist;
3. cover direct named-agent and orchestrated/WorkItem paths when both reach the
   seam;
4. cover a no-op, ambiguous, missing-identity, or post-side-effect failure edge;
5. verify receipts and public status cannot contradict provider truth;
6. keep composite helpers and intentionally narrower routes bounded.

Use fixtures, fake models, mocked providers, and dry runs first. For any live
reproof, follow `$codex-general-api-cost-aware-agent-testing` and
`$kba-live-sdk-smoke-and-cost`, keep runs serial, inspect the Slack result and
trace before the next run, and stop after the first conclusive result.

### 5. Record What Generalized

Report the shared boundary changed, matrix rows covered, route-specific
exceptions, tests run, live proof boundary, and remaining `UNPROVEN` rows. Do
not say “fixed across agents” when only one direct test passed.

## Verification

Before closing:

1. Run focused tests for the diagnosed defect and every changed shared seam.
2. Run registry/architecture tests if tools, prompts, builders, schemas, or
   agent cards changed.
3. Run Ruff, `python -m py_compile` for changed Python modules, and
   `git diff --check`.
4. Confirm no prompt-specific company, person, record, message, date, or test
   phrase entered production code.
5. Confirm successful mutations are reconciled from provider receipts before a
   retry or failure card is emitted.
6. Confirm the final public answer distinguishes provider success, model
   synthesis, verification, latency, and any remaining proof boundary.
7. After changing this skill, run
   `tests/test_kba_propagate_agent_fixes_skill.py` and the repo skill inventory
   tests, then perform an immediate improvement pass for missing guards,
   commands, pitfalls, and reusable shortcuts.
