# KBA V2 pilot results

Snapshot: 2026-09-11, saved pilot reports, public-workflow attempts and serial Slack tests. These small GPT-5.4-mini and GPT-5.6 Terra probes
identify useful behavior and concrete failure modes. They do not establish broad
quality improvement, production acceptance, or superiority of additional agents.
No blinded human ratings are recorded. Every report records zero provider writes.

## Recorded runs and accounting

“Requests” counts admitted model requests, including responses that later failed
validation. A retry denied before dispatch is not another admitted request.
Tokens below are recorded input/output usage; cached tokens are included in input.

| Saved report | Requests | Known input / output tokens | Known estimated USD |
|---|---:|---:|---:|
| [Baseline](../.keystone/v2/live-baseline/report.json) | 1 | 3,467 / 246 | 0.00370725 |
| [Compact context](../.keystone/v2/live-compact/report.json) | 1 | 3,140 / 227 | 0.00147570 |
| [Self-review](../.keystone/v2/live-self-review/report.json) | 3 | 3,467 / 258, author only | 0.00151485 + unknown |
| [Saved-review diagnostic](../.keystone/v2/live-saved-review-diagnostic/report.json) | 1 | 4,033 / 25 | 0.00313725 |
| [Seeded critic before schema fix](../.keystone/v2/live-seeded-critic/report.json) | 1 | 3,318 / 141 | 0.00312300* |
| [Seeded critic after schema fix](../.keystone/v2/live-seeded-critic-v2/report.json) | 1 | 3,560 / 148 | 0.00333600 |
| [Seeded revision](../.keystone/v2/live-seeded-revision/report.json) | 1 | 3,402 / 177 | 0.00334800 |
| [Document text only](../.keystone/v2/live-layout-text/report.json) | 1 | 3,392 / 180 | 0.00145320 |
| [Document text plus image](../.keystone/v2/live-layout-image/report.json) | 1 | 4,624 / 180 | 0.00237720 |
| [Public workflow stopped in preflight](../.keystone/v2/live-public-graph/failure-trace.json) | 3 | 54,078 / 5,712 | 0.04241610 |
| [Terra sandbox transport failure](../.keystone/v2/live-terra-baseline/report.json) | 1 local admission, no response | Unavailable | Unavailable |
| [Terra matched baseline](../.keystone/v2/live-terra-baseline-network/report.json) | 1 | 3,467 / 331 | 0.01263800 |
| [Second public workflow preflight failure](../.keystone/v2/live-public-graph-v2/failure-attempt.json) | 2 | 41,038 / 4,023 | 0.03523080 |
| [Instrumented planner diagnostic](../.keystone/v2/live-public-graph-v3/failure-attempt.json) | 1 | 20,519 / 1,856 | 0.02374125 |
| [Planning passed; persistence blocked](../.keystone/v2/live-public-graph-v4/cli-output.json) | 1 | 21,125 / 1,761 | 0.02376825 |
| [Native restart passed; Research bypassed SDK](../.keystone/v2/live-public-graph-v5/planning-checkpoint-proof.json) | 1 | 21,125 / 1,709 | 0.02353425 |
| V6: live plan; Research blocked by private input cap before dispatch | 1 | 21,125 / 1,612 | 0.02309775 |
| V7: live planning repair and accepted Research; Opportunity input precheck blocked | 3 | 73,855 / 5,056 | 0.06414645 |
| V8: live plan and Research; native restart reaches two failed Opportunity attempts | 4 | 52,634 / 3,241, plan and Research only | 0.05406000 + unknown |
| Exact saved Opportunity input with terminal observer | 1 | 38,286 / 6,000 | 0.05571450 |
| Current Opportunity stage; one-result ceiling and 12,000-token allowance | 1 | 38,458 / 12,000 | 0.08284350 |
| Scoped supplied-evidence instruction profile; structural acceptance, semantic partial | 1 | 23,886 / 2,795 | 0.03049200 |
| Slack newsletter workflow: planner decision-contract failure | 1 | 21,418 / 2,640 | 0.02794350 |
| Slack link transport scope rejection | 0 | 0 / 0 | 0 |
| Slack email-only workflow: accepted planning, request-estimate block | 1 | 21,205 / 2,543 | 0.01438725 |
| Same Slack prompt, Terra planner / Luna configured specialists; clarification then estimate block | 1 | 21,205 / 1,300 | 0.05801000 |
| **Recorded subtotal** | **35 local admissions** | **505,827 / 54,161** | **0.59549605 + unknown** |

