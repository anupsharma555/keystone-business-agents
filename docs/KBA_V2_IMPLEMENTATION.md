# KBA V2 implementation and proof boundaries

Current implementation review: 2026-09-14.

This implementation adds native SQLite graph checkpoints, a shared local execution
journal, stronger evidence/authority boundaries, and isolated architecture
experiments. Existing direct specialist and non-graph WorkItem execution remain
available. A graph node is an execution boundary, not necessarily a separate
agent or another model call.

The checks recorded below establish bounded local behavior. They do not establish
full live Slack/provider acceptance or prove that more nodes, agents, or review
calls improve answer quality.

## Current checkpoint and proof boundary

KBA V2 remains an isolated local implementation; GitHub publication and production
promotion are separate operator decisions. Exact newsletter-to-Outreach and
correspondence workflows have bounded successful evidence. The latest two-turn
Slack root also delivered the requested answer and source link, but its same-thread
follow-up stopped after planning and has not been live-proved after the typed
context-only correction.

Saved receipts record 691 broad tests and a later 77-test focused executor gate
passing for that correction. Those sets overlap and were not rerun on September 14.
They establish an offline contract, not live provider reuse, Slack continuation,
transport correctness, remote CI, release readiness, or broad agent robustness.

The next reviewed sequence is: verify the integration/source/dependency baseline;
verify the existing architecture generators, topology and visible assets; perform
one separately budgeted context-only two-turn Slack reproof; then test a materially
different provider/request and the signal-to-opportunity path. Model/provider tests,
Slack writes and Linear updates require their own reviewed scope.

## Historical V12 Slack checkpoint and sequence (superseded)

This section preserves the earlier checkpoint and recommendations. It is not the
current execution order.

The latest complete V12 gates passed **6,609 tests / 13 skips** with graph dependencies and **6,576 / 46** without them. Two monitored model-backed Slack tests nevertheless failed before provider work: a planner selected-set contradiction, then an accepted plan rejected by the post-planning request estimate: one observed planner response + eight possible Chief turns + one conditional formatting repair = maximum ten against a cap of eight. Only one request was consumed. A separate URL-formatting mismatch was blocked by the test harness with zero model calls. Neither live test reached a WorkItem or native graph stage.

A six-line optional test-profile change enables one existing guided structured correction, keeping the eight-request ceiling and zero HTTP retries. **91 focused tests** and Ruff passed; no prompts, decision schemas or validators were weakened. This later focused count overlaps the earlier full suites and does not constitute another complete-suite result.

The recorded recommendation was to prioritize request-estimate reconciliation,
executable Gmail/Workspace capability ownership, source-acquisition versus
internal-draft readiness, per-deliverable output constraints and Slack transport
fixtures before repeating the email-only workflow. Later evidence superseded that
ordering. The original worker configuration, main source, environment files and
operator database were preserved.

## What changed

