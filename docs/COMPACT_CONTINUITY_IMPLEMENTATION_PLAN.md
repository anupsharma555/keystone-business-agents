# Compact continuity implementation plan

**November 15 rollout update:** the operator subsequently authorized workspace
enrollment. The [workspace rollout record](WORKSPACE_CONTINUITY_ROLLOUT.md)
describes the current scope, per-channel isolation and activation evidence.
The original proposal and pilot checkpoints below remain historical evidence.

**Recommendation:** begin with a small, source-linked task continuation packet for the existing single-channel pilot. Keep seven-day channel retrieval selective, preserve the exact current request, and let the Orchestrator decide what previous material is relevant. Add approved memory only when an eligible fact fills a concrete gap. Leave SDK history off for ordinary new roots until a bounded tail demonstrates value beyond the task packet. Preserve existing compatible prompt-cache reuse without enlarging memory or relaxing privacy controls.

This is a design deliverable, based on source inspection on 2026-11-14 Eastern and the companion audits. **No runtime implementation, provider/model calls, deployment, configuration changes, database writes, or live tests were performed for this plan.** Only this document was created. The checkout contains concurrent work; the cited source and audit documents may advance independently. Exact private run/channel identifiers and message bodies are deliberately absent.

The [cross-session audit](CROSS_SESSION_MEMORY_STATE_REVIEW.md) establishes what was retained in the inspected runs; the [channel-context review](CROSS_CHANNEL_CONTEXT_REVIEW.md) owns the developing retrieval and live-acceptance history. This plan does not upgrade their evidence. In particular, forwarding completed channel reads into general-answer synthesis and permitting one tool-free citation correction fixes evidence handoff; neither constitutes reusable memory or cross-session continuity.

## Technical index

