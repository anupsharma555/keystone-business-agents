# Orchestrator and Chief of Staff R/W/M Acceptance

This is the executable acceptance map for ANU-193 and ANU-194. It separates
internal control-plane or manager state from provider mutation.

| Owner | Capability | Required proof | External boundary |
| --- | --- | --- | --- |
| Orchestrator | Read | Preserve the raw request shape, inspect available context, select an owning route, and record a decision trace. | Reading never grants provider mutation. |
| Orchestrator | Write | Produce route advice, preflight metadata, draft planning fields, review notes, or WorkItem events. | Orchestrator does not execute provider writes. |
| Orchestrator | Modify | Replan after an explicit correction, reject the former route, and retain no-send/no-write gates. | Ambiguous corrections stop for context. |
| Chief of Staff | Read | Use operating context, WorkItems, approved memory, and bounded context-agent evidence. | Read access does not authorize posting or mutation. |
| Chief of Staff | Write | Produce an internal plan, artifact/write plan, approval request, or structured durable/context handoff. | Provider execution remains with the owning typed tool or specialist. |
| Chief of Staff | Modify | Revise an internal plan or remove a proposed write after user correction or new evidence. | Nested specialists remain advisory and cannot inherit write authority. |

An authenticated direct operator command may approve the exact supported
provider operation and target it names. That operation still requires the
owning typed tool, provider live flag, unique target identity, scoped approval
reference, read-back receipt, and any cleanup contract. Orchestrator and Chief
may select or prepare that path; neither may silently broaden it.

The focused executable gate is:

```bash
.venv/bin/python -m pytest tests/test_manager_rwm_acceptance.py \
  tests/test_prompt_contracts.py tests/test_orchestrator.py \
  tests/test_chief_of_staff.py -q
```

This gate covers read behavior, internal writes, plan correction, ambiguous
modification blocking, structured handoffs, nested advisory boundaries, and
schema-level rejection of direct send or unscoped Slack posting. Live provider
lifecycles remain separately scoped operational validation.

## Low-friction manager review contract

Manager review is a quality and safety layer, not a mandatory wrapper around
every natural-language request.

- Route a complete, explicitly named, read-only specialist or context-agent ask
  directly to that owner. Polite wording does not make the ask broad or require
  a Chief-of-Staff advisory step.
- Use Chief of Staff when the goal is broad, ambiguous across capabilities,
  requires multiple dependent agents, or needs a manager to combine specialist
  results into one operating recommendation.
- Treat non-critical review gaps as advisory. A completed artifact remains
  usable when review only recommends optional polish, greater depth, or a later
  follow-up.
- Attempt at most one bounded repair when an otherwise useful result has a
  repairable quality gap. Do not loop between manager and specialist.
- Block only for authoritative conditions: unsafe or unauthorized effects,
  materially wrong/off-target output, unsupported claims, missing required
  evidence, ambiguous provider identity, or an unmet deterministic gate.
- Do not request a second approval for the same exact supported operation and
  target already named by an authenticated operator. Provider live flags,
  unique identity, read-back, and cleanup gates still apply.

The acceptance test is operator friction: a complete low-risk ask should reach
its owning agent and return one useful result without extra route selection,
approval ceremony, repeated review messages, or duplicate final output.

## ANU-222 advanced scenario gate

`src/keystone_agents/advanced_manager_scenarios.py` is the canonical registry
for the 12 advanced no-live manager scenarios. It covers multi-turn correction,
selected-object continuity, conflicting instructions, partial specialist
failure, evidence disagreement, approved-write transition, reversal, advanced
advisory synthesis, and completion receipts.

Run the complete gate with:

```bash
npm run test:advanced-manager:no-live -- \
  --json-output artifacts/test-pack/advanced-manager-scorecard.json
```

The runner executes the registry's focused pytest evidence one scenario at a
time and stops on the first failure. Its scorecard records pass/fail, failure
category, next fix, proof node IDs, zero OpenAI requests, no live connectors,
and no external side effects. This gate must pass before proposing the bounded
live ANU-222 batch.

Repeat the bounded live correction proof with:

```bash
npm run test:advanced-manager:live-correction -- \
  --max-openai-requests 2 \
  --max-total-cost-usd 0.10 \
  --output artifacts/test-pack/advanced-manager-live-correction.json
```

It uses one local SDK session across exactly two model requests, attaches no
tools, performs no provider reads or writes, and removes the temporary session
afterward. Acceptance requires the first compound request to remain Chief-owned
and the correction to supersede stale opportunity and outreach direction while
retaining explicit evidence that prior direction was considered.

## ANU-223 delegation readiness gate

`src/keystone_agents/manager_delegation_readiness.py` keeps manager-entry proof,
provider-owner lifecycle proof, and joined manager-to-provider proof as
separate fields for Calendar, Gmail, Airtable, Google Workspace, and Zotero.
Run it with:

```bash
npm run test:manager-delegation:no-live -- \
  --json-output artifacts/test-pack/manager-delegation-readiness.json
```

The gate must not mark ANU-223 complete merely because both layers pass in
isolation. Its scorecard names which joins are proven and the next exact live
proof for each missing join. Already-proven Calendar and Gmail paths are not
rerun; the remaining serial candidates are Airtable, Workspace, and Zotero.
