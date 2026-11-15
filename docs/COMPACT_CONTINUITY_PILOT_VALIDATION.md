# Compact continuity pilot validation

**Subsequent scope change:** the operator authorized extending these capabilities
to the workspace on November 15. See the [workspace rollout record](WORKSPACE_CONTINUITY_ROLLOUT.md)
for current enrollment and isolation checks. Single-channel limits below
describe the historical pilot and its live tests, not the later configuration.

The pilot remains restricted to one configured Slack channel. Model inference
tests are serial, capped at three minutes and eight model requests per run,
with no automatic whole-run retry. Five post-implementation Slack tests were
authorized; the log below counts actual dispatched requests, including failures.
Backend checks in this phase use scripted native SDK transports and temporary
stores, not paid inference. No dependency was added or upgraded.

## Technical index

- [Backend proof](#backend-proof)
- [Serial Slack tests](#serial-slack-tests)
- [Boundaries](#boundaries)
- [Additional serial graph retest](#additional-serial-graph-retest)
- [Post-reload graph reply tests](#post-reload-graph-reply-tests)

## Backend proof

The integrated admission gate passed 235 checks. Earlier affected CLI/context/
graph and Chief/workflow gates passed 782 and 747 checks, and the final Chief
integration gate passed 283. These overlap and are not a sum of unique coverage.
The later completion guard passed 287 Chief checks, including both legacy Chief
suites. Ruff and whitespace checks passed on the changed surfaces.

The checks exercise actual native SDK tool schemas and execution, scoped local
capture and read-back, event idempotency, scope/approval/expiry admission before
ranking, two-fact forwarding, actual JSON byte limits, profile-subject isolation,
no-query behavior, omission of raw session history and unsolicited legacy
memory, stable prompt-cache keys across dynamic facts, direct child transport,
and native graph checkpoints. A completed verified capture has a review action
without a second execution-approval checkpoint. An ordinary future plan keeps
its approval checkpoint; missing authorization or storage receipts cannot pass
as a completed capture.

## Serial Slack tests

| Test | Request purpose | Observed result | Inference evidence |
| --- | --- | --- | --- |
| 1 | Save the operator's compact-state, selective-memory and source-reuse preference | **Not accepted.** Slack honestly reported no memory-write tool. No saved preference was claimed. The scoped query tool and history-omission policy were active. | Five SDK model requests; local aggregate estimate USD 0.01116116, not an invoice. |
| 2 | Retry capture after the planning/admission repairs | **Partial operational success, failed run.** One scoped preference was saved and read back successfully, but the final completion check rejected its tool evidence. The saved record and request provenance were independently inspected; capture must not be repeated. | Seven model starts recorded. The historical child failure envelope did not retain full SDK usage/cost, so a complete local cost total is unavailable. |
| 3 | Use an historical feed preview and relevant operator context to propose one small experiment | **Delivered.** Correct preview URL, two approved profile facts, a concrete Python data-cleaning target, three steps and a before/after review result were visible in Slack. The saved preference was not retrieved in this run. One quantitative sentence omitted the preview's young-library subgroup; source-fidelity instructions were tightened afterward. | Four model starts; one local operator-context query, one scoped channel query and one message read. Approved profile projection: 868 SDK-serialized bytes. No SDK history attached. |
| 4 | Explicitly query the existing compact-context/source-reuse preference in a new run | **Verified.** Preflight retrieved the one saved record, forwarded a 461-byte approved fact, and Chief returned its guidance in Slack. The child query acknowledged the already admitted identity without repeating the full fact. No capture occurred and the scoped record count stayed one. | Four model starts; estimated aggregate USD 0.0098838, not an invoice. Raw SDK session history remained disabled. |
| 5 | Resume the same completed graph WorkItem using only its cached selected article and the saved preference | **Blocked before graph execution.** Preflight selected the correct URL and retrieved the saved preference, but source identity was rejected before the existing saved-source adapter could run. No specialist, source-window read, extraction or new brief ran. The original WorkItem remained unchanged. | Five Orchestrator model starts. This exhausted the five-test Slack allowance; the subsequent backend repair has not been rerun live. |

The first request exposed a host planning defect that the initial fixtures did
not cover: the correct current reference-capture plan was overwritten as generic
Slack operations when the current request also appeared as the thread root.
The Chief itself selected the reference-capture workflow, but its required tool
was withheld by that incorrect upstream plan. The repair preserves coherent
current capability semantics when context identifies the same owner, advertises
conditional downstream capabilities without granting preflight write tools, and
requires a verified capture receipt before a model-selected capture can complete.
The subsequent public CLI regression passed using the exact request shape with
scripted Orchestrator and native Chief SDK transports, the real child script,
and temporary storage. No plan was supplied in advance. That test also exposed
and verified repair of a read-only quality-tier filter that omitted the required
local capture tool. The authorized bundle now contains only query and capture;
other write tools are not added. Default AUTO supports unnamed requests through
the same host-bound bundle, while an explicitly requested scoped mode retains
its existing restrictions. The final combined retry gate passed 164 checks;
the final Chief baseline/scoped gate passed 294. These are overlapping counts.

Test two retained a tool result with `status=saved`, `verified=true`, and the
saved local record ID. The common receipt completion path requires canonical
nested verification or a recognized completion status; the original minimal
receipt did not carry that canonical form. A native SDK reproduction that saves,
then encounters malformed structured output, then repairs through a query-only
turn reproduced the missing-capture failure. The exact historical repair trigger
was not preserved by the child envelope, so the reproduction establishes the
failure mechanism without claiming which parse/decision error triggered the live
repair. Canonical local-write receipts, mutation classification, local-versus-
external write accounting and failure-envelope accounting were then repaired.
The native reproduction passes for both final workflow choices, preserves the
cost guard, hides capture during repair and creates exactly one record. The
integrated receipt/CLI/decision compatibility gate passed 296 checks; the worker's
affected receipt/Chief/durable-execution gate passed 387. Global receipt status
rules were not broadened for arbitrary providers. Test four subsequently
retrieved the already saved record without recapturing it.

An administrative usage snapshot for this phase's time window reported twelve
model requests, matching the five and seven model starts from the two live tests.
It reported 461,335 input tokens, 217,428 cached input tokens and 4,251 output
tokens. This is aggregate usage metadata, not an invoice or proof that optional
memory consumed those tokens. Static instructions, schemas, tools, repeated
model turns and dynamic request data all contribute. The optional continuity
packet retains its separate measured byte cap.

The local save also advances the durable effect revision used by read caches.
An instrumented control cached a successful pre-save query, performed one verified
local capture, then ran the identical query. It executed again and returned the
new approved fact. There was one verified mutation operation, no capture read-cache
entry and no external write. Existing shared policy does not cache pure-empty
query results; the test used an already-admitted fact response to prove actual
cache invalidation rather than assuming an empty result had been cached.

Test five exposed two host admission defects. The source-identity-only block
returned before the saved-source binder, while the actual heuristic plan left
its prior-context dependency unspecified despite the model selecting the exact
saved URL. Once bound, the completed-WorkItem reuse check expected a
`source_summary` artifact, although the saved research artifact was correctly a
`research_brief`. Neither defect was a model choice to search or replace the
article.

The repair validates the current explicit WorkItem, model-selected source URL,
unique saved source and retained snapshot identity before clearing only that
source-identity block. Missing or altered cached files fail without fetching.
Completed-item admission requires the actual local binding object and no
unresolved blocker or required pending approval; a source label supplied by a
model cannot grant admission. Other preflight blocks retain their existing
behavior. A blocked response also now carries its current execution-attempt
identity, avoiding accidental correlation with an older successful thread run.

The independent public CLI regression starts with the real incomplete plan,
current authenticated pilot context, a completed WorkItem and its prior
`research_brief`. Native SDK transports execute the graph reader against the
cached snapshot and save a new source-linked brief on the same WorkItem. No
search, extraction or new memory record occurs. Different URLs, different
WorkItems, missing snapshots and unbound sources stop before reading. The
combined source, reader, graph and recovery gate passed 151 checks; six linked
CLI regressions also passed. These scripted results prove backend wiring and
admission, not natural model behavior or Slack delivery after the repair.
The final parent integration gate passed 222 checks across source resume,
public graph execution, compact continuity, blocked-attempt correlation,
supplied-source isolation, local-capture receipts, registry and prompt contracts.
Ruff, compilation of the changed source-resume modules and whitespace checks
also passed. These gates overlap with the earlier counts.

A further Astra review at medium reasoning reproduced one additional evidence
projection defect through two paths: approved historical WorkItem facts and
project-context source excerpts reached the exact-article specialist input.
The failures were detected inside actual native SDK model inputs in temporary
public CLI graph tests, without provider access. This is a confirmed input-scope
defect; it is not evidence that a live model used the material in an answer.
The stage-local projection now excludes historical facts, artifacts and project
source references before either orchestration or context-pack construction.
Project identity, goals, restrictions and approved operator context remain
available; canonical saved facts and references are unchanged. Both original
Astra public-path reproductions passed unchanged after the repair. The
independent maintained regression also checks persistence after success and
fail-closed cases. The scoped repair and acceptance results are tracked in the
[graph variation review](GRAPH_VARIATION_REVIEW.md#final-cached-resume-review-addendum).

## Boundaries

Raw SDK history is omitted only for admitted pilot requests. The session database
is preserved. Source snapshots and request-scoped source readers retain their
own evidence contracts; source reuse is not equivalent to prompt-cache hits or
proof of complete article coverage. Optional task handles and approved facts are
capped at 6,144 actual SDK-serialized bytes after the operator-approved limit
increase; task handles have a 2,048-byte sublimit and memory has at most four
relevant facts. Recent linked-thread interpretation has a separate 8,192-byte
ceiling. These are not exact token ceilings.
The approved profile is available only to explicitly configured operator IDs.
Other operators may query their own scoped approved memory without gaining that
profile. No cross-channel pooling, broader workspace rollout, external write,
automatic memory capture, commit or publication is demonstrated by this pilot.
The live phase recorded 25 model starts across the five requests. The earlier
administrative usage snapshot covers only the first two tests; it is not a full
phase accounting total. Some conservative child-budget reservations differ
from actual model starts and must not be reported as billed request counts.
The pilot remains enabled only for its existing channel. A fresh live check of
the repaired cached graph resume and capture-completion path is still required
before claiming consistent operation or extending to other channels.

## Additional serial graph retest

On November 15, the operator approved up to two further serial Slack tests after
backend verification. The focused backend gate passed 222 checks before the
first dispatch, with Ruff and whitespace checks clean. The first test repeated
the cached-only graph resume in the original thread and failed before Research
or a source-window read. Three Orchestrator model starts were recorded. The
current WorkItem remained complete, with no unresolved blockers or pending
required approval, and no replacement was created.

The current preflight input contained the correct WorkItem identity and target
URL. The model selected Research and described use of the saved cached article,
but omitted its optional selected-source URL. The heuristic plan also left its
prior-context dependency unspecified. The source binder still depended on one
of those redundant fields and returned no binding, causing the exact-WorkItem
admission failure. This differs from the earlier failure in which the model
populated the URL but a source-identity block ran before binding. The new repair
and natural-language thread-continuation controls require backend acceptance
before using the remaining live allowance.

The usage metadata snapshot after this first additional test reported three
requests, 71,814 input tokens, 46,067 cached input tokens and 798 output tokens.
The local preflight estimate was USD 0.00407782. These are usage and estimated
cost evidence, not an invoice. No graph or source read occurred in this failed
attempt; it cannot establish repaired-graph acceptance.

After source binding and model-owned reply-relation repairs, the second approved
test used only the short reply "Try again." in the original thread. The model
correctly chose `continue_existing` and the Business Research owner, but also
chose `context_only_response=true`, reasoning from an older completed summary.
The provider-free composition gate could not admit that response and blocked
before graph execution. Five Orchestrator model starts were recorded; no source
window was read and no replacement WorkItem or new brief was created. Both
additional Slack allowances are now exhausted. A completed execution envelope
for delivering a blocked result is not successful graph execution.

The pre-live gate passed 562 backend checks, including 18 public CLI/native
graph variants for short continuations, branch/new-task isolation, ambiguity,
wrong identities and missing saved plans. This did not prove that a live model
would choose the compatible execution mode. The subsequent repair applies the
existing composition-admission predicate inside the model decision validator:
an incompatible context-only choice receives structured feedback for the
existing one-turn repair. Python does not rewrite the model's choice. A local
model transport corrected the mode on its second turn, while an admitted
completed-result rewrite still completed in one turn. Live response to that
feedback remains unverified.

The host capture review also confirmed that the pilot's early lazy-context
return omitted recent thread messages. The Slack host now retains lazy channel
history but captures one bounded page of the current thread for replies,
excludes the triggering and future messages, and passes the root plus up to
seven prior turns. KBA admits at most six excerpts within its 8,192-byte thread
budget. Incomplete long-thread reads are reported as incomplete, rather than
claiming the latest messages were available. The host also supplies the exact
latest failed/blocked attempt status from its append-only journal, only for the
same linked task and thread. KBA validates that scope and prior timestamp before
model use. Task status and attempt status remain distinct.

The Slack host's actual-checkout offline checks passed 75 thread/run-record
tests and 30 runtime-guard tests. The latter used the available KBA test
interpreter because the host's documented Python interpreter lacked pytest;
the runtime itself was not migrated or given new dependencies. Source compilation
and whitespace checks passed. No extra Slack or model request was used for these
checks. These repairs still require a fresh authorized live acceptance check.
The final combined KBA gate passed 599 checks after the execution-mode repair,
thread-attempt projection and context-budget increase. Ruff, source compilation
and whitespace checks passed. The idle Slack worker was reloaded through its
active-request guard, and a fresh connected hello was observed at 09:25 Eastern
on November 15. No new agent request or automatic replay was dispatched. This
proves the repaired host loaded and connected, not live graph acceptance.

The final administrative usage snapshot for both additional tests reported eight
requests, 180,960 input tokens, 131,427 cached input tokens and 1,766 output tokens.
Their aggregate local model-token estimate was USD 0.00838830, not an invoice or
a full provider charge statement. All recorded model requests used the configured
`gpt-6-luna` path. The original approved preference remains one record; no capture
or broader channel rollout was performed by these retests.

## Post-reload graph reply tests

The operator then approved up to two more serial graph-reply tests. Backend
readiness was the final 599-check KBA gate plus the Slack host checks above;
the first new dispatch occurred after its guarded reload and fresh connection.

| Test | Current reply | Observed result | Consumption |
| --- | --- | --- | --- |
| 1 | "Try again." with no task ID or source URL | **Accepted for this pilot case.** The host captured eight prior thread messages and the latest blocked attempt. The Orchestrator chose the same task and Research owner with execution mode, not context-only mode. Native graph Research read the exact cached source and saved a new brief on the original WorkItem. Slack delivered the brief in the original thread with formatted sections, source link, evidence limits and saved-task status. | Seven model starts of the eight-request ceiling; approximately 47 seconds of child execution. Local model-token estimate USD 0.01224903. |
| 2 | Separate graph branch from the same cached article, preserving the original task | **Partial: unnecessary clarification.** The model introduced a competing article by resolving an older feed reference against the newer request date. It asked which article to use. No branch or new brief was created, no article window was read, and the original WorkItem's complete serialized-state digest was unchanged. | Four Orchestrator model starts; approximately 20 seconds of child execution. Local estimate USD 0.00459823. |

For the accepted continuation, the initial supplied source covered characters
0–1,200. Four current-run reader receipts covered 1,200–9,084, 9,084–17,001,
17,001–24,945 and 24,945–30,639 under the same immutable snapshot hash. They form
continuous coverage through the available cached text. This proves cached-text
reading in this run, not independent corroboration, linked-reference inspection,
or a fresh fetch of the external article. No search or new extraction ran.
The source summary preserved the mature/young library populations and stated
the limits of the study. Raw SDK history stayed detached; source reuse and
provider prompt caching remain distinct.

The branch trace showed a temporal-reference defect: the latest reply said to
retain the cached source, but a historical "two days ago" reference was treated
as a new source selection. The context projection now supplies explicit
`sent_at` values and `thread_started_at` in Eastern time, and the Orchestrator
instructions distinguish historical selection provenance from a current source
change. The follow-up focused gate passed 344 checks, with Ruff and whitespace
checks clean. This final guidance/input correction has not been rerun live.
The successful continuation remains valid evidence; branch behavior remains
unaccepted pending another separately authorized test.

Local current-run receipts total eleven model requests, 353,139 input tokens,
258,120 cached input tokens and 4,779 output tokens. Their local aggregate
model-token estimate is USD 0.01684726, not an invoice. The immediate final
administrative snapshot exposed only the branch's four requests; provider
aggregation did not yet reconcile the successful continuation, so the platform
snapshot is partial and must not replace the complete local request receipts.
Both new allowances were used. The pilot remains restricted to its original
channel; no broader rollout or additional live replay occurred.

Refs: [implementation plan](COMPACT_CONTINUITY_IMPLEMENTATION_PLAN.md),
[graph variation review](GRAPH_VARIATION_REVIEW.md),
[state and session audit](CROSS_SESSION_MEMORY_STATE_REVIEW.md).