| Surface | Implemented behavior | Important boundary |
|---|---|---|
| Graph persistence | Saved compiled graphs use `SqliteSaver`, stable execution identity, and synchronous checkpoints. A failed graph can resume its saved pending node. | WorkItems remain canonical business state; graph checkpoints record execution progress. |
| Direct execution | Saved non-graph WorkItem execution uses the same operation and model-budget journal. | Direct replay is not native graph-node resume. |
| Duplicate execution | Origin-event identity distinguishes a repeated delivery from a separate identical request. OS-owned locks and a state re-read after locking prevent a stale caller from restarting completed work. | Serialization is local and per execution identity; it is not distributed coordination. |
| Shared WorkItem ownership | A shared lock protects a known WorkItem across distinct graph/direct executions, native resume, dependency-free graph execution, and signal transitions. Nested stages reuse their owner; expired copied contexts cannot reuse a closed lease. | Busy competitors are rejected before advancement and can retry after release. The identity must be supplied or already recorded on the execution. |
| Mutation recovery | An intent is recorded before a wrapped mutation. Observed identities, verification receipts, and original successful tool results support safe reuse. Equivalent JSON serialization maps to the same operation. | Unknown or unverified outcomes block another mutation; an exact observed object must be reconciled before reuse. |
| Approval and scope | Native approval resume checks authoritative WorkItem approval state. Archived work, changed scope/evidence/ownership, and incompatible runtime revisions cannot silently resume. | `--approved` does not grant approval. Existing provider gates remain authoritative. |
| Local transactions | Related WorkItem/artifact/event writes can use one transaction; WorkItem updates support an expected-version check. Signal transitions use the shared guarded persistence boundary. | The existence of a compare-and-swap API does not prove every legacy caller uses it. |
| Final response replay | Duplicate CLI deliveries reuse the canonical reviewed JSON or final plain-text response; intermediate specialist rows and progress output are excluded. | This preserves KBA output; the parent transport still owns visible delivery acknowledgment. |
| Recovery audit and response | An exact bidirectional command/execution link marks an abandoned command interrupted and records native recovery separately. Recovery can render and cache the accepted result for later duplicate delivery without a model call. | Historical commands without a link are not guessed from wording. Newly rendered recovery output does not prove the original response was delivered. |
| Answer preservation | The exact-sentence formatter no longer substitutes historical source claims for the model's answer. Accepted text and pending actions survive formatting unchanged. | Formatting validation and any budgeted model repair remain upstream of rendering. |
| Accounting | Separate preflight stages are included once, copied usage is deduplicated, and conflicts or missing coverage cannot be called a confirmed total. | Provider-reported usage and conservative request-ledger consumption remain separate. |
| Planning observations | Returned planning usage is retained before later preparation can fail. The returned plan and the subsequent dispatch request have distinct audit references; handled preparation errors reach the original command record. | Audit snapshots do not bypass execution identity, approval or runtime-migration checks. |
| Validation diagnostics | Structured failures retain content-free error codes, schema paths and validator locations. The existing retry receives bounded feedback about the observed rule. | Failed model bodies and arbitrary error messages remain excluded; validation and retry limits are unchanged. |
| Runtime identity | Fingerprints include source, runtime skills, installed package versions, Python patch version, configuration, and dependency constraints. | A resumed execution must satisfy its compatibility check; silently loading changed code is not a migration. |
| Inspection and forks | Checkpoint inspection reads existing tables through a read-only saver. A diagnostic fork exports source-linked, capability-restricted input. | Fork export does not execute historical nodes or replay live effects. |
| Delivery preparation | Pending delivery records and exact message acknowledgments can be recorded idempotently. | A delivery record alone is not proof of Slack transport or successful display. |

The execution database name appends `.execution.sqlite3` to the complete business
database filename. For example, `business.db` and `business.sqlite3` receive
different execution databases. The business database is never inferred from a
similarly named checkpoint file.

Direct specialist subprocesses inherit `KEYSTONE_DURABLE_EXECUTION_REFERENCE`, an
internal, masked reference generated by the parent. The child reopens only that
existing journal and running execution, including its consumed model budget and
verified operation receipts. Deterministic provider wrappers resolve the same
reference before SDK execution. Missing, completed, or incompatible referenced
executions fail closed; reconstruction never creates a database or falls back to
the operator database. Child request-budget and deadline settings can differ,
while loaded code, dependencies, Python, and package versions must match. The
reference carries no credentials, provider clients, checkpoints, or approvals.

New CLI executions record an exact, bidirectional link to their initial audit row
before graph entry. Native resume marks an abandoned original row `interrupted`
and records the recovery outcome separately, including failed recovery. It does
not invent an original exit code or delivery acknowledgment. Already finalized
rows and legacy executions without a link are not heuristically rewritten.
Accepted preflight usage remains in the saved graph request for accounting.

A completed native invocation can also render and cache a recovered JSON/text
response from its saved result through the shared deterministic presentation
boundary, without a model request or eval-side effect. Duplicate delivery reuses
that response without repeating business work. It is explicitly newly rendered
recovery output; original CLI delivery remains unverified. Graph invocation
completion does not override an unfinished or blocked WorkItem status.

SDK conversation sessions, WorkItem state, graph checkpoints, operation receipts,
and delivery state serve different purposes. In particular, a conversation
session is not proof that a provider mutation completed, and a completed graph
is not proof that the requested answer was delivered correctly.

Recognized source bundles supply bounded target, source and fact context before
Orchestrator routing, including optional manual planning. Their permissions and
approval fields are excluded, and excerpt paths do not authorize attachment reads.
The main routing decision records a completed choice even when selecting
`clarification`; its question remains in `clarification_request`. Generic provider
decisions retain their separate missing-context flag. Python validates these
contracts without selecting or replacing the model's route.

