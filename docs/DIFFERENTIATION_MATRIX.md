# ANU-175 Differentiation Matrix

Executable source: `src/keystone_agents/differentiation_matrix.py`

Focused coverage: `tests/test_differentiation_matrix.py`

KBA should not compete with ChatGPT on generic conversation breadth or with
Codex on repo editing. It should be meaningfully different when Keystone work
requires persistent WorkItem state, Slack-native execution, source-backed
specialists, durable approvals, typed context packs, and operational
follow-through.

## Compact Comparison

| Differentiator | KBA claim | Offline proof | Later live proof |
| --- | --- | --- | --- |
| Persistent WorkItem state | Preserve intent, blockers, approved facts, artifacts, next actions, and continuation history across runs. | WorkItem, context-pack, storage, and graph-completion tests. | Source-thread Slack continue or revise probe preserving the same WorkItem state. |
| Slack-native execution | Let operators start, review, continue, approve, block, and revise work from Slack without route dumps. | Slack bridge-contract, action, and renderer fixture tests. | Controlled Slack API probe showing answer-first completed or blocked output. |
| Source-backed specialists | Use named specialists with retrieval policy, structured outputs, and visible sources. | Fixture research, opportunity, source-attribution, and registry tests. | Capped read-only live research probe with source URLs visible in the first answer. |
| Durable approvals and audit | Persist read, draft, internal-write, and external-side-effect boundaries as reviewable audit state. | Approval-gate, side-effect policy, and dry-run write-plan tests. | Draft-only or live-read probe that records blockers without sending or mutating externally. |
| Typed context packs and handoffs | Pass approved facts, source refs, blockers, artifacts, and thread metadata between Orchestrator and specialists. | Context-pack, workflow-runner, and multi-agent template tests. | Combined workflow probe proving downstream specialists use approved context. |
| Operational follow-through | Convert asks into durable next actions, artifacts, smoke tasks, automation candidates, and follow-up work. | Smoke-task matrix, workflow-template, and trace-summary tests. | Later scheduled or Slack-driven read-only workflow probe after offline proof is clean. |

## Validation Milestones

1. Strategy and comparison matrix: validate by reviewing ANU-175, updating the
   backlog/docs, and running static matrix tests. No live Slack/API probes are
   part of this milestone.
2. No-live execution proof: run focused pytest, fixture, local eval, or smoke
   tasks that prove at least one differentiator without side effects.
3. Later live Slack/API probe: only after the offline gate is clean and a scoped
   operator approval plus live-test budget or stop condition exists.

The live probe is evidence for delivery, not a substitute for offline tests.

## Selected Near-Term Commitments

ANU-175 now narrows the broader matrix to five measurable commitments:

1. Slack-native, answer-first execution with one final receipt.
2. Reduced context re-entry through persistent WorkItem and same-object state.
3. Source-visible specialist judgment rather than generic unsupported prose.
4. Durable exact-scope approvals and audit without duplicate approval friction.
5. Operational follow-through into a reviewed artifact or next safe action.

Generic reasoning, generic coding, and connector breadth are explicitly not KBA
differentiators. The representative proof workflows are research-to-internal-
Doc, selected Gmail-thread follow-up, current opportunity assessment, and a
weekly/project brief.

`DifferentiationObservation` is the comparison contract. KBA and the clean
Codex/ChatGPT-style baseline must receive the same natural ask, identified by
the same SHA-256 hash, and each observation must retain direct evidence refs.
The comparison records useful result, route correctness, visible sources,
follow-up continuity, context fields re-entered, manual provider IDs, approval
round trips, unintended writes, duplicate artifacts, developer intervention,
latency, and estimated cost. Missing or mismatched baseline evidence never
implies a KBA win.

An individual KBA observation is safe and useful only when it returns a useful
correctly routed result with zero unintended writes, zero duplicate artifacts,
and no developer intervention. A comparative claim is supported only when the
matched KBA observation is safe and useful, improves at least one committed
operator metric, and introduces no recorded capability or safety regression.

Run the focused operator-safe contract smoke with:

```bash
npm run test:differentiation:no-live -- \
  --json-output artifacts/test-pack/anu175-no-live-smoke.json
```

The receipt records zero OpenAI requests, connectors, provider writes, and
external side effects. It proves that matched observations can support a claim
and that missing or mismatched baseline evidence cannot. It does not fabricate
or replace the later real matched operator baseline observation.

## First Matched Live Comparison

The first real matched observation uses the exact compact current-opportunity
ask, the same two-page supplied packet, the same output schema, `gpt-5.4-mini`,
and no tools/search/provider actions for both systems. Both KBA and the generic
baseline passed every factual and safety check. The comparison is therefore
honestly `not_supported`: the baseline cost estimate was `$0.005883`, while the
optimized KBA estimate was `$0.01107525`.

This does not show that KBA is worse as an operating system. It shows that a
simple one-turn supplied-packet answer is not itself a KBA differentiator. KBA
claims should be tested where its product contract is relevant: persistent
WorkItem state, Slack-native continuation, approved Keystone context, durable
approval/audit, typed cross-agent handoffs, and operational follow-through.

The comparison still produced a concrete product improvement: KBA's compact
Opportunity prompt was reduced from 119,733 to 31,503 characters, cutting live
input from 26,167 to 8,029 tokens and the maintained estimate from `$0.02411625`
to `$0.01107525` with every factual and safety check preserved. The generic
baseline remains cheaper, so the result stays `not_supported` rather than being
reframed as a win.

The comparison also repaired the measurement contract: cost and latency fields
now participate in improvement/regression decisions instead of being recorded
but ignored.