Numeric usage covers thirty responses, including 107,264 cached input and
3,464 cache-write tokens. One additional local admission failed in sandbox DNS/
connection setup and returned no provider response; it is not a demonstrated
provider admission or a quality result.
The two original failed self-review responses and both V8 Opportunity failures have no retained token or cost metadata. The dollar subtotal sums available local pricing estimates, not billing
receipts; it is not a complete bill. *The first seeded-critic report marks its
usage/cost scope incomplete after a subsequent retry was blocked. Its one admitted
response has recorded numeric usage; the denied retry is not added to the total.
Later telemetry improvements do not reconstruct these four missing records.

## What the probes establish

**Compact context reduced input size on one held-out wording.** Serialized case
input fell from 2,690 to 962 characters: 1,728 fewer, or 64.2%. Full model input
fell from 3,467 to 3,140 tokens: 327 fewer, or 9.4%; shared instructions and schema
still contribute overhead. Both answers distinguished retrospective performance
from unmeasured prospective clinical benefit. The lower observed cost cannot be
attributed entirely to compaction: baseline cached zero input tokens, while the
later compact run cached 2,816; output length also differed, 246 versus 227 tokens.
This was not a randomized or cache-controlled comparison.

**Self-review exposed a validation failure, not a demonstrated quality gain.**
The initial author response completed; both review attempts failed, for three
admitted requests total. The old report labels the observation `blocked`, but its
review stage records `ModelBehaviorError`. The detailed original schema reason
was not retained. A separate one-request diagnostic used the identical saved
review input fingerprint and returned `accept` with no findings. This establishes
that the saved review can succeed; it does not identify the earlier cause or prove
a deterministic repair. The original report remains historical evidence.

**The planted-error critic revealed a specific schema gap.** Its first response
violated the requirement that each finding target exactly one claim or requested
obligation; a retry was denied by the one-request cap. After the wire-compatible
schema union enforced the two exclusive forms, one fresh request on the same
input fingerprint produced a valid review. It correctly challenged synthetic
claim `h2`: retrospective evidence does not establish prospective patient benefit.
It also raised deployment obligation `o_deployment`. Codex review finds that concern
largely redundant: the seed already recommended requesting prospective evaluation
before routine deployment. More explicit wording could help, but this should not
be counted as another independently verified defect or a blinded quality score.

**One targeted revision corrected the planted claim.** The resulting answer
explicitly says prospective benefits are unestablished, includes the missing
evaluation limitations, and retains `h1` as a source-linked retrospective-accuracy
claim. Its text and source list exactly match the seed. This is one successful
repair example, not evidence that a critic improves every answer. Codex reconstructed
both probe inputs from the [synthetic catalog](../evals/static/v2_experiments.json)
and the saved probe scripts, matching their recorded input fingerprints before
comparing the seed with the result; the scripts were not executed during this review.

**The page image recovered missing cells but introduced an unsupported statement.**
The paired document reports reference the same synthetic page, extraction and gold
hashes. Text-only output correctly treated the omitted prospective-evaluation
cells as unknown. With the page image, the answer correctly recovered “No” and
“Not measured.” However, it also claimed that the extracted text and image agreed
on that row. The extraction deliberately omitted those values, so that agreement
claim is unsupported. Preserve the extraction gain while testing whether future
answers distinguish what came from text, image, or both. One synthetic page does
not establish general document-extraction quality.

## Pending operational evidence