- [Existing facilities and gaps](#existing-facilities-and-gaps)
- [Smallest implementation slice](#smallest-implementation-slice)
- [Continuity contract and ownership](#continuity-contract-and-ownership)
- [Exact source and task handles](#exact-source-and-task-handles)
- [Approved memory admission](#approved-memory-admission)
- [Tailored next actions from profile and skills](#tailored-next-actions-from-profile-and-skills)
- [Conditional SDK history](#conditional-sdk-history)
- [Conflict, freshness and approval rules](#conflict-freshness-and-approval-rules)
- [Budgets and marginal value](#budgets-and-marginal-value)
- [Cache reuse with small memory](#cache-reuse-with-small-memory)
- [Implementation sequence and compatibility](#implementation-sequence-and-compatibility)
- [Backend test matrix](#backend-test-matrix)
- [Bounded live verification plan](#bounded-live-verification-plan)
- [Complete package review](#complete-package-review)
- [Decisions and remaining uncertainty](#decisions-and-remaining-uncertainty)
- [Evidence and verification](#evidence-and-verification)

## Existing facilities and gaps

| Surface | Verified implementation or cited audit finding | Gap relevant to this proposal |
| --- | --- | --- |
| Channel context | The request-bound capability supplies at most seven days in the active channel, two bounded queries and two exact reads. Candidate IDs are admitted within that capability instance. | A previous run's candidate ID cannot simply become a valid candidate in a new run. Reuse needs explicit rebinding and current scope validation. |
| Completed-read projection | `runtime/channel_answer_evidence.py` admits successful reads matching the accepted capability and keeps historical-only authority, coverage, source links and visible-citation validation. | It projects evidence within an execution; it does not discover or authorize prior executions. |
| WorkItems | SQLite WorkItems and typed context packs are canonical business state; memory hydration already exists. | Direct answers can have no WorkItem. Creating a WorkItem for every answer would add state without demonstrated need. |
| Slack records | Append-only attempt rows and a latest thread projection carry request, outcome, artifacts, WorkItem and telemetry fields. | A thread projection is mutable; reuse must identify an exact attempt/artifact and validate its lineage. |
| Durable execution | Accepted request snapshots and stage results support exact recovery. | Retry/replay identity is not a new user's continuation identity or a fresh approval. |
| Memory | `MemoryItem` has source IDs, approval, sensitivity, confidence, expiry and supersession. Retrieval filters eligibility before local lexical ranking. | No first-class destination/channel ACL is present in the inspected memory schema/retrieval signature. Approved and prompt-safe do not by themselves establish destination eligibility. |
| SDK history | Optional local SQLite sessions are keyed by Slack workspace/channel/thread; route is excluded. Existing adapter preserves dependent reasoning/call/output items. | Six or 24 items is not a hard token bound. Audit metadata reports attachment/configuration, not actual loaded history size or useful influence. |
| Prompt cache | The shared SDK already derives stable profile keys from scope, model, instructions, tools, schema and data-handling profile. | Compatible keys are not receipts of a cross-session hit. Dynamic context and eligible cache endpoints still matter. |

The companion memory audit found no attached SDK history, memory-retrieval evidence or WorkItem linkage in the inspected pilot answers. It measured preflight first requests above 21,000 input tokens in two blocked cases, before channel-tool responses. Thus the existing prompt overhead cannot be attributed to cross-session memory. The same audit found almost all stored memory was older efficiency/retrieval metrics, rather than project facts or strategy. A relevant-memory selector may correctly return zero; filling a quota with operational metrics would make the pilot worse.

## Smallest implementation slice

Implement **reference continuity with measured admission**, initially for the already opted-in pilot channel and both mention forms, across direct and native graph entrypoints:

1. Preserve current raw request and request identity unchanged. Resolve an explicit reply's exact prior attempt or WorkItem through host-owned linkage. For a new root, initially supply only the existing channel capability; do not inject the last task from the channel.
2. Build one optional compact packet containing the prior task/attempt handle, exact selected source/artifact handles, status at capture, a short unresolved constraint if available, and coverage/freshness limits. Use existing structured fields; do not introduce a separate summarizer model call.
3. Present it to the first Orchestrator decision. Model-selected dependencies then travel through the same validated projection to the selected specialist and answer review, including Chief of Staff. A named agent remains routing advice; do not force a handoff or bypass preflight.
4. For new-root references to prior work, let the existing channel query/read locate the relevant post. A bounded host resolver maps an exact returned post or artifact to its canonical attempt and source manifest. The model sees candidate identities before choosing; Python validates the selection and access.
5. Record the packet's actual serialized size, retained identities, omission reasons and decision-selected identities. Do not attach SDK history or general memory automatically.

This slice addresses exact task/source continuity without creating another business-state database or relying on transcript replay. Approved-memory admission and SDK-tail admission are subsequent, independently measurable slices. An unrelated self-contained root should have zero optional continuity content and zero history/provider reads.

## Continuity contract and ownership

Introduce one small internal Pydantic projection, provisionally `CompactContinuityContext`, under a dedicated schema module and a pure builder in `runtime/continuity.py`. These names are **proposed**, not existing APIs. This is an optional context projection, not a new planner, intent taxonomy, workflow authority or general knowledge store.

| Field group | Host-side durable record | Model-visible minimum |
| --- | --- | --- |
| Binding | Schema version, accepted current request/snapshot hash, workspace/channel/thread, creation time, policy revision | Compact scope/age labels; current request remains separately intact |
| Task | Exact prior attempt/execution, optional WorkItem and artifact references, original request provenance, latest revision relationship | Short task handle, objective/constraint excerpt when necessary, status-as-of, unresolved blocker |
| Sources | Canonical source ID/URL, parent message and ordinal, revision, read receipt, extraction window, retrieved-at, completeness, access basis | Opaque short handle, source label, evidence level and as-of/coverage limitation; exact URL when needed for identity/citation |
| Memory | Exact record ID/version, source IDs, applicable scope, approval purpose, expiry/supersession checks | At most a few concise eligible facts with source/age labels |
| Decisions | Candidate universe and validated selected refs, owner/stage, conflict markers | Only current stage's selected context and material unresolved conflicts |
| Admission | Component sizes, token-counter identity, omissions, history count/hash, packet digest | A short limitation only when an omission affects the answer |

Retain full canonical references locally; short handles index a validated run-local map. Never truncate URLs, IDs or conflict/coverage qualifiers to fit a budget. If a complete useful entry does not fit, omit it with a reason and preserve a bounded read path. Do not label a shortened URI as an exact source.

One serialization owner should render selected fact/source bodies once per model input. Context packs, preflight memo and history refer to those same identities instead of repeating their content. A provider-free final citation repair receives the same already-admitted packet and cannot fetch additional context. This preserves the existing repair contract.

## Exact source and task handles

**Same thread:** use exact reply/request lineage from the Slack attempt store, not merely the latest successful-looking text. A failed latest attempt and an earlier completed task must both remain distinguishable when relevant. Resolve a linked WorkItem from its current canonical row; prior reply text is not authoritative present task state.

**New root in the same channel:** keep the seven-day selective capability. The Orchestrator queries/reads the relevant historical message and chooses its task/source identity. The host can then resolve a task handle from that exact returned message's attempt/artifact linkage. Do not infer a task from recency, shared wording or global last-session state. Multiple plausible previous tasks remain candidates or lead to clarification.

**Source reuse:** a persisted source manifest can reuse a completed read as historical evidence when the current request permits historical reuse, the source/revision and excerpt are available, scope remains authorized and coverage is adequate. It must be labeled with the previous retrieval time. A previous message title or link alone never becomes an article read. An artifact containing an LLM summary remains a derived artifact; its claims require the underlying source basis when factual fidelity matters.

**Rebinding:** current `SlackChannelContextCapability.read()` requires a candidate in its current in-memory candidate map. Do not bypass that guard by stuffing an old candidate into it. Extend the existing fixed KS adapter contract with a versioned exact-reference resolution operation, or re-query the narrow identified interval and read the returned candidate. The first option can avoid repeated acquisition only when it returns a validated local receipt/artifact with explicit historical freshness; otherwise use the second. This is a proposed contract extension and needs producer/consumer compatibility tests. No arbitrary DB path or provider URL comes from model arguments.

**Window and access:** an old handle does not enlarge the new request's active-channel seven-day capability. Older task records may still exist, but access outside that pilot window needs a distinct authorized task-artifact scope; until that exists, report the limit. A source URL explicitly supplied by the operator can use existing source tools under their independent gates. Cross-channel references require explicit scope and destination authorization, never just bot membership.

**Persistence:** attach the compact source manifest to the existing durable stage/result/artifact metadata with exact attempt lineage. Avoid a second copy of full article or Slack bodies. If existing joins cannot reliably resolve response-message identity to its producing attempt, add that exact link first; do not attempt fuzzy reconciliation. A durable handle must survive process restart and missing ephemeral context files.

## Approved memory admission

Keep this optional and off for the first reference-only comparison. Enable only for scoped tasks where memory can add a stable preference, project constraint or prior sourced fact absent from current task/source context.

Reuse the existing approved-only, prompt-safe store retrieval and tool boundary. Add a request-scoped admission wrapper that restricts record types, object/project scope, destination eligibility and count **before ranking/limiting**. Global database selection remains host-owned. Do not expose the existing broad `database_url` argument to the pilot model as a means of selecting another store.

The current schema has general metadata but no enforced channel ACL. For the initial opt-in pilot, an explicit host-owned mapping of eligible record IDs or reviewed scopes is safer than treating missing scope as global permission. A typed, versioned metadata scope can avoid a table migration, but validators must reject unknown/unscoped records for this pilot and filtering must occur before the top-k limit. If later retrieval needs indexed scope columns, migrate deliberately rather than interpreting arbitrary free-form tags as authorization.

Local lexical matching can generate a small candidate set; it must not decide which fact changes the answer. The existing Orchestrator chooses relevance from the admitted candidates in its normal decision loop, and the specialist synthesizes the validated selection. No phrase-specific trigger, separate memory judge model or automatic semantic summarizer is needed. Exact task/source needs have priority over optional memory. Retrieval that returns nothing is a valid outcome.

Deduplicate by memory ID/version and source/claim identity against task facts and selected evidence. Exact deduplication is deterministic; uncertain semantic overlap remains visible to the model rather than being silently merged. If both general and specialized context-pack fields reference one memory, render its body only once. Do not inject metrics/efficiency memories into an article-readiness answer merely because they contain a matching word.

Approval purpose and provenance matter: `approved_for_research` can result from a reviewed capture helper, not necessarily individual human review. Approval for memory reuse never authorizes sending, posting, scheduling or provider writes. Expired, revoked, unsafe or superseded items are ineligible. Preserve the existing rule that expiry of a replacement does not resurrect its superseded predecessor. Do not automatically promote pilot answers to memory; capture remains an explicit operator action or separately reviewed policy.

## Tailored next actions from profile and skills

The latest operator feedback identifies another requirement: next steps should be useful for the operator's actual work, rather than generic advice to read an article, consider its relevance or plan an exercise. Adding arbitrary memory will not reliably fix that. The final answer needs a small amount of relevant positioning and project context, plus a clear recommendation-quality contract.

Source inspection found a concrete asymmetry. Full `compose_instructions()` includes `operator_context.md`, whose provenance points to `documents/keystone_business_agents_context_answers.md`. That file contains approved professional positioning, decision priorities, constraints and workflow preferences. `compose_direct_instructions()` includes only the memory and writing policies as shared prompts. The general-answer builder uses the compact composer plus a broad company profile and calls skill selection with the fixed text `general informational answer`. Its answer input has request/format/source evidence but no compact operator/project profile. The compact supplied-synthesis Chief path likewise uses the compact composer. This proves an input/contract difference; it does not by itself prove the cause of every generic recommendation or that all full-profile facts are relevant.

Distinguish three inputs:

| Input | What it can establish | Minimal admission rule |
| --- | --- | --- |
| Stable operator/project profile | Approved role, objectives, enduring constraints, decision priorities and preferred work products | Select only source-backed fields relevant to the current requested decision. Keep provenance/version and approved destination; omit biography and contact details. |
| Episodic approved memory | A prior decision, correction, project fact or outcome at a stated time | Current eligibility and freshness checks; never convert an old project status into a current priority. |
| Runtime skill/custom instruction | How to evaluate and express a useful recommendation | A short stable action-quality contract, plus only a relevant task-specific section. Instructions do not supply facts about the operator, repository or unread article. |

For the first implementation, reuse a reviewed short projection of existing approved profile fields and current explicit request preferences. Preserve exact provenance internally, such as profile document/section/version and current request identity. Do not create a broad new autobiographical profile, infer clinical/client commitments, or persist newly inferred preferences. Current operator requirements for selective channel scope, small memory inputs and cache reuse can guide this request; durable capture remains a separate explicit action. The old profile's stale safety wording must not override current authenticated exact-operation authorization rules.

Place the stable recommendation contract with the existing answer/Chief instructions, for cache reuse. Place the small selected profile/project facts in dynamic input, carried consistently from Orchestrator to the actual answering owner. Budget these facts **inside**, not in addition to, the optional 512-token ceiling; initially aim for two to four brief relevant fields and omit them entirely for unrelated factual answers. Relevant existing runtime-skill sections can be selected from typed task needs or model-selected capabilities; do not add keyword branches for one article or force every runtime skill into the compact answer.

A useful action recommendation should state a concrete target, why it supports the established objective, the smallest feasible next step, and the artifact or decision it will produce. Distinguish recommendations from completed actions, and attach source/repository qualifiers where needed. The final user-facing sentence can be short; these are semantic checks, not a demand for a verbose checklist or a new public output schema.

For example, if the supplied post concerns mutation testing and current context establishes a code-quality objective, a conditional recommendation could be: “Choose one small validation module, try a bounded mutation-testing experiment, and classify surviving changes to decide which missing behavior checks are worth adding.” Selecting a particular repository module requires current repository evidence; a post title alone does not establish the article's technique, the repository's test gaps, or that an experiment was run. An agent without those inputs should say what it would inspect first, with the expected selection artifact/decision, rather than claim a diagnosis. A concrete no-action recommendation can also be useful when evidence or fit is weak.

Backend action-quality cases should score specificity, alignment to admitted profile facts, feasibility under available tools/time/permissions, explicit artifact/decision, source fidelity and absence of invented attributes. Compare the same evidence with no profile, a relevant compact profile, an irrelevant profile and contradictory current instructions. Test both general-answer and actual Chief paths. Profile absence should yield a bounded conditional recommendation grounded in the request, or one targeted question only when the decision is materially ambiguous; do not fill the gap with invented expertise, priorities or project state. A model selecting Orchestrator successfully does not establish actual Chief-owner acceptance.

## Conditional SDK history

Do not turn on global SDK sessions to solve cross-root retrieval. Existing Slack session identity is thread-scoped; broadening it to a whole channel would mix unrelated tasks and increase accidental instruction carryover.

For a true in-thread follow-up, retain a small SDK tail only when the compact task/source packet omits useful conversational meaning. An eventual bounded history adapter should load a bounded suffix, form complete dependent reasoning/tool-call/tool-output units, then admit whole units within the remaining budget. If a unit is too large, omit it and rely on the task/source packet or explicit read. Never split a tool output from its call, cut opaque reasoning content, or make a 128-token target override provider validity. Protect the current request from all history trimming.

The existing adapter already widens item-based suffixes to preserve dependencies. Extend that boundary rather than replacing it with naive `history[-N:]`. Record actual loaded/admitted item counts and sizes, dependency-driven widening, omissions, and a content-free digest. Attachment alone is insufficient proof. Do not prune the persistent session store as part of prompt admission.

Current official SDK documentation supports bounded session retrieval and custom history merging; the installed 0.22.2 `RunConfig` also exposes the relevant hooks. This is an integration option, not a recommendation to upgrade the SDK or adopt server-managed continuation. [Official session documentation](https://github.com/openai/openai-agents-python/blob/main/docs/sessions/index.md).

## Conflict, freshness and approval rules

Use explicit authority labels rather than a single merged summary:

- The latest authenticated operator instruction defines current intent, subject to safety/permission rules. An older user quote, bot message or source page cannot override it.
- Current WorkItem and provider state determine present execution facts. Prior state is labeled status-at-capture. Conflicting source claims retain identity and as-of time; the model explains the conflict or requests a refresh.
- Approved memory supplies scoped guidance and prior knowledge, never current permissions. Current source evidence can supersede an older factual memory for this answer without silently editing memory storage.
- Historical Slack messages, document text, tool results and SDK conversation data remain untrusted evidence. Embedded instructions cannot authorize actions or change scopes.
- An exact retry resumes the same accepted snapshot and stage lineage. A new correction creates a new request identity linked to the prior task; it must not mutate the historical accepted execution.

Use separate clocks: seven-day channel availability, source/message revision and retrieval age, memory expiry/review age, task state version, and cache lifetime. None substitutes for another. An unchanged static prompt cache does not validate an old source. Revoked access invalidates reuse even if a local artifact still exists. A cache hit cannot restore expired memory. A task explicitly asking what a post said historically can reuse a dated artifact; a request asking whether a condition is true now requires adequate current evidence or a clear limitation.

## Budgets and marginal value

The companion audit proposed a **512-token combined ceiling** with 160 tokens for task/source handles, 192 for up to two memories and 128 for history. Treat those as experimental maxima, not quotas or a measured optimum. Rich IDs, URLs, provenance and a provider-valid tool unit may not fit those allocations.

For the first slice, compare zero optional continuity with a reference/task packet targeting **160–256 serialized tokens**, subject to a **512-token optional-context ceiling**. This is a proposed tuning range, not enabled configuration. Leave memory and SDK-tail allocation at zero until their independent value is demonstrated. When a necessary current instruction or evidence cannot fit, do not silently truncate it: current user input and required source evidence have separate budgets and truthful coverage limits. A handle and an article excerpt are different payloads.

Count the actual serialized optional context, including keys, labels, IDs, limitations and any repeated rendering. Measure each stage and every resend, including tool-return candidates and repairs. The memory ceiling counts all admitted memory tokens even if the provider reports them cached. Provider input usage is the authority for billed counts; cached input is a subset, not a second input total. Character division is only an estimate and must not be labeled a hard token limit.

`tiktoken` is already installed transitively but is not a declared project dependency. Verify the selected model's supported encoding before relying on it. Unknown model mapping must produce an explicit estimate/unsupported counter state; do not silently claim another model's tokenizer is exact. Before implementing a hard token gate, either declare and validate that dependency or use an approved supported counter. Until then, a separate strict serialized-byte bound plus a labeled estimate can bound payload size, but does not prove a 512-token maximum.

Evaluate marginal benefit with paired identical fixtures: correct source/task identity, latest-instruction compliance, missed/irrelevant facts, retrieval calls avoided, total input and uncached/write/read usage, and latency. A candidate ten-percent incremental cost target from the audit is a tuning guard, not an acceptance law: cached baselines and write charges can distort a percentage. Prefer the smallest packet that fixes an actual continuity failure without irrelevant-context regressions. No extra LLM compression call is justified for this pilot. If memory adds no useful information, retain zero memory.

## Cache reuse with small memory

Keep existing stable instruction/schema/tool profiles reusable; append bounded dynamic request, selected continuity and evidence after that stable prefix. The current `agent_with_stable_prompt_cache_key()` excludes Slack/session IDs and request text while including privacy scope, model, instruction digest, tool schemas, output schema and data-handling profile. Preserve this behavior. Do not put per-request timestamps, memory facts, source revisions or channel identities into the static instruction text merely to make them available to the model.

The ordinary supported-model path currently leaves the reviewed 30-minute default in place. The private-context path sets `store=false`, disables sensitive tracing, and uses explicit cache mode with no breakpoints. **Preserve that privacy distinction.** Do not enable implicit caching or longer retention for private context to improve a chart. A compatible profile key is a routing/grouping hint, not an authorization token or a promise of cached contents.

Official guidance confirms that stable prefixes and eligible matching endpoints determine reuse; compaction can disrupt a previously reusable prefix. Explicit static-prefix breakpoints are a possible future improvement, but require payload compatibility and privacy review before use. Explicit-only mode without breakpoints does not cache. [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

Accordingly, first retain existing cache policy and measure cross-run behavior. The parent audit reports substantial cached input across a three-request preflight with sessions/memory absent; that demonstrates caching can occur without a continuity feature, not that a particular prefix hit across sessions. Add content-free receipts for profile-key hash, static-prefix digest, model/data profile, cache mode, component sizes and provider cached/write/total input by request. Compare two separate executions with compatible static profiles and different dynamic tails. Report an observed cross-run improvement only if per-request receipts support it; the provider may not identify the originating cache entry, so causal attribution can remain an inference.

There are two independent reuse decisions:

| Reuse | Eligibility/invalidation | Cost and fidelity boundary |
| --- | --- | --- |
| Provider prompt cache | Compatible exact prefix/endpoint, model/profile and permitted cache policy; changed static instructions/tools/schema produce a new profile | Reduces repeated computation when a hit occurs. Does not add knowledge, establish source freshness or permit wider disclosure. |
| Local source/task artifact | Exact identity/revision, scope, provenance, coverage and task-appropriate freshness | Can avoid repeated retrieval while retaining an explicit historical limit. Does not prove a provider cache hit. |

Do not keep irrelevant transcript tokens to improve hit rate. After compaction, accept a possible cache miss if total useful-context cost and correctness improve. Compare total effective cost, including cache-write/read usage and all model turns; do not optimize only cached-token percentage. Optional static-prefix breakpoint or lifetime changes belong in a separate measured follow-up, with a zero-private-content breakpoint boundary and no relaxation of the present private profile unless explicitly reviewed and authorized.

## Implementation sequence and compatibility

| Priority / proposed change | Canonical owners | Compatibility and completion requirement |
| --- | --- | --- |
| P1: reference-only packet and identity resolver | KS `business_agent_run_store.py`, `business_agents_bridge.py`, fixed channel adapter; KBA new schema/builder under `schemas/` and `runtime/` | Reuse exact attempt/artifact lineage. Optional versioned fields default absent. Old producers remain accepted as limited legacy context; unsupported schema versions fail visibly. No broad state import. |
| P1: one Orchestrator-to-answer projection | `orchestrator/preflight_context.py`, `entrypoints/cli_impl.py`, `runtime/request.py`, `orchestration/stages.py`, existing `workflow_runner.py`/`langgraph_workflow.py`, Chief child input | Current preflight compactor has a channel-only allowlist: explicitly preserve the new optional field. Match direct and graph serialization; avoid new business logic in top-level facades. |
| P1: actual admission/usage receipts | SDK/model boundary, durable stage metadata, execution telemetry; diagnosis collector when needed | Separate generated, admitted, selected and used evidence. Hashes/counts in logs, not raw private text. Unknown counters remain unknown. No database migration unless the existing metadata cannot express the receipt. |
| P1: relevant profile projection and useful-action contract | Existing `operator_context.md`/approved source document, general-answer and Chief prompt/builders, shared continuity projection | Reuse approved facts; carry selected profile into actual final synthesis. No wholesale operator-file injection, unsupported current project facts or public schema change. Test compact and full paths separately. |
| P2: scoped approved memory | `tools/memory_tool.py`, `memory.py`, `storage/sqlite_store.py`, context-pack hydration, memory schema metadata validator | Destination scope before ranking, no permission widening through legacy arguments, no readiness-gate changes. Legacy unscoped memory omitted for pilot rather than reclassified globally. |
| P2: conditional bounded SDK tail | `sdk_sessions.py`, `sdk.py`, actual ask/WorkItem/Slack-action session call sites | Preserve identity behavior and complete tool units; explicitly gate pilot behavior. Do not globally change current 6/24-item semantics. |
| P2: cache optimization, only after measurements | Shared `sdk.py` cache/settings/model boundary | Keep current policy first. Any explicit breakpoint requires verified payload support, privacy/profile separation and cost comparison. No silent retention changes. |

Update registry/tool-policy/prompt metadata only where a model-visible tool or instruction contract changes. Pure private projection helpers do not require a new agent. Add instructions to existing markdown surfaces explaining current-versus-historical authority, source evidence levels and semantic selection. Use schema constraints and typed context requirements; do not introduce trigger phrases for “continue,” “that article” or a particular channel.

Graph checkpoint compatibility needs explicit treatment: old checkpoints have no continuity field; exact resume must preserve the accepted snapshot and selected packet. A new memory version must not silently alter a resumed decision's previously frozen input. Rehydration for a new decision is a new admitted revision with recorded provenance and current eligibility. Sequential stages can share the same packet by reference; do not repeatedly append it to session storage or context packs.

## Backend test matrix

Use synthetic records, the real entrypoint/bridge schemas and scripted/native SDK boundaries. Parameterize both mention forms and direct/native graph routes where applicable. Scripted model outputs prove wiring and rejection behavior, not natural semantic selection.

| Case | Required observable result |
| --- | --- |
| Self-contained new root | Empty optional continuity, no memory/history read or Slack acquisition caused by continuity; normal Orchestrator first. |
| In-thread follow-up / restart | Exact prior attempt and selected source survive restart and loss of ephemeral context file; current request unchanged. |
| New root referring to earlier channel work | Query/read admits bounded candidates, model selects exact historical task/source, resolver returns matching lineage. No channel-global last-task fallback. |
| Two similar tasks / repeated ordinal | Both identities remain distinguishable; invalid model choice rejected; Python never substitutes newest source. |
| Seven-day edge / timezone / future post | Boundaries and fixed snapshot enforced; one second outside excluded; later posts absent; channel B denied. |
| Stale handle / edited or deleted post | Revision mismatch, unavailable source or revoked access yields explicit limitation/reacquisition under current scope, never silent equivalence. |
| Link/title versus article evidence | Post-only answer can cite retained link; substantive article claim requires the article-read receipt/coverage. |
| Correction contradicting old request/memory | Latest instruction wins for intent; forbidden action remains blocked; contradiction retained when factual. |
| WorkItem status changed / prior approval | Current state projected; historical approval cannot authorize a new effect or recipient. |
| Approved memory positive and negative | One relevant scoped fact admitted; unrelated, expired, superseded, pending, sensitive-unapproved and unscoped records excluded before top-k. |
| Duplicate fact in several layers | One rendered body with linked identities; no duplicate through preflight memo, pack and session. |
| Oversized/multilingual packet and long URLs | Complete refs/limitations preserved or whole entry omitted; sizes reported accurately; unsupported tokenizer never labeled exact. |
| SDK dependency group exceeds budget | Entire group retained within limit or omitted; no orphan call/output/reasoning items; current input untouched. |
| Exact resume versus new correction | Resume input stays frozen; conflicting snapshot rejected; new request obtains its own identity and eligibility checks. |
| CoS child and answer repair | Selected packet reaches actual owner; same evidence retained for one tool-free repair; no repeated reads or side effects. |
| Old/new transport and checkpoint | Missing optional fields safe; unknown versions explicit; no phantom continuity evidence. |
| Cache profile separation | Equivalent static inputs stable across threads; changed instructions/tools/schema/model/profile alter hash; dynamic memory doesn't alter static prefix; private controls unchanged. |
| Cache/continuity accounting | Cached tokens still count toward memory admission; cache reads/writes/total and repeated stages remain distinct; no double-counted copied telemetry. |
| Tailored next steps / profile missing or irrelevant | Specific feasible action and expected artifact/decision grounded in admitted facts; no invented project state; current instructions beat profile defaults. |

Start with focused new projection/identity tests, then existing memory, session, lazy-channel and CoS/graph backend tests, and cross-repo bridge/mention tests. Repository wrappers and pinned interpreters remain authoritative. Re-run only impacted broader suites after these pass. No production validation or test counts are claimed by this plan.

## Bounded live verification plan

The operator subsequently authorized implementation after graph verification and **up to five additional Slack tests**. The parent implementation task owns that authorization and remaining budget; this design worker performs none. After offline gates and the prerequisite graph result, use at most four of those requests for a source task with tailored actions, its genuine follow-up, a new-root reference to the same task, and a self-contained negative control. Reserve the fifth for a needed distinguishing case or repair, without automatic consumption. Choose entrypoint/backend combinations not already established by equivalent evidence. Count all implementation workers against the one shared remaining budget.

Before starting, state the exact model/configuration, total model-request and spend stop conditions, permitted read tools, source/reference targets and logging path under the live-test skills. No memory capture, external writes or broad-channel rollout is part of this plan. If a separately approved memory test is needed, substitute a controlled pre-existing scoped approved fact; do not fabricate or silently save one.

For each request, inspect actual admitted packet, model selection, matching source receipt, latest-request compliance, selected owner, visible citations and Slack delivery. Compare total token/cost/latency against a matched reference-only/off baseline where available, and distinguish within-run caching from cross-run observations. Stop on scope escape, invalid citation, wrong task/source, budget exhaustion or uncertain write state. Do not consume a retry without the stated budget covering it.

A useful follow-up and new-root reuse, together with a clean unrelated control and tailored-action evidence, establish the pilot behavior. The operator's broader-channel rollout is conditional on the implemented pilot working. If that condition is met, enable the same per-active-channel contract across authorized channels; do not pool their histories, import private profile facts across destinations, widen lookback or assume one channel's evidence proves every channel's source semantics. Record the pilot acceptance decision and verify isolation/transport configuration before that rollout. Longer windows, natural approved-memory relevance and SDK-tail benefit remain separate evidence. If reference continuity works without SDK history, leave history off.

## Complete package review

This review evaluated only capabilities relevant to this bounded local design. No package was installed, upgraded or removed. Package versions below were read from the repository interpreter: `openai-agents 0.22.2`, `pydantic 2.12.5`, `tiktoken 0.12.0`, `langgraph 1.2.11`, `langgraph-checkpoint-sqlite 3.1.1`. Installed presence does not equal a directly declared dependency.

| Package/capability evaluated | Purpose | Decision | Rationale, alternative and verification boundary |
| --- | --- | --- | --- |
| Python standard library + existing SQLite stores | Exact reference lookup, local persistence, bounded retrieval | **Retained; preferred first slice** | Existing attempts, WorkItems and execution stages already own state. Avoid another service/index for exact IDs. Reuse repository store wrappers rather than introducing cross-repo SQL coupling. |
| SQLite FTS5 | Faster lexical candidate retrieval at larger corpus sizes | **Deferred for new continuity indexing** | Existing discovery FTS remains retained. FTS5 offers ranked lexical search, but a new index needs synchronization and eligibility filtering. Benchmark local query latency/recall first; no measured bottleneck or benefit here. [SQLite documentation](https://www.sqlite.org/fts5.html). |
| Pydantic | Typed bounded projection and validation | **Retained** | Fits existing contracts, field bounds and structured serialization; hand-built dicts would weaken consistency. No new framework or version change needed. [Official serialization documentation](https://github.com/pydantic/pydantic/blob/main/docs/concepts/serialization.md). |
| OpenAI Agents SDK | Existing agents, optional local sessions and input boundaries | **Retained** | Extend existing session adapter only if tail value is proven. Replacing the runtime or using server-managed continuation adds migration/privacy work with no demonstrated benefit. Current source exposes required hooks; no SDK upgrade proposed. [Official session documentation](https://github.com/openai/openai-agents-python/blob/main/docs/sessions/index.md). |
| `tiktoken` | Local serialized-text token measurement | **Recommended conditionally for a hard token gate; no dependency change made** | Already installed, not directly declared. Validate model encoding and declare if used in production. Character heuristics are cheaper but not an exact token guarantee. Unknown names can fail mapping; do not substitute silently. [Official implementation](https://github.com/openai/tiktoken/blob/main/tiktoken/model.py). |
| `langgraph` and `langgraph-checkpoint-sqlite` | Existing optional execution/checkpoint backend | **Retained, no expanded memory role** | Carry identical accepted continuity data through existing nodes/checkpoints. They do not replace WorkItems, source manifests or approved memory; this plan proposes no new graph API. |
| No added memory/retrieval dependency | Composition of existing schemas, stores and tools | **Recommended** | Exact identity and safe admission are the demonstrated gaps. A vector database, hosted memory service or embedding pipeline is not justified by the inspected evidence. No named vector package was evaluated or benchmarked. |

“Performance” here means lower total model input/repeated acquisition and acceptable local lookup/serialization latency. “Fidelity” means correct task/source identity, preserved latest instructions, accurate evidence levels and explicit limitations. Documented capabilities support feasibility; expected savings and answer improvements still need paired measurement. Public package benchmarks would not establish the pilot's relevance, privacy or continuity correctness.

## Decisions and remaining uncertainty

Proceed with the reference-only slice using no new dependency, no automatic memory capture and no global session enablement. Keep the current single-channel rollout boundary, existing cache privacy profiles, Orchestrator-first semantics and source/approval gates. This is a bounded implementation recommendation; it is not implemented by writing this plan.

The implementation must still verify response-message-to-attempt linkage, whether existing source manifests contain enough retained text/coverage for useful historical reuse, the selected model's tokenizer mapping, and a reviewed destination scope for memory. These are discriminating engineering checks, not reasons to ask the operator to design the system. No relevant eligible memory should simply result in no memory admission. If exact linkage is unavailable, retain current query/read behavior until it is added and tested.

A hard 512-token ceiling and 128-token SDK tail are not yet justified as optimal. Begin smaller, measure the marginal value, and omit layers that do not help. Cache hit rate alone cannot justify a larger prompt or weaker privacy. The existing blocked runs do not show that enabling memory would fix their separate acquisition/handoff/citation defects.

## Evidence and verification

### Implementation update: scoped pilot

The reference-only slice is now implemented behind an off-by-default channel
allowlist. Current authenticated workspace/channel/operator identity plus the
matching seven-day channel capability admit the pilot; saved task metadata and
model arguments cannot choose the memory destination. Other channels retain
their existing behavior. This does not enable cross-channel retrieval.

Eligible runs omit raw SDK session history and automatic legacy memory/metric
hydration. Canonical WorkItems, approvals, source snapshots and source-window
readers remain authoritative and available. The session database is preserved.
This prevents unaccepted answers and orphan prompts from silently becoming new
model input while retaining useful source reuse.

Optional continuity consists of canonical task handles (at most 2,048 serialized
bytes) and at most four approved operator facts. Their combined model-input packet
is capped at 6,144 bytes using the SDK's actual JSON serialization, including
Unicode escaping and spacing. Whole identities and URLs are omitted when too
large, never shortened. Source text has its own evidence budget. When a root run
creates task state after facts were retrieved, those model-seen facts take
precedence in this optional packet; the typed context pack still carries task
state. These are byte limits, not claimed exact token counts.

The Orchestrator may make up to two local `query_operator_context` calls when
approved preferences or a small source-checked profile projection would improve
the answer. The Chief receives the same admitted facts, can fill remaining slots,
and uses a shared fact ledger so subsequent queries cannot add an unforwardable
fifth fact. Each query/capture response remains bounded at 4,096 bytes. A fresh
root request inherits no unrelated last-task narrative. The earlier 512-token
proposal above is historical design guidance, superseded by these byte limits.

For an authenticated linked-thread reply, a separate intent-only context packet
can include up to six prior captured messages, with 1,200 characters per excerpt
and an 8,192-byte total ceiling. The configured ceiling may range from 1,024 to
32,768 bytes. If recent messages were not captured, the current host's thread root
is a labeled fallback; it does not establish full-thread coverage. Scoped channel
query/read tools can retrieve the known thread when needed. Canonical state and
approved source tools remain authoritative; historical assistant prose is not
source evidence or approval. Raw SDK session replay remains disabled for the
pilot. Larger ceilings are selective capacity, not quotas to fill.

The Orchestrator explicitly chooses `linked_work_item_relation`: continue the
existing task, create a branch, handle a new request, or clarify. The host then
validates current thread/task identity and execution gates. A model-owned
continuation may use the prior canonical exact-read plan when a short reply's
heuristic plan is incomplete; keywords alone do not decide task reuse. A branch
gets a separate task and parent reference, with only explicitly selected source
evidence; it does not inherit approvals, artifacts or execution history. A new
request has no inherited task execution state.
Profile facts are guidance, not evidence about active projects or permissions;
the current request takes precedence.

Memory capture is explicit and local. A current affirmative capture request,
typed capture ownership and validated current host scope are all required.
Only the Chief's capture capability can save one item, with exact read-back
verification and request-event idempotency. There is no automatic transcript or
agent-answer memory. Scoped retrieval filters approval, safety, expiry,
supersession and workspace/channel/operator/project scope before ranking.

Approved profile access also requires the current operator ID in the host's
explicit profile-subject allowlist. A different operator cannot enable the
profile by requesting it or replaying a seeded profile fact.

No package was added or upgraded. Existing SQLite storage, Pydantic schemas,
SDK tool/session boundaries and static prompt-cache policy were retained.
Cache reuse remains a provider optimization; it never authorizes a result or
substitutes for successful evidence reads. The final integrated offline admission
gate passed 235 tests. Earlier affected CLI/context/graph and Chief/workflow gates
passed 782 and 747 tests, and the final Chief integration gate passed 283. These
suites overlap; their counts must not be added as unique coverage. Scripted
native SDK tests cover scoped capture/read-back, optional query and no-query
behavior, one-copy fact forwarding, actual serialized budgets, profile-subject
isolation, raw-history omission, unchanged ordinary approval checkpoints, and a
completed native graph capture checkpoint. No backend inference was used for
these checks. Live acceptance is recorded separately; workspace-wide rollout
remains disabled.

Primary local surfaces inspected: [channel tool boundary](../src/keystone_agents/tools/slack_channel_context_tools.py), [completed-read evidence](../src/keystone_agents/runtime/channel_answer_evidence.py), [preflight transport](../src/keystone_agents/orchestrator/preflight_context.py), [accepted snapshots](../src/keystone_agents/runtime/context_snapshot.py), [durable execution](../src/keystone_agents/runtime/durable_execution.py), [session policy](../src/keystone_agents/sdk_sessions.py), [SDK adapter and cache policy](../src/keystone_agents/sdk.py), [memory schema](../src/keystone_agents/schemas/memory.py), [memory tool](../src/keystone_agents/tools/memory_tool.py), [memory retrieval store](../src/keystone_agents/storage/sqlite_store.py), [context-pack hydration](../src/keystone_agents/work_items.py), [Slack attempt records](../../keystone-slack/kni_integrations/business_agent_run_store.py), and [Slack bridge](../../keystone-slack/kni_integrations/business_agents_bridge.py).

The plan also reviewed repository guides, relevant contract-change/run-diagnosis skills, the two linked context audits, current installed package metadata and official documentation for the evaluated capabilities. Current source was checked directly; historical run/count/token observations are attributed to the companion audit rather than represented as a fresh database audit. Tests, runtime fingerprints, Slack UI and live provider behavior were not rerun here. Report structure and local document links were checked; implementation acceptance remains the matrix and later bounded live plan above.