A live diagnostic identified a concrete selection inconsistency: the planner's
explicit chosen IDs disagreed with its assessments marked selected. Shared schema
descriptions and routing examples now explain that equality, including a manager
whose children appear in the workflow. The next live planning response passed on
its first attempt. This is a bounded reproof, not a reliability-rate claim.

That successful response exposed a later persistence problem: a legitimate
`cost.billable_tokens` map was classified as a secret. Numeric-only billing maps
now survive context freeze/restore, while unknown keys or malformed values still
cause rejection. The post-decision workflow summary also uses its existing typed
model instead of an unchecked dictionary assignment that emitted serialization
warnings. Full source material remains in the model input and request snapshot.

The subsequent live plan persisted successfully and resumed native graph state
in a fresh process. That resume exposed a separate semantic defect: Research's
supplied-evidence branch produced a fixture result despite live SDK execution
being requested. Related bypasses were found in Opportunity, Gmail and Chief.
Their corrections are currently being tested; the interrupted execution remains
partial evidence and is not migrated to pretend it used the corrected agents.
See the [pilot record](KBA_V2_PILOT_RESULTS.md#native-checkpoint-restart-exposed-a-semantic-bypass).

## Dependency and CI baseline

The reviewed package baseline is:

| Package | Tested version |
|---|---:|
| OpenAI Agents SDK | 0.22.2 |
| OpenAI Python SDK | 3.12.0 |
| LangGraph | 1.2.11 |
| LangGraph checkpoint core | 4.1.1 |
| SQLite checkpointer | 3.1.1 |

Model defaults were not changed. The candidate was tested in isolated environments
before the shared interpreter was upgraded. Existing dependency versions were
retained wherever the new requirements permitted them. Stricter SDK validation
exposed reused tool-call IDs in fake-model fixtures; those fixtures now use unique
IDs rather than disabling validation.

The [dependency constraints](../constraints/README.md) describe the 126 version
pins and pinned build-tool setup. CI retains the Python 3.11 minimal installation
and adds a Python 3.13 compiled-graph installation with SQLite support. Both jobs
check dependencies and run the test suite. The minimal installation asserts that
LangGraph is absent; the graph job requires successful compiled-graph construction.
These are configured checks, not evidence that remote CI has already run.

## Inspecting and resuming executions

Use the dedicated local command with the exact business database. For a disposable
fixture database, the following starts no model or live-provider operation:

```bash
.venv/bin/python scripts/kba_execution.py --database-url sqlite:////tmp/kba-v2-demo.db start --request "research Example Analytics" --execution-id synthetic-demo
.venv/bin/python scripts/kba_execution.py --database-url sqlite:////tmp/kba-v2-demo.db list
.venv/bin/python scripts/kba_execution.py --database-url sqlite:////tmp/kba-v2-demo.db inspect synthetic-demo
```

Use `journal EXECUTION_ID` to inspect model consumption and provider-operation
evidence for either direct or graph execution, including without LangGraph installed.

Use `resume EXECUTION_ID` for a failed native graph. A pending human interrupt also
requires `--approved` after the exact approval has been persisted in WorkItem
state. Saved live capabilities must be explicitly reaffirmed; the command does
not grant missing provider permissions. A completed execution returns its saved
result without repeating its work.

`inspect EXECUTION_ID --include-state` includes the complete historical state
for local debugging; this may contain private evidence. The default view exposes
changed keys, pending nodes and fingerprints without dumping that evidence.

`fork EXECUTION_ID CHECKPOINT_ID` exports an isolated diagnostic request. The
export carries source execution/checkpoint references and a state fingerprint;
it does not transfer approvals, provider identities, active sessions, or live
capabilities. Inspection of a missing execution does not create its database.

## Recorded verification

### Durability boundary checks

`tests/test_durable_execution.py`, `tests/test_v2_durability_boundaries.py`, and
`tests/test_signal_lifecycle_tools.py` passed **50 cases** together in the isolated
security candidate after the boundary corrections. The corresponding minimal
installation passed **41 cases**, with **9 native-graph cases skipped** because
LangGraph was absent. The adversarial cases cover:

- a separate process exiting abruptly before finalization, followed by a fresh
  process resuming the real SQLite checkpoint without repeating the specialist;
- two concurrent resumes, with one owner blocked and the pending node executed once;
- stale callers arriving at the lock after another caller completed, for graph
  and non-graph execution;
- distinct graph/direct executions competing for one existing WorkItem, safe retry
  after ownership release, and signal-stage reentrancy without self-deadlock;
- copied execution contexts refusing to reuse an expired WorkItem ownership lease;
- equivalent SDK JSON arguments, including JSON-valued Airtable field payloads,
  reusing one verified mutation and preserving the original return envelope;
- literal document text remaining distinct even when it looks like JSON;
- a simulated provider create followed by read-back failure, retaining the observed
  object and blocking another create until exact-object reconciliation;
- a native pending approval that cannot be released merely by supplying an
  `approved` flag, followed by approved resume without recreating the draft;
- archived WorkItems, changed graph/runtime dependencies, and failed direct replay
  after runtime drift;
- distinct business database filenames, read-only checkpoint inspection, transaction
  rollback, persisted model ceilings, and delivery identity conflict handling.

These tests use synthetic data and fake provider boundaries. The separate-process
test fixes the runtime fingerprint in its test double so concurrent repository
edits do not change the recovery question; dedicated tests separately verify
runtime-mismatch rejection.

### Other bounded checks

During the dependency assessment, the clean upgraded environment passed **552
focused compatibility tests**. A separately installed environment without
LangGraph passed **297 focused tests**. After the final accounting changes, **50
accounting/provenance/adjacent tests** passed. Both isolated environments passed
`pip check`, and a package wheel built from a scoped temporary source snapshot.

The subsequent architecture review passed **37 tests**. The current experiment
module calls the existing shared typed SDK runner; it does not need an exception
to the raw-SDK execution allowlist. Focused Ruff and diff checks passed for the
reviewed changes.

These counts describe different checks at different integration points. They must
not be added together or substituted for the final combined repository gate.

The latest completed combined gate, including preflight persistence and accounting
repairs, passed **6,388 tests / 1 skip** with LangGraph and **6,368 tests / 21 skips**
without it. The new supplied-evidence SDK corrections postdate that gate and need
their own verification before further live execution.

## What remains a separate proof

- Native persistence is SQLite-based. Postgres deployment, distributed workers,
  cross-host locking, and datastore migration are not implemented by this tranche.
- Native graph checkpoints recover graph steps. They do not automatically recover
  every internal SDK turn or every sub-operation of a composite provider helper.
- Native approval interrupts are enabled by default for saved graph execution.
  Callers can explicitly use `enable_interrupts=False` for legacy WorkItem
  continuation behavior; those terminal checkpoints are not suspended-node resumes.
- Early provider-response observation covers the reviewed Airtable writes and
  attachment links, Gmail draft operations, Calendar create/update/delete, Docs
  writes, Sheets creation, and single-object Zotero note/item/collection changes.
  Multi-folder and Slides composite operations still need separately identified
  child operations or a receipt set. Missing outcome evidence remains blocked/unknown.
- Per-execution locking and selected atomic transitions are proven locally.
  A shared WorkItem lock now covers known-identity graph, direct, native resume,
  dependency-free fallback, and signal advancement. Uncoordinated administrative
  edits and late implicit WorkItem discovery remain distinct integration boundaries.
- A completed provider receipt is not a new provider read. Reused historical
  receipts should not be described as fresh verification of externally changed data.
- Operation reuse is keyed to execution, tool, and normalized arguments. A
  per-execution barrier prevents new mutations while any effect is unresolved,
  and persisted scope digests reject changed provider defaults. After a verified
  effect, reuse does not infer that different arguments mean the same plan step.
  Existing approval and object-identity checks remain necessary; this report does
  not claim blanket exactly-once behavior when a replaying model changes its write
  plan.
- Delivery preparation/acknowledgment is a local contract. Transport retry, one
  final Slack message, visible content, and provider UI agreement need separate
  correlated acceptance.
- Local checks are not a substitute for Linux/Python 3.11 remote CI. Live-model
  probe evidence, when recorded below, does not establish provider-write, Slack,
  sandbox, or deployed-worker acceptance.

## Architecture experiments

The [V2 experiment harness](KBA_V2_EXPERIMENTS.md) and
[`evals/static/v2_experiments.json`](../evals/static/v2_experiments.json) define
seven concrete comparisons: same-agent phases, independent criticism,
complementary investigators, targeted repair, compact context, checkpoint inspection,
and text-only versus selected-page-image document evidence.

The fixture controls establish mechanics and guardrails. Model-quality lift
remains **unmeasured** until a separately budgeted run receives blinded assessment
against the original task and evidence. More agents, agreement between agents,
additional artifacts, and longer graph paths are not success metrics by themselves.

Use the original production path as one comparison baseline when deciding whether
an experiment belongs in production. The harness's isolated tool-free author is
an experimental control, not a complete production KBA baseline. Promotion requires
held-out task fidelity, safety parity, useful operator outcomes, and reconciled
cost/latency evidence. Existing non-graph modes remain available throughout.

## New managed-backend option

The [Agents API assessment](KBA_AGENTS_API_ASSESSMENT.md) reviews the newly
announced managed Codex harness as an optional future research/document backend.
It preserves KBA business authority and both current execution modes; no managed
API session or sandbox has been created.

## V2 direction and current proof review

The original V2 direction remains: durable LangGraph plus supported direct/non-graph
execution, with richer reasoning and collaboration evaluated rather than assumed
beneficial. Current execution follows the September 14 sequence above: baseline
identity, architecture verification, one bounded continuation reproof, then a
different provider/request and the signal workflow.

| Track | Current evidence | Next acceptance boundary |
|---|---|---|
| A — Baseline and measurement | Dependency upgrade, local constraints/audit, accounting, safe diagnostics and isolated worktree are established. | Freeze the latest corrections and run combined graph/minimal verification. Remote CI and its complete release/coverage gate remain unverified. |
| B — Durable execution | Local checkpoint/operation/budget mechanics are tested; one live accepted plan survived process death and native resume. | Actual specialist output must survive recovery with correct evidence and no repeated completed work. Do not count the saved fixture bypass as success. |
| C — Architecture and quality experiments | Seven experiment families exist; small model pilots found both useful behavior and errors. | Prioritize full-path failures now, then return to matched/held-out comparisons before promoting critics, investigators, extra phases or models. |
| D — Business acceptance | Exact newsletter-to-Outreach and correspondence cases have bounded successful evidence; the latest two-turn root passed, but its context-only follow-up remains unproved after the offline correction. | Reprove the context-only follow-up once under a reviewed budget, then test a different provider/request and the signal-to-opportunity lifecycle with duplicate/restart evidence. |

All further substantial changes and tests use the isolated V2 worktree. No GitHub
merge or push is planned without an explicit later operator decision. Historical
main-checkout work is preserved rather than reset indiscriminately.

Current review explicitly tracks three newly demonstrated handoff risks: semantic
SDK paths must not return fixture results, a selected company must not be replaced
by the newsletter publisher, and accepted Research findings and verified email
links must reach the next deciding model. Subsequent validation may reject an
invalid decision or request bounded repair; it must not silently substitute another
selection. Their focused tests do not replace complete Slack acceptance.

Linear remains the ownership tracker. The local `LINEAR_BACKLOG.MD` records the
dated milestone mapping and distinguishes refreshed issue records from historical
comments. ANU-321 and ANU-310 received verified milestone updates; neither is closed.

## Historical development options after the initial implementation

The numbered list below is preserved as design history. The current execution order
is the September 14 checkpoint above.

1. **Prioritize a monitored natural-language Slack workflow.** After verifying
   the supplied-evidence fixes, use a selected newsletter email to request company
   research, a verified manager/director contact and an outreach template in the
   same Slack thread. Verify the loaded worker, original request, evidence consumed
   by each agent, read receipts, complete deliverables, citations, rendering and
   total cost. A truthful missing contact with a placeholder is valid; an invented
   contact or CEO substitution is not. Inspect the first result fully before another
   run. Keep the selected source and exact draft in private local test artifacts.
   Then prove the separate RSS/preprint → Research → Opportunity path with stable
   source revision, restart and duplicate-delivery evidence. Neither isolated
   model probes nor fixture success replaces provider/transport acceptance.
2. **Complete multi-resource recovery.** Give each created folder, slide resource,
   or other compound effect its own operation identity and receipt. Preserve the
   current unresolved-effect barrier until reconciliation. Distinct planned
   operations that intentionally have identical arguments also need explicit
   operation identities; argument hashes alone cannot express that distinction.
3. **Connect delivery acknowledgment to the parent transport.** Reuse the stored
   canonical result without rerunning specialists. Correlate one delivery record
   with the actual Slack message ID and verify final displayed content. Keep
   transport recovery separate from repeating business operations.
4. **Evaluate quality changes individually.** Test a source-bound critic on
   incorrect and omitted deliverables, compare it with the author reviewing its
   own answer under equal total resources, and measure false objections. Test
   complementary investigators on genuinely separable evidence questions.
   Keep the current baseline and report raw paired outcomes before adopting a
   permanent role or hierarchical subgraph.
5. **Extend evidence and context experiments.** Broaden page-image tests beyond
   the one controlled synthetic table; evaluate long-document retrieval,
   corrections outside recent history, and targeted context expansion. Promote
   compact memory only if exact identities, later instructions, and source
   qualifications survive. Do not choose an embedding database merely to add RAG.
6. **Turn diagnostic exports into controlled counterfactual runs.** Current fork
   export preserves the original execution and strips capabilities. Actual replay
   of a historical decision with one changed variable needs a separate isolated
   state/artifact store and explicit saved-evidence inputs; it is not enabled by
   relabeling the source WorkItem or replaying live provider tools.
7. **Evaluate managed execution as an optional child backend.** Follow the Agents
   API addendum with no environment and read-only brokered functions first.
   Preserve KBA's local business rules and both graph/non-graph modes. Add
   PostgreSQL only when a concrete shared-worker or multi-host requirement
   justifies its migration and concurrency work.

A disabled recurring canary follows the complete operational acceptance. New
agents, automatic self-modification, broader provider access, and an enabled
schedule are not automatic consequences of passing these offline tests.

## Model comparison added by the operator

Add a controlled **GPT-5.6 Terra versus GPT-5.4-mini** experiment. The operator
confirmed GPT-5.4-mini as the baseline; no GPT-5.1-mini substitution or new default
is intended. Terra is a candidate for better judgment at a practical cost, not a
presumed winner. [Terra model documentation](https://developers.openai.com/api/docs/models/gpt-5.6-terra)

Use the same source version, request, tool scope, topology, output schema and
renderer. Record supported reasoning settings and any unavoidable differences.
Terra compatibility is now implemented centrally and passed **268 related offline
checks**. Actual SDK requests captured by a mocked HTTP transport preserve the
explicit model override, omit the incompatible legacy cache-retention field, and
retain the existing private-context storage/caching restrictions. GPT-5.4-mini
settings and defaults remain unchanged.

Pricing now retains cache-write counts and individual request boundaries. Standard
short-context rates are $2/$0.20/$2.50/$12 per million uncached input, cache-read,
cache-write and output tokens; a request above 272,000 input tokens uses the
documented long-context rates for that whole request. Missing counts remain
unknown rather than becoming zero. These are model-token estimates, excluding
account, regional, service-tier and hosted-tool differences.
[Pricing](https://developers.openai.com/api/docs/pricing),
[prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).
Start with chronology-sensitive Gmail selection, opportunity eligibility,
unsupported-claim detection, omitted deliverables, document-table evidence, and
a bounded multi-stage graph. Scripted schema/request compatibility and the
existing experiment harness are proven; run serial paired model probes and
inspect every failure before expanding.

Compare task correctness and usefulness, unsupported claims, false blockers,
repair frequency, all model/tool requests, latency, and total cost per accepted
result. Include input/output, reasoning, cache, failed-attempt and review usage;
control warm-cache order or disclose it. Keep infrastructure and missing-evidence
failures separate from model-judgment failures. A targeted escalation policy may
be better than switching every agent. Preserve model defaults until held-out
results demonstrate an improvement worth its total cost. One matched live Terra probe has now completed; see the
[pilot results](KBA_V2_PILOT_RESULTS.md#terra-comparison) for its quality and cost boundaries.

## Historical V8 acceptance checkpoint

The latest complete pre-observer gates passed: 6,496 tests with 13 skips in the graph environment; 6,466 with 43 skips in the minimal environment. A complete recoverable source snapshot reproduced its runtime fingerprint; original main source and operator state stayed unchanged.

Live Research now reasons over the supplied evidence, retains all four source identities, and survives native fresh-process resume after deletion of the disposable context-file copy. Opportunity is reached without repeating Research. Its two attempts failed before schema validation and numeric usage retention; the complete business workflow remains blocked.

Next, retain allowlisted terminal-response status/reason and numeric usage before SDK redaction, distinguish incomplete output from invalid schemas, and prevent an unchanged structured retry on a known terminal failure. Verify this with the real SDK and a mocked transport before one saved-input diagnostic. Review the observed count and tool-free instruction conflicts independently. Then complete the business result and route the monitored Slack test explicitly to the isolated worktree. Broad instruction/catalog cleanup remains ANU-369; no extra editable tool registry is proposed because executable ownership already exists in code.

The one-request exact-input diagnostic subsequently confirmed private output-cap exhaustion: `incomplete / max_output_tokens`, 6,000 output tokens, zero reasoning tokens. The observer retained usage and avoided an identical retry. Normal Opportunity construction has no explicit cap; adjust the private proof budget instead of claiming the deployed runtime has a 6,000-token defect. Opportunity instructions now match attached tools and distinguish supported no-action from missing evidence. Planned result counts still need durable explicit-limit provenance; a limit of three allows, but does not require, three records.

## Historical September 11 checkpoint

Current-source full gates passed **6,540 / 13 skips** with graph and **6,509 / 44 skips** without graph. All 1,158 frozen main-source hashes and the original operator database still match. The 1,167-file V9 source snapshot reproduces its runtime fingerprint. Explicit result-limit provenance is additive across constructors, CLI/API and checkpoint readers; legacy snapshots do not fabricate explicit limits.

The next actual Opportunity-stage proof still exhausted 12,000 output tokens for one requested result, with zero reasoning tokens. It retained complete usage and left the WorkItem unchanged. Paid repeats are paused. The immediate next work is a bounded audit of actual instructions and full output-schema obligations, considering an independently tested phase-specific assessment contract if justified. Broad instruction/skill cleanup remains ANU-369, but a directly observed blocker can justify a narrow repair now. Do not claim complete business/Slack acceptance from the successful mechanical gates.

## Supplied Opportunity provenance and semantic review

The supplied-evidence Opportunity phase now rejects invented numeric source-quality
objects/summaries and nonzero current-run retrieval bookkeeping through the existing
bounded decision-repair path. `WorkItemSourceRef.source_quality` is a descriptive
string, not numeric scoring authority; prior model-artifact ratings cannot authorize
new measurements. This phase acquires no authoritative numeric quality data, so
per-source and aggregate quality objects remain null. Model claim confidence and
priority/consulting scores produced by the existing deterministic normalizer remain
separate. Invalid observations are rejected, not silently erased or corrected.

Schema, identity, and provenance acceptance does not prove semantic accuracy. Review
whether the category fits, whether a proposal is mistaken for an open opportunity,
and whether source conditions and uncertainty survive summarization. Source text
takes precedence over conflicting prior summaries. If no existing business category
accurately fits, the model may return no formal records while preserving a substantive,
source-linked assessment in `human_summary`; no taxonomy expansion or Python-selected
category is implied. Direct and native-graph manager controls, including a legacy
company-profile artifact, preserve that canonical summary without renderer changes.
Live output quality still requires source comparison and operator review.

## Same-prompt Terra/Luna test

A controlled Slack test configured Terra for Orchestrator and Luna for Chief/specialists. Terra recognized the relevant Gmail/Outreach roles but asked for context-use approval for the requested private draft. Python still rejected the maximum10 estimate against ceiling8. Only one Terra request occurred; Luna quality and graph processing remain unproven. Prioritize draft-versus-send authority, evidence-acquisition readiness and planner-to-execution agreement alongside request allocation before repeating the mixed-model test. No default model migration is justified by this single blocked case.

Subsequent testing returns to GPT-5.4-mini for Orchestrator, Chief and specialists by operator decision. Retain Terra/Luna as a recorded experiment; correct approval/entry-budget behavior before further model comparisons.

## Historical full-path diagnosis before the next mini test

The next validation should explicitly cover runtime/command identity, both deliverables, private-draft permission versus evidence readiness, executable provider ownership, clarification handling, bounded model allocation, selected WorkItem/graph state, exact email retrieval, typed source-backed drafting context, placeholder composition, separate output constraints, accepted-result persistence and Slack delivery. Recovery and direct/graph equivalence follow the first useful completion. No web research or verified individual contact is needed for the source-limited organization-targeted template.

Two additional review findings are now reproduced offline: execution estimation precedes the clarification return, and the run-diagnosis collector can label authoritative failed records PASS when no listed heuristic finding exists. Correct terminal-aware diagnostics and control-order semantics before treating helper verdicts as acceptance. Existing Outreach WorkItem readiness also requires a selected source-backed company profile/allowed claims, so prove that handoff explicitly rather than force unrelated research or fabricate readiness.