| Probe | Status | Evidence required before closing |
|---|---|---|
| Live graph workflow on a public source | **Partial: accepted live plan and Research survive native restart; Opportunity fails before schema validation** | Diagnose terminal model responses, complete Opportunity, and retain final-result/delivery evidence. Existing checkpoint recovery is proven separately from semantic completion. |

## Live entrypoint failure before graph execution

The normal-entrypoint public-source attempt admitted three Orchestrator requests
under a six-request ceiling; three remained. It failed before Research or any
LangGraph node executed. The terminal exception was `AgentDecisionValidationError`.
Both recorded semantic attempts report
`orchestrator_needs_more_context_selection_conflict` and select the `clarification`
route. The trace does not identify which decision row held the conflicting flag;
one separate structured-output retry also lacks its original validation detail.
All three response usage records survived, including 1,215 reasoning-output tokens
already included in output usage.

Offline inspection found two concrete contracts to repair before another paid
attempt. Recognized non-Slack source-bundle files were omitted from the Orchestrator's
preflight context, despite being available downstream. Separately, the main routing
contract required a route selection but advertised an unreachable combination of
no selection and `needs_more_context=true`. Clarification should remain a valid
routing choice, with missing information expressed in `clarification_request`.
Neither repair should choose a route for the model or grant provider permission.

## Next decisions

Keep compact-context and page-image comparisons as candidates for broader paired
testing. Measure unsupported claims and false-positive criticism alongside useful
corrections. Test the critic on clean answers and omitted work as well as planted
errors; do not infer benefit from consensus or an accepting review alone. Control
cache order and include failed, review and repair requests in cost per accepted
result. Preserve current model defaults until held-out, blinded comparisons justify
a change.

