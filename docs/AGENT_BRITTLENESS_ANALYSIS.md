# KBA Agent Brittleness Analysis

Status: evidence baseline and test strategy, updated 2026-08-04.

## Conclusion

KBA brittleness is primarily an execution-architecture problem, not a random
model-quality problem. In the reviewed live sample, the composed Slack path had
no full passes across nine recent root requests, while several hundred focused
offline decision, tool, routing, repair, and trace checks passed. The components
work under controlled contracts; failures concentrate where the entrypoint,
Orchestrator, specialist, child process, provider evidence, and public renderer
are combined.

`gpt-5.4-mini` can amplify instruction-following and structured-output failures,
but it cannot explain missing tool attachment, fixture evidence entering a live
path, specialist budget starvation, lost child telemetry, or a renderer
downgrading a provider-verified operation.

## Ranked causal ledger

| Rank | Cause | Evidence status | Why it creates intermittent behavior |
|---|---|---|---|
| 1 | Divergent production execution paths and pre-agent authority | Proven, very high confidence | Similar asks can reach a direct specialist, Chief, legacy provider-prefetch path, fixture-backed path, or WorkItem path. Each path has different tools, evidence, validators, and recovery behavior, so a contract proven in one path does not guarantee Slack behavior. |
| 2 | Candidate-context drift between decision and repair | Proven, very high confidence | Opportunity runs selected identities that disappeared from the repair universe or could not fit in the replay packet. The validator then rejected a semantically reasonable choice because the execution boundary changed underneath it. |
| 3 | Request-budget and tool-loop mismatch | Proven, very high confidence | Recent Opportunity runs exhausted seven requests, used all four tool turns without finalizing, or timed out after repeated retrieval. Recovery capacity and actual stage caps did not match the advertised execution envelope. |
| 4 | Shared mutable tool state | Proven, very high confidence | SDK agent clones shared FunctionTool objects. Overlapping correction and repair disables could leave a read tool disabled for a later Airtable, Workspace, or Zotero run, making failures depend on test order or concurrency. |
| 5 | Tool admission and live/fixture binding errors | Proven, high confidence | Tool tiers could depend on incidental words such as “detailed”; recent Gmail evidence also included missing required calls and fixture identity in a nominal live path. A model cannot call an unattached tool or convert fixture output into provider evidence. |
| 6 | Orchestrator decision, latency, and request-budget coupling | Proven, high confidence | Every reviewed live run spent one additional model request and about 11–15 seconds in routing preflight before the specialist. Invalid route shapes or oversized route evidence can still consume time and shared budget. |
| 7 | Semantic evidence acquired or selected before the specialist | Proven, high confidence, path-specific | Some legacy Gmail and WorkItem branches still prefetch or sort evidence before the main specialist. Deterministic normalization is useful; deterministic semantic selection or silent evidence substitution prevents the specialist from owning the decision. |
| 8 | Result-schema, promotion, and renderer fragmentation | Proven, high confidence | A provider-verified Calendar write was later labeled partial when a separate Chief narrative failed schema validation. Provider-operation truth and narrative-synthesis quality were conflated. |
| 9 | Oversized prompts, schemas, and toolboxes | Strong inference, medium-high confidence | Large prompts and broad tool surfaces increase latency, instruction dilution, schema error risk, and repair cost. The effect is strongest for Chief and broad Workspace, Zotero, Outreach, and research profiles. |
| 10 | Inconsistent recovery and failed-attempt accounting | Proven, medium-high confidence | Boundaries used different repair counts, and failed child records could omit Slack provenance or reduce provider evidence to stderr. The parent then could not reconstruct exact attempts or costs. |
| 11 | Model capability (`gpt-5.4-mini`) | Plausible contributor, medium confidence | The model has produced invalid routing shapes and omitted required tools, but it has also made correct tool and selection decisions. It is an error amplifier after architectural defects, not the leading cause. |
| 12 | Runtime revision or configuration drift | Unresolved, medium-low confidence | Process health alone does not prove the loaded source and configuration match the tested checkout. A source/config fingerprint is required before attributing a new live failure to current code. |
| 13 | Provider instability | Low for Gmail/Calendar; material for research | The latest Opportunity sequence ended in a 150-second child/provider timeout, but most other failures were validation, admission, repair, or budget defects. Provider reliability is real but not the leading cross-agent cause. |
| 14 | Observability gaps | Proven diagnostic multiplier | Missing model-visible inputs, tool calls, receipts, run correlation, and failed-attempt counts do not always cause the failure, but they make path errors look like model errors and slow reliable correction. |