Use the [implementation and proof boundaries](KBA_V2_IMPLEMENTATION.md),
[experiment catalog](KBA_V2_EXPERIMENTS.md), [Agents API assessment](KBA_AGENTS_API_ASSESSMENT.md),
and [Terra comparison plan](KBA_V2_IMPLEMENTATION.md#model-comparison-added-by-the-operator)
for the separate architecture, operational and model-selection decisions. These
pilot results do not establish managed Agents API acceptance or a general model ranking.

## Operator billing confirmation

At 5:01 PM ET on 2026-09-10, the operator supplied the billing page showing
**$9.01**, compared with the **$9.04** starting balance: approximately **$0.03**
of displayed balance change. This confirmed the earlier pilot spending before the subsequent $0.04241610
preflight attempt; it is not a final balance observation for all rows above. It does not recover
those responses’ missing token breakdowns.

## Terra comparison

One tool-free GPT-5.6 Terra request used exactly the same author input and
instruction fingerprints as the saved GPT-5.4-mini baseline. Both correctly
distinguished retrospective performance from unestablished prospective clinical
benefit and declined to treat the supplied evidence as sufficient for routine
deployment. Terra made the missing study details and uncertainty more explicit.
This is Codex's qualitative review of one paired case, not blinded acceptance or
evidence that Terra improves every KBA task.

Mini used 3,467 input / 246 output tokens and about $0.00370725; Terra used
3,467 / 331 and about $0.01263800, roughly 3.41 times the estimated cost. Neither
reported reasoning-output tokens or cache reads. Terra reported 3,464 cache-write
tokens, which the new accounting includes. The models have different cache
policies, output lengths and rates; this is observed case cost, not a general
price/performance ranking or a randomized latency experiment. Keep the default
mini model and test harder held-out tasks before deciding on targeted escalation.

The initial Terra attempt failed with `APIConnectionError` in roughly 0.29 seconds;
a non-model DNS check confirmed the sandbox could not resolve OpenAI's hostname.
The same one-request probe succeeded after authorized network escalation. The
transport failure was not classified as model rejection or a reasoning failure.

A fresh billing-page reload after the Terra response showed **$8.96**, about
**$0.08** below the original balance. This is consistent with the local subtotal
and earlier unretained usage, at the precision of the displayed balance.

## Second public-workflow planning failure

After the source/routing fixes and complete offline gates, a new normal-entrypoint
run added explicit KBA design goals to the same public source packet. It stopped
before graph entry with two `ModelBehaviorError` structured-output failures. Four
requests remained; no WorkItem or provider operation was created. Retained usage
is 41,038 input / 4,023 output, including 20,224 cached input and 615 reasoning
output tokens (already included in output). Estimated cost: $0.03523080.

The SDK's privacy redaction removed the underlying validation detail before the
application retained it. The exact failing field or validator remains unknown;
scripted cases demonstrate several possible cross-field constraints but do not
establish the live cause. Add bounded field/validator diagnostics before another
paid attempt, preserving SDK privacy and actual acceptance rules. Also preserve
new numeric usage fields through the storage/trace redactor; the older trace
replaced a missing cache-write count with a redaction marker. Do not alter the
historical failure to imply those details were captured.

A fresh billing reload after this attempt showed **$8.92**, about **$0.12** below
the initial balance.

## Confirmed selection-set failure

A one-request diagnostic used the same supplied-source task, disabled automatic
structured-output retry, and checked source visibility at the actual SDK model
input boundary. All four official source identities were present. The response
failed at `AgentDecisionRecord._validate_selection_shape`: its explicit selected
identity set differed from the IDs assessed as selected. This is the confirmed
cause of this response; earlier uninstrumented responses may have different causes.

The retained CLI record now contains the safe field path, error type and validator
location, with no model body or private values. Usage was 20,519 input / 1,856 output,
including 261 reasoning tokens already counted in output, with no cache reads and
an estimated $0.02374125 cost. Five of six execution requests remained; the graph
had not started. Billing refreshed to **$8.90** afterward.

The next correction preserves rejection of conflicting selections: clarify the
shared schema and routing prompt, and give the existing structured retry bounded
validator feedback instead of simply resending the same prompt. Do not choose
a replacement route in Python or increase the retry allowance.

## Planning passed; persistence boundary failed

After the selection-set guidance change, the next preflight passed both schema
and semantic validation on its first response. It selected Chief as manager and
Research then Opportunity as the child workflow, with the same complete selected
set in its assessments. This is one successful reproof, not a measured reliability
rate; no live guided retry was needed.

The subsequent graph-entry preparation rejected the request because the storage
redactor treated the legitimate `cost.billable_tokens` map as a secret. No native
graph node, WorkItem or provider operation ran. The captured CLI output retains
the accepted plan and usage: 21,125 input / 1,761 output, estimated $0.02376825.
The original entrypoint row missed that usage after the handled downstream error;
it has not been retrospectively rewritten. Corrections now preserve safe numeric
billing maps and capture preflight observations before later dispatch failures.

The successful path also revealed an assignment-time Pydantic warning: a raw dict
replaced the typed workflow-state summary. The correction uses the existing summary
model, keeps scoped fields, and retains the full source packet before the model
and in the request snapshot.

## Native checkpoint restart exposed a semantic bypass

The next isolated public-source run accepted its plan on the first model response
and persisted native graph state before WorkItem preparation. It retained four
source identities, the accepted Research → Opportunity sequence and one consumed
request under the original six-request limit. An intentional process exit and
fresh-process resume reached Research using the same execution identity.

Research then returned a deterministic placeholder with fixture provenance and no
specialist model request despite `live_sdk=True`. This proves native restart at
that boundary, but fails the required live agent interpretation. The paused
checkpoint and its artifact are preserved as partial evidence. They must not be
patched or resumed under changed source fingerprints to manufacture acceptance.

Read-only propagation checks found related supplied-evidence bypasses in
Opportunity, Gmail and Chief. Repairs should use the owning registered SDK with
bounded evidence while keeping provider retrieval disabled when the task requires
supplied evidence only. Successful fixture execution is not sufficient proof.

The planning response used 21,125 input / 1,709 output tokens, including 269
reasoning tokens already included in output, estimated at $0.02353425. The
Research resume used zero additional model requests. Paid testing paused for
diagnosis and offline correction.

A fresh billing-page reload after this attempt showed **$8.85**, about **$0.19**
below the starting balance. The local subtotal remains an estimate with two
earlier unretained response costs; the displayed change is not a per-run invoice.

## Next acceptance priority: monitored natural-language Slack work

The operator prioritized complete Slack tasks because they expose interpretation,
thread-context, delegation and visible-output defects that isolated probes miss.
Prepare one recent newsletter email as the anchor for email review → company
research → relevant contact identification → an outreach draft in the same Slack
thread. Read actual provider evidence, join each stage to its retained execution
record, and inspect both content and rendering before starting another run.
An outreach draft in Slack does not authorize sending an email or creating a
provider draft. The exact prompt and current worker version must be reviewable
before the live test.

## Latest sequential diagnosis: V6–V8

V6 admitted only planning; a private 128 KB whole-request guard stopped Research before dispatch. After offline measurement, V7 admitted two planning responses (one structured repair) and one grounded Research response. Its next stage stopped before dispatch because duplicated context exceeded the existing 48,000-character Opportunity limit. A lossless inference projection removes only exactly equivalent repeated summary fields; distinct evidence and durable context remain intact.

V8 passed the complete graph and minimal offline gates, then accepted a live plan and Research result. A fresh process resumed the same native execution after the disposable input-file copy was deleted. All four sources remained visible; Research was not repeated. Opportunity made two identical-input attempts and returned a truthful blocked result. Neither reached schema validation, and neither retained numeric usage. This exposes a separate SDK terminal-response observability gap: failed/incomplete Responses can raise before the usual end-of-model usage hook.

Offline interception reproduced both live input hashes and measured 193,111 serialized bytes, no tools, and a 6,000-token output cap. Reasoning and verbosity were omitted. Output-limit exhaustion remains a hypothesis until a terminal status/reason is captured. Independently confirmed instruction defects are the unconditional scoring-tool requirement in a tool-free stage and inconsistent no-action guidance; the accepted one-result plan also expands to a three-result limit. No full workflow retry is justified before these diagnostics are reviewed.

A refreshed billing page after V8 displayed $8.71. The approximately $0.33 balance change is consistent with known local estimates at displayed precision but does not recover missing per-response usage or establish a final invoice.

## Confirmed terminal-response diagnostic

One isolated replay exactly matched both V8 inputs and settings. The new observer captured `status=incomplete`, `reason=max_output_tokens`, 38,286 input tokens, 6,000 output tokens and zero reasoning tokens. Estimated cost was $0.05571450. It made one request and no retry, performed no WorkItem advancement or provider writes, and preserved original history. This confirms that the private 6,000-token harness cap can truncate this request. The ordinary Opportunity builder has no explicit output cap; the evidence does not establish a graph-recovery failure or justify a production-wide limit increase.

The new terminal observer has 231 related offline passes, including the real SDK with a mocked transport, and 50 minimal-environment observer/operator checks. Specific scoring-tool/no-action prompt corrections passed 457 related tests with one skip. The next proof should use the actual current Opportunity stage with semantic/identity validation and a sufficient explicitly budgeted output allowance. The prior four responses without usage remain unknown; this replay cannot reconstruct them.

The billing page refreshed to $8.63 after this diagnostic. No additional model run or Slack post followed this observation yet.

## Current stage still exhausts the output allowance

After the complete current-source gates passed (6,540 graph tests; 6,509 minimal tests), one actual supplied Opportunity-stage request used the preserved Research evidence, an explicit one-result caller ceiling, updated instructions, and all existing semantic/identity checks. A private 12,000-token output allowance and 120-second timeout replaced the earlier 6,000-token harness cap. The original heuristic plan was not relabelled: its model preflight accepted route/order, not every count or permission field.

The response again returned `incomplete / max_output_tokens`: 38,458 input, 12,000 output, zero reasoning tokens, estimated $0.08284350. There was one request, no retry, no tools, and no WorkItem change. Partial model text was not retained; its contents and the exact source of output expansion remain unknown. The billing page refreshed to $8.57, with potential reporting lag.

Further paid tests are paused for full offline instruction and schema review. The schema exposes the complete business result, including repeated source-bundle, quality, state and trace structures. That is a reason to investigate a narrower phase-specific reasoning contract, not proof that the schema alone caused the observed truncation. Any change must preserve model-owned proposal/eligibility/selection and exact source identity while keeping public results and direct/graph compatibility intact.

## Instruction scoping comparison: completion improved, semantic review remains partial

The next controlled comparison kept the exact input/evidence, output schema, mini model, tools, requested count, 12,000-token cap and 120-second timeout. Only the supplied-evidence instruction profile and its derived cache hint changed. Instructions shrank from 91,844 to 21,244 bytes, and complete serialized request components from 194,086 to 121,779 bytes.

One response completed and passed existing schema/identity/decision checks: **23,886 input / 2,795 output**, zero cached input, estimated **$0.030492**. The incomplete baseline used 38,458 / 12,000 and $0.0828435, also with zero cache reads. The observed input reduction is 37.9%; this is one unblinded comparison, not a general reliability or quality result.

Independent review rates the result **partial**. It gives a reasonable internal experiment recommendation and preserves all four source identities and no-write boundaries. However, it assigns a clinical domain to general agent tooling, marks an internal proposal open, invents numeric source-quality ratings, and repeats a narrowed recovery caveat from the prior Research summary. That caveat is general in the original source; it is not restricted to self-hosted compute. The concurrency-versus-spend limit is also omitted.

Attribution is explicit: the 95-point quality ratings are model-produced; the 78 priority and 94 outside-consulting values were computed by the existing deterministic normalizer from the chosen category and signals. The input already contained 0.99 claim confidence. Correct arithmetic amplified unsuitable inputs; it did not establish commercial relevance. The accepted artifact remains unchanged, with a separate substantive review preventing promotion into trusted business state.

The immediate corrections reject unobserved quality/bookkeeping through existing repair, preserve source scope over previous summaries, and allow a substantive assessment without forcing an unfitting formal record. Scoring applicability and unknown/cautionary signal weighting are separate backlog work. No taxonomy expansion or Python replacement of the model's semantic choice is presumed necessary for this internal test case.

## Monitored Slack acceptance

Two model-backed natural-language Slack attempts stopped before specialist/provider execution. The first failed nested planner selected-set validation. A separate linked prompt was rejected before dispatch because Slack transformed its URL representation; this is a test-harness failure, not another model admission. The revised second attempt passed planning on its first response, then the post-planning request estimate rejected execution with seven of eight requests unused. Neither run created a WorkItem, used a business provider, generated a draft or exercised native graph execution. Failure delivery and desktop presentation were verified; original worker routing and operator state were restored.

The acceptance profile now permits an explicit zero-or-one structured correction under the unchanged total request and output limits. Ninety-one focused offline tests passed, including actual SDK/mock-HTTP correction and independent budget limits. No live correction was needed by the second attempt. Investigate the estimate and planned capability ownership before another live batch; do not treat these results as workflow acceptance.

## Terra Orchestrator / Luna specialists Slack comparison

The same email-only prompt, instruction hash and output-schema hash produced one valid Terra planning response. Terra recognized Gmail/Outreach ownership but selected clarification for approval to reuse the specified source in a Slack-local unsent draft. The prior mini result assigned Gmail reading to Workspace. The Python maximum10 request estimate again blocked the ceiling8 after only1 response. Luna never ran; there is no live specialist-processing or completion comparison. Better role recognition in one case is not general model superiority.

Terra used21,205 input/1,300 output tokens with no cache reads/writes, estimated$0.05801. Mini had19,200 cached input tokens, so the costs are not cache-matched. Event/time metadata also differs. The mixed profile retains read-only permissions and totalrequest8, with one maximumTerra response; that extra test bound did not cause the observed stop.319 focused offline tests passed. Runtime defaults and original Slack routing remain unchanged.