## Correct deterministic/model boundary

Deterministic code should own normalization, schema checks, candidate-ID
binding, permissions, approvals, exact write scope, deduplication, lifecycle
state, and provider read-back. It should not silently select the semantically
best candidate, replace the agent's choice, hide substantive evidence until
after the decision, or turn invalid output into an apparently grounded result.

The agent should own which admitted read/query tool to call, which candidates
matter, the selected candidate, exclusions, reply relevance, delegation, and
specialist synthesis. A validator may reject the choice and return structured
feedback for one repair; it must not choose a replacement.

## Current mitigations

- The standalone planner remains outside the normal production path; the raw
  request remains authoritative.
- Orchestrator is still the first model control plane, but routing-only
  preflight uses a compact policy profile. The instruction surface dropped from
  about 155,000 to about 38,700 characters in the local measurement.
- Orchestrator now has one decision repair. A non-safety route-shape failure is
  recorded but advisory for an explicit, recognized, single-owner specialist;
  ambiguous and cross-agent requests still fail closed.
- A fresh Gmail selection reserves a minimum of three model calls: query,
  candidate-context read, and final decision. An exact verified continuation
  reserves two: exact read and decision.
- Gmail's query is forced on the first provider-selection turn. Selection and
  repair tests assert the per-turn tool choice.
- Structured Gmail child failures now retain request counts, tool calls,
  receipts, decision evidence, repair state, request budget, cost, and latency
  through the parent CLI.
- Provider-operation completion and synthesis/review status have separate
  public-result handling so narrative failure cannot negate a verified write.
- Every SDK run now receives isolated tool-state copies before instrumentation
  or repair disables, so one run cannot hide tools from later agents.
- Opportunity repair preserves a frozen, bounded candidate universe and uses a
  tool-free semantic repair; merged provider candidates are capped before they
  enter the model context.
- Exact-page Research and bounded one-result Opportunity requests now admit the
  required tools from task structure rather than requiring “detailed” wording.
- Failed isolated-child records now retain validated Slack provenance so the
  diagnosis collector can correlate terminal failures without a manually copied
  agent-run ID.

These mitigations are offline-proven but require bounded live reproof against a
freshly identified runtime/configuration fingerprint.

## Discriminating test plan

1. Replay the same natural paraphrases through the Slack-equivalent wrapper and
   direct specialist wrapper using identical fake providers. Any behavioral
   difference proves entry-path divergence.
2. Inject an invalid Orchestrator decision for an explicit safe Gmail request.
   Confirm the failure is traced, the specialist retains its reserved budget,
   and deterministic safety and permission gates still apply.
3. Cross live/fixture mode, explicit/vague ownership, and fresh/continuation
   requests. Assert attached tool schemas, first required tool, provider receipt
   type, and rejection of fixture evidence in live mode.
4. Use a four-candidate Gmail fixture with current, canceled, obsolete, and
   reminder threads in varied order. Require one query, no more than four
   context reads, agent-owned selection, and explicit exclusions.
5. Inject a verified mutation followed by invalid narrative output. The provider
   operation must stay verified while synthesis is separately marked degraded.
6. After path parity passes, compare frozen scenarios on `gpt-5.4-mini` and one
   stronger approved model. Measure valid structured decisions, required-tool
   use, repair rate, selection accuracy, latency, and estimated cost.
7. Before each live proof, record the runtime/config fingerprint and correlate
   entry, Orchestrator, specialist, tool, receipt, validator/repair, renderer,
   usage, latency, and terminal status in one trace.

## Model-upgrade decision rule

Do not upgrade the default model merely because a composed run failed. First
require tool/path parity and complete traces. A stronger model is justified if,
under the same frozen inputs, tools, candidates, schemas, limits, and provider
fixtures, it materially improves decision validity or selection accuracy enough
to offset its latency and cost. This turns model choice into an experiment
rather than a substitute for architecture repair.
