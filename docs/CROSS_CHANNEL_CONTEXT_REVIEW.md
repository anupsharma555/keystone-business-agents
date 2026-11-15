# Cross-channel context review and implementation backlog

**November 15 update:** workspace enrollment is now authorized by the operator;
the [workspace rollout record](WORKSPACE_CONTINUITY_ROLLOUT.md) owns its current
configuration, channel isolation and verification. Earlier pilot-only decisions
below retain their original dates and proof limits.

Review date: 2026-11-14. Scope: local source inspection, recorded-run diagnosis, and offline tests in Keystone Business Agents (KBA) and the adjacent Keystone Slack runtime (KS). This document authorizes no provider calls, permission changes, new services, or external issue creation. Examples are synthetic.

## Summary

Provide a **rolling seven-day window of the active channel that agents can query when relevant**, not seven days of messages in every prompt and not a pooled all-channel window available to every run. Cross-channel retrieval requires an explicit request or validated context requirement plus access authorization. The current bridge can fetch bounded channel history, serialize selected thread context, and recover an original task from the local attempt journal. The active-channel pilot now exposes a bounded, model-callable query/read contract during Orchestrator preflight; it is not enabled across all channels. Automatic history capture, text truncation, phrase-based context admission, specialized feed adapters, and execution receipts currently have different limits and meanings.

The immediate source-summary failure has a specific cause: the model identified an article in supplied context, but canonical planning represented the task as general topic research; the completion contract accepted search evidence without an article read. That is distinct from an interrupted parent run failing to deliver a successful child's result. Local source-selection, read-contract, and lifecycle repairs are now present; their proof boundaries are recorded below. The scoped live source-summary cases establish delivery for those requests; they do not establish general seven-day continuity across all channels or run types.

Use a shared read-only context boundary with common identity, scope, provenance, freshness, completeness, and cost semantics. Keep channel-specific interpretation with the appropriate specialist. Prefer existing Python, Pydantic, provider adapters, and SQLite facilities; no new package or service is required by this proposal.

## Pilot live validation

A supervised Slack test on 2026-11-14 exercised the new lazy capability. The model received both tools and used them, but returned clarification instead of an article summary. Its completed channel query scanned the correct day and filtered out the feed post; another query incorrectly restricted retrieval to the triggering request thread. The run ended in about 18 seconds after four model requests, two adapter operations and one raw Slack API call, with no article read or synthesis. A separate read-only provider diagnostic with an empty text query and the same day bounds returned the feed and all four ordinal links. This demonstrates live acquisition availability, not successful model selection. Tool descriptions, Orchestrator instructions and its runtime skill were then clarified: text queries are exact case-insensitive substrings; dated/numbered references should query a narrow interval without an invented text filter; thread scope requires a known prior thread. KBA also rejects a triggering-thread timestamp at the request boundary before adapter invocation. The follow-up no-network gate passed 174 tests, including six new scoped-tool regressions.

The second and final authorized Slack test passed in about 20 seconds. The live Orchestrator recovered from one rejected query, acquired the correct dated feed, read its exact message, selected ordinal three, extracted the selected article and synthesized the brief itself. It used four preflight model requests plus one final synthesis request, two adapter operations and two raw Slack API calls. No broad search or specialist handoff occurred. The visible Slack reply contained the correct source URL and explicitly limited its claims to the first 4,000 characters of extracted text (10,258 available). This was an Orchestrator direct answer; the graph and Chief of Staff combinations remain verified through mocked backend tests only.

Both approved tests were used, with no automatic replay. Total model requests were nine on the configured mini model. Local pricing estimates sum to about USD 0.0555; this is not an invoice amount. The immediate post-test Admin Usage snapshot showed the first four requests; its lag meant it had not yet reflected the second run. The local durable ledger and per-run SDK usage record all nine. One additional provider-only read diagnosed availability without a model call. The planned self-contained live control was replaced by the repaired-context test, so zero-history behavior remains backend-verified rather than newly live-verified. Scope remains the single opted-in channel; this result does not justify rollout to all channels yet.

The operator subsequently tested a different ordinal from two days earlier. That run made five model requests but zero adapter or Slack API calls: both counted query attempts failed the generic argument-bounds guard, then exhausted acquisition slots. The exact offending field was not retained, so an oversized result limit is a regression scenario, not a proven historical input. A read-only provider query confirmed the requested dated feed and ordinal-two link were available. The tool contract now exposes query/cursor length and result-count bounds directly in its SDK schema. Safe field-specific rejection feedback returns to the same model loop, labels acquisition as not performed and preserves admitted acquisition slots. Unexpected execution failures remain separate with unknown acquisition status. A 373-test no-network gate passed; the expanded cross-repo matrix includes argument repair, two-days-old ordinal selection and a selective seven-day search finding evidence one second inside the oldest boundary across both mention forms and direct/graph paths. This addresses the retrieval boundary rather than introducing natural-language phrase routing.

The single subsequently authorized Computer retest of the two-days-earlier request passed in 17.7 seconds. It made one valid dated query, one exact message read and two raw Slack API requests, with zero argument rejections. The Orchestrator selected ordinal two from the retained message links, extracted the selected source and answered directly. Three preflight model requests plus one final synthesis request ran on the configured mini model; their local cost estimate totals USD 0.02962, not an invoice amount. The visible Slack answer included the correct source link and an excerpt limitation. The article read covered 4,000 of 30,639 extracted characters. No broad search or specialist handoff was used. This is the second successful scoped live source-summary case; remaining live graph/Chief-of-Staff and general zero-history-control coverage is unchanged.

## CoS and graph pilot extension

The next CoS live diagnostic exposed a separate upstream boundary: a descriptor-bearing request entered a legacy Slack adapter that did not forward lazy channel context. The bridge now selects the canonical entrypoint by capability presence before legacy wording heuristics, preserving exact supported mutation, Calendar and automation compatibility paths. Cross-repo offline coverage now exercises the actual Slack command dispatcher, context-file transport, normal CLI entrypoint, real CoS child script and native scripted SDK. Earlier CLI-only coverage did not establish this upstream seam.

The first post-restart retest reached the repaired canonical path, made a valid channel query and exact read, and correctly identified the dated ordinal. The model chose Orchestrator for the bounded internal note rather than CoS, interpreting the prohibition on other agents as avoiding a handoff. The final general-answer phase then dropped the completed reads and received only the request and format constraints. Its source-needed block was therefore justified for its actual input. This is an evidence handoff defect, distinct from provider acquisition and distinct from the prior legacy dispatch defect.

The general-answer handoff now projects only successful reads matching the immutable request snapshot and channel, with source identity, coverage and historical-only authority. It admits at most two messages under a combined 24,000-character message budget, never promotes query previews to read evidence, and validates visible citations against links actually retained. The post-only lane performs no reacquisition. Public-search and extracted-article lanes retain their own evidence contracts. Native scripted SDK controls exercise both a legitimate Orchestrator choice and the CoS child path; an invented citation remains blocked. A prompt clarification distinguishes the requested responding owner from additional delegation without forcing named-agent routing or bypassing preflight.

The operating model defaults, seven configured runtime profiles and twelve registered builders have moved to the operator-requested Luna model. Explicit overrides and historical mini canary benchmarks remain separate. Three bounded backend live diagnostics were used: tool/structured-output compatibility passed; an initial CoS diagnostic lost its answer after an unsuitable self-reported-mode assertion; the corrected retained-result CoS diagnostic passed. The latter used one request and returned a preview-only note with no tool or handoff. This is narrowed compatibility evidence, not default Slack behavior. The combined no-network gate passed 688 tests before the final mixed-evidence isolation adjustment; affected checks are rerun below as evidence develops.

The second post-repair Slack test retained the correct historical post in the final answer and no longer reported missing context, but failed the visible-citation invariant. Its original candidate was not retained, so missing citation versus an unsupported URL cannot be reconstructed. The answer path now records content-free initial/final citation verdicts and permits exactly one tool-free model correction with unchanged evidence. Python gives the failed invariant rather than choosing the source. Persistent invalid citations, a failed correction, or exhausted model budget remain blocked; no completed provider read is repeated. Native SDK regressions exercise missing and unsupported citations, successful and unsuccessful correction, provider-free failures and budget admission. Combined usage preserves both answer attempts, including unknown usage rather than silently zeroing a failed attempt.

The following post-only readiness test completed in 30 seconds: three preflight requests, one final answer, one query, one exact read, and two Slack API calls. It retained the correct source link and explicitly stated that the article had not been read. The initial citation verdict passed, so the new repair was not invoked. The actual owner remained Orchestrator despite the CoS mention; this establishes the mention entrypoint and evidence handoff, not a Chief specialist execution. The local model-cost estimate was USD 0.004963, not invoice billing. The operator found its three next steps too generic; the separate compact-continuity plan addresses missing final-stage operator context and action quality.

The first native graph diagnostic created a durable WorkItem and nine graph checkpoints, but no Research artifact. Its three preflight requests plus five Research requests spent the eight-request ceiling reading successive bounded source windows; another model response for synthesis was rejected before the provider call. Saved window identities differ, so identical rereads are not established. A separate Slack worker restart interrupted delivery while the child continued; local receipts later established the blocked outcome. The restart initiator is unknown. The manager now checks recorded active Business Agents runs before stopping a live worker, while allowing dead-worker recovery; it is a best-effort guard, not protection against direct service commands or every admission race.

The source-reading repair exposes remaining capacity, preserves one final model response, and removes scoped continuation tools only after already-advertised calls remain executable. The existing 8,000-character window limit is unchanged; escaped text is packed efficiently under its existing serialized cap. Native SDK graph tests cover tight budgets, multiple windows, parallel reads and Unicode, retain a final Research artifact/checkpoint, and preserve truthful partial-coverage limits. Live graph completion/resume remains pending; the prior interrupted result is not replayed automatically.

The first explicit resume exposed an independent identity defect: the current request named the original blocked WorkItem, but a replacement was created. The resulting partial research answer disclosed the mismatch. Current explicit resume IDs now bind the existing same-channel, same-thread row before preflight, override stale adapter links, and cannot fall through to creation. A bounded selected-source retry can resolve only its prior Research execution-failure blocker after accepted synthesis and artifact construction. Ambiguous IDs and mismatched scope stop before model or provider work. The affected backend gate passed 1,192 tests, including the public Slack dispatcher through native graph resume and saved-state verification; those scripted tests do not prove live recovery.

The subsequent live resume stopped during Orchestrator decision validation, after three model requests and before Research execution. Its advertised route universe included the registered RAG specialist, while the validator's separate route list omitted it. Assessing that advertised alternative therefore failed the contract. This is a producer/validator mismatch, not evidence that the model invented an unsupported route. Contract alignment and corresponding regression controls are being completed before another live attempt. Its local model-cost estimate was USD 0.002327, not invoice billing. The original WorkItem remains unproved as successfully resumed.

The shared Slack payload formatter now converts Markdown bold, headings and links into Slack formatting while retaining code and existing Slack links. Offline formatting checks passed; the latest failed response contained no bold heading, so successful visible heading/link rendering is still pending. Failure telemetry now retains an exact entry-attempt link internally: a failed preflight may legitimately retain an older thread WorkItem as context, and diagnosis must not correlate that older successful child as the current failure. Historical records were not rewritten.

After route-universe alignment and bounded route repair, the combined affected gate passed 1,034 tests; independent acceptance passed 33 controls, including exact replay of both completed channel outputs and budget rejection before a fourth model request. The next live attempt passed preflight and retained the original WorkItem through native graph dispatch. It completed five local immutable-source window reads, but the graph had replaced the saved exact-source execution shape with a heuristic plan lacking its URL/read operation. The generic Research contract consequently demanded `search_web`, contrary to the selected-source-only request. Window reads correctly could not satisfy that broader discovery contract. The eight-request ceiling held; the result remained blocked with no new Research artifact. Its local model-cost estimate was approximately USD 0.023006, not invoice billing.

The Computer screenshot of that result **verified bold section headings** in Slack. A fresh successful source-link rendering check remains pending. The operator requested cleaner graph sections and relevant natural-language follow-ups instead of generic company/contact/outreach buttons. This is being addressed at the structured-result/presentation boundary, together with truthful distinction between an execution failure and information the operator actually needs to provide. The exact-source resume contract is being corrected before further live testing; memory implementation and cross-channel rollout remain conditional.

The [cross-session memory and state review](CROSS_SESSION_MEMORY_STATE_REVIEW.md) records the separate, small-budget continuity proposal. Its proposed token limits are not enabled runtime configuration. The live graph and actual CoS-owner acceptance decisions remain pending the bounded remaining Slack tests. Context scope remains the single pilot channel.

## Technical index

- [Observed implementation and evidence](#observed-implementation-and-evidence)
- [Target retrieval architecture](#target-retrieval-architecture)
- [Cross-channel consistency and specialist semantics](#cross-channel-consistency-and-specialist-semantics)
- [Context, execution state, memory, and sessions](#context-execution-state-memory-and-sessions)
- [Implementation-ready backlog](#implementation-ready-backlog)
- [Limits, privacy, and cost controls](#limits-privacy-and-cost-controls)
- [Immediate work and live-test decision](#immediate-work-and-live-test-decision)
- [Validation and source index](#validation-and-source-index)

## Observed implementation and evidence

The inspection found these distinct surfaces. These are code capabilities, not claims that every route exercised them successfully in production.

| Surface | Current ownership and behavior | Evidence boundary |
| --- | --- | --- |
| Root mention history | KS `slack_socket_mode.py` fetches channel history when the global history feature permits it. The reviewed implementation fetched up to five pages of 100 messages, with a seven-day lower bound on root requests. | Available lookback is capped; it is not an exhaustive seven-day archive. Replacing automatic root capture with selective query/read is proposed in issue 2. |
| Thread continuity | KS fetches replies, compacts roles/text, and recovers the exact original request from `business_agent_slack_runs` when needed. | The reviewed reply fetch took one page of 20; later replies and block-only content could disappear. |
| Handoff envelope | KS writes `keystone.slack.history_context.v1` or selected-message context to a local JSON artifact and invokes KBA with `--context-file`. | Writing a file or passing a flag proves transport only. It does not prove model-visible admission or use. |
| KBA model context | CLI projects state into Orchestrator context and smaller specialist context. Structured thread messages take precedence over fallback digest text. | Empty structured lists previously suppressed fallback; that defect is repaired locally. A phrase-based specialist admission gate and multiple text caps remain distinct risks. |
| Run state | KS stores append-only attempts and a latest thread projection; KBA owns its agent-run and WorkItem state. | A successful child does not establish a terminal parent or delivered Slack answer. |
| Slack repository tools | Chief of Staff has `search_slack_repo_context` and `read_slack_repo_context_file`. | These search allowed repository text files, not arbitrary Slack channel messages or private databases. |
| Feed context | RSS has a separately gated, announcements-channel live history fallback; Preprints has a narrow read-only discovery-store adapter. | Neither is a general cross-channel message query service. |
| Discovery archive | KS persists discovery candidates and an SQLite FTS index. | Discovery records are canonical for saved candidates; Slack presentation is a separate record. Do not scrape Slack to reconstruct already-stored canonical results. |

Recorded-run inspection showed the original article-summary request and identified source reached the research planning rationale. The executable plan nevertheless became a generic topic `research_brief`, with the raw request as `primary_target` and live search required. The child made two search calls, no extraction/read calls, and explicitly disclosed that its summary was based on a retrieved snippet. The parent remained started and the Slack projection running at inspection. This supports a plan/required-evidence defect and a separate lifecycle gap; it does not establish an article read or delivery.

Offline reproductions of the reviewed code established:

- A new root asking for “link 3 from yesterday” selected replies on its own new thread instead of earlier channel history.
- A reference to “article #10” was parsed as channel name `10`.
- With a valid digest, “summarize #3 from yesterday” lost specialist context; adding “above” admitted it.
- A synthetic seven-day digest with ten links per day exhausted the rendered text budget before the omission warning and message excerpts. A later KBA cap removed further identities.
- Structured thread conversion lost a URL present only in rich message blocks.

These observations are regression cases, not a claim that all remain unfixed after concurrent implementation. The backlog below records acceptance requirements independent of implementation status.

## Target retrieval architecture

Start each run with the raw request, exact current message/thread identity, small necessary continuation state, and a **context capability descriptor**: what sources can be queried, permitted channel scope, available time window, freshness, and tool budgets. Do not attach the channel transcript simply because a run started.

The Orchestrator decides whether context is needed. It may query a bounded candidate set, read selected messages/threads, then choose a specialist and pass the original request plus selected evidence references. A specialist may request additional scoped evidence when its admitted tools permit it. The response to a model-selected query must re-enter that model's reasoning loop before it selects an article, record, or other decision target.

Proposed operations below are design contracts, **not existing API names**:

1. **Describe context:** enumerate allowed source kinds, query fields, time/size bounds, channel capabilities, and known availability without returning message bodies.
2. **Query context:** accept bounded channel IDs, time interval, optional thread ID, text/semantic query, source kind, result limit, and continuation cursor. Return candidate identities and compact excerpts, not a seven-day dump.
3. **Read context:** accept exact candidate/message/thread references already within scope and return bounded content windows with provenance and completeness.

Use a minimal versioned schema rather than separate intent taxonomies for “yesterday,” “link 3,” or each channel. The common result needs:

- workspace/channel/message/thread identity; canonical source kind; stable candidate ID;
- message time plus retrieval time, revision/edit identity where available, and snapshot identity;
- role/author classification and whether content is a human instruction, bot result, source excerpt, or historical context;
- source URLs and visible ordinal labels tied to their parent post, not globally unique numbers;
- bounded text or a read handle, pagination cursor, available interval, and explicit truncation/completeness;
- access outcome, freshness/coverage limitations, and provider/cache receipt identity.

Python validates permission scope, identities, date bounds, budgets, and schema. The model decides relevance and substantive selection. Number extraction, filtering, ranking, and deduplication may be deterministic, but they must not silently select the user's intended article or replace missing evidence.

KS should own Slack acquisition and its stable read model. KBA should use named tools/adapters, not unrestricted SQL access to KS databases. The first implementation can be a versioned local adapter consistent with existing boundaries. A long-running shared service or broad mirror is unnecessary unless measured latency, contention, or availability requires it.

## Cross-channel consistency and specialist semantics

“Synchronized” means compatible context contracts and coherent references across entrypoints and channels; it does not mean copying every channel into every agent's context.

| Shared across channels | Remains specialist- or channel-specific |
| --- | --- |
| Scope enforcement, IDs, pagination, timestamps, freshness, redaction, budgets, receipts | How a research digest, operating discussion, opportunity list, or task status should be interpreted |
| Root/request/follow-up continuity and revision handling | Which evidence is sufficient for an article summary, opportunity decision, or draft |
| Candidate query/read contract and explicit ambiguity | Ranking criteria and domain relevance |
| Current instruction versus historical text distinction | Output shape, vocabulary, and approved communication workflow |
| Exact source-read and terminal-delivery proof | Permission to use an artifact externally; posting/sending remains separately gated |

Default queries should be scoped to the active channel/thread. Cross-channel retrieval requires an explicit named scope or a validated, authorized context need; the tool must enforce the permitted set. Bot membership alone must not be treated as permission to expose private material into another destination. Results retain their origin so the synthesis layer cannot quietly merge unrelated channel histories.

## Context, execution state, memory, and sessions

These stores have complementary roles in synthesis. Synchronization should mean consistent references, provenance, authoritative task updates, and relevant retrieval, not copying all information into one store or one prompt. In particular, do not promote raw channel logs into durable memory to compensate for retrieval gaps.

| Layer | Actual source of truth and purpose | What it must not become |
| --- | --- | --- |
| Task context | Bounded Slack/source evidence plus current request and selected references, passed through the accepted context envelope and typed specialist packs. | A permanent channel mirror or independent approval authority. |
| Durable business/task state | KBA SQLite WorkItems, source/artifact references, blockers, approvals, next action, and events. KS separately owns thread attempts/latest projection and delivery linkage. | A transcript-only reconstruction of execution or a substitute for provider verification. |
| Execution state | Durable execution journal and graph checkpoints track the accepted request, stage/resume position, tool outcomes, and terminal state. | A second competing business-state database or permission to replay completed effects. |
| Reusable memory | KBA `MemoryItem` records and `memory_index`: typed summaries/facts/feedback/style/dedup hints with source IDs, approval state, confidence, sensitivity, expiry, and supersession. | Live task status, current source proof, raw private conversations, or fresh authorization. |
| SDK session | Separate local Agents SDK conversation SQLite store, used only when the selected route/configuration creates a session. | Canonical WorkItem state, approved reusable memory, or a guaranteed transcript of all Slack history. |

**Verified implementation.** `work_items.build_context_pack_for_route()` derives specialist input from WorkItem state and, when supplied a store, hydrates bounded approved memory. `hydrate_context_pack_memory()` deliberately leaves readiness gates unchanged. Core direct workflow and graph call sites pass the store; a secondary Orchestrator summary builds a restricted pack without memory hydration, so that summary must not be described as the full specialist memory view. Chief of Staff's strategic-memory helper uses approved, prompt-safe records and bounded route-specific types. General agents can have a memory retrieval tool attached, but attachment depends on their builder and scoped tool policy. Route-only Orchestrator construction normally disables tools; a valid host-supplied pilot capability attaches only the two channel query/read tools. Its preflight cannot be assumed to query memory merely because the full Orchestrator builder can.

The memory store filters expired records and suppresses approved superseded records before ranking/limiting. An expired replacement does not resurrect the fact it superseded. This is useful existing behavior to preserve. However, `MemoryItem.source_ids` and open metadata do not by themselves guarantee source revision, retrieval time, channel access scope, or a current execution association; any richer provenance must be deliberately populated and validated. Memory creation is not evidence that a memory record was subsequently retrieved or used.

`runtime/context_snapshot.py` freezes an accepted context file into JSON for durable workflow execution and rejects a different request/snapshot on restoration. Graph execution uses this snapshot boundary and keeps WorkItems canonical. This protects reproducibility if a temporary file changes; it does not make the frozen content fresh indefinitely. A later user correction is a new accepted request, not a silent rewrite of a resumed execution's historical input.

**SDK sessions are implemented but conditional.** `sdk_sessions.py` resolves flags and explicit IDs, derives opaque Slack identity from team/channel/thread, and uses a separate configurable SQLite file. The generic recent-history limit is 24 items; Slack ask and WorkItem helpers normally request six because typed context already carries root/recent turns. These are item limits, not seven-day guarantees. Direct ask defaults vary by route: context agents, Chief of Staff, and Gmail use a different default from ordinary research asks, and unscoped CoS/context/Gmail calls default off. WorkItem sessions default according to live-SDK execution. Explicit options/environment can override defaults. A configured session is not proof of an available/loaded session or prior history; per-run metadata and observed history loading remain required.

**Duplication and staleness risks, distinguished from demonstrated failures.** The same evidence can appear in a handoff envelope, WorkItem pack, session tail, and memory excerpt. Source inspection establishes those parallel paths, not that every run repeats all of them. Shared Slack session identity does not include agent/route, so cross-route reuse within a thread is possible when enabled; this can be useful continuity but needs the same current-request/old-bot-prose boundaries as explicit context. Rehydrated memory during a graph resume can legitimately differ from an older frozen source snapshot. Report the difference and revalidate affected decisions rather than silently mixing versions. No blanket change to every agent's memory prompt is justified by these risks alone.

The inspected concrete memory-boundary gap was narrower: model-callable `retrieve_memory` exposed `approved_only`, passed it through the storage wrapper, and the store permits `False`. The local repair now enforces `True` at that model-facing boundary while retaining the argument for compatibility; its result reports the effective approved-only policy. CoS/context-pack callers already hardcode approved-only retrieval. Historical/unapproved inspection remains a separately scoped storage/operator concern. The regression invokes the actual decorated tool with `False` against pending memory and confirms that no record is returned or deleted. This closes that local tool-policy defect; it is not evidence that the reviewed live run consumed memory, or that every proposed eligibility/route/session acceptance case has passed.

The desired synthesis order is: authoritative current request and permitted task state define the objective; fresh selected context supplies evidence; approved relevant memory supplies reusable guidance and prior facts with provenance; session history supplies bounded conversational continuity. Conflicts stay visible. Current provider/WorkItem state wins for present execution facts; memory approval never bypasses a current write gate. The model synthesizes these complementary inputs while Python enforces identity, safety, and execution boundaries.

## Implementation-ready backlog

Priorities: P1 blocks a trustworthy next source-summary proof or the requested selective-retrieval design; P2 is required before broad cross-channel rollout. Owners identify implementation boundaries, not external assignments. Status is local and provisional while fixes are integrated.

### 1. P1 — Preserve selected-source intent through canonical planning

**Problem:** public-research projection can erase the model's exact selected source and turn a read request into topic search.

**Implement:** extend/reuse typed Orchestrator and plan fields to represent model-selected source identity and required acquisition. Validate the identity against model-visible candidates. Preserve `source_summary`, exact URL, read-only scope, and source evidence requirements through projection. Require matching extraction/read evidence, not merely `search_web` completion. Avoid requiring unnecessary additional windows when the initial extraction is sufficient.

**Owners:** KBA `agents/orchestrator.py`, `schemas/orchestrator.py`, `agents/business_research_analyst.py`, decision validation and source tools.

**Acceptance:** synthetic selected URL survives model output → plan → attachment → execution; unrelated URL is rejected; search-only success cannot pass selected-source completion; inaccessible article produces a truthful limitation; cached evidence includes exact identity and freshness. No Python-selected substantive URL. **Status:** local source and tests now preserve a model-selected, model-visible URL as an exact read-only `source_summary` plan. The research agent attaches target-bound extraction/window tools and requires source-read evidence rather than search-only completion. Backend integration checks are described under issue 9; no live source-summary success is certified here.

### 2. P1 — Add lazy, shared channel query/read capability

**Problem:** automatic root lookback spends API and prompt budget even for unrelated self-contained requests, while agents lack a generic way to request missing channel evidence.

**Implement:** add the capability descriptor and bounded query/read contract above. Make seven days the available rolling window by default, with narrower model-selected requests. Preserve necessary small current-thread continuity. Use named KS adapters and existing registry/tool-policy contracts. Reuse existing feed/discovery adapters through the common result envelope where appropriate.

**Acceptance:** a self-contained request makes zero history calls and receives no unrelated messages; each default capability descriptor exposes only the active channel's seven-day window; a dated ordinal request queries that channel's relevant interval; ambiguity yields candidates/clarification; requesting more context makes a bounded second read. A run in channel A cannot query channel B merely because the bot can access B. Explicit authorized cross-channel requests retain separate origin labels and enforced scope. Denied channels and older out-of-window requests fail explicitly. Verify model-called selection with actual runtime tools, not only scripted responses. **Status:** implemented as an explicit active-channel pilot, with backend verification. The opt-in `KNI_BUSINESS_AGENTS_LAZY_CONTEXT_CHANNEL_IDS` supplies a request-bound seven-day capability instead of eager root/thread history. The model can make at most two narrow queries and two exact-message reads in its preflight SDK loop. The KS adapter enforces the allowlist, snapshot and pagination; KBA admits source URLs only after a completed read. Other channels retain their existing behavior. Usage distinguishes adapter operations from raw Slack API pagination and marks incomplete transport accounting explicitly. Two supervised direct-answer Slack cases now verify live selection and delivery for yesterday and two days earlier, after diagnostic failures and shared contract repairs; broader live consistency remains unproven.

### 3. P1 — Make context admission depend on typed need and preserve structure

**Problem:** acquired context can be discarded by phrase gates or serialized text caps. Rich block evidence can be absent from the preferred structured transcript.

**Implement:** admit selected evidence through the validated context dependency in the plan. Use one normalized message representation for text, blocks, attachments, and source references. Keep structured identities and coverage metadata separate from excerpt budgets. Retain compatibility with old envelopes while explicitly labeling their limitations.

**Acceptance:** “summarize #3 from yesterday,” a URL-only clarification, a rich-block article, and ordinary paraphrases deliver the same selected identity to the intended specialist. Empty structured data cannot suppress usable fallback. Unrelated history remains excluded. **Status:** pilot implementation and backend verification complete. Completed reads retain source links/ordinals, message identity, revision, historical authority, retrieval time and coverage separately from excerpt caps. Read evidence reaches the specialist and persists with WorkItem state through direct/graph preparation. A fresh request clears stale evidence; an exact resume retains the accepted snapshot. Necessary small root/current-thread context remains separate. Message text is bounded (8,000 characters at KBA); oversized-message continuation is still follow-up, and truncation is disclosed.

### 4. P1 — Correct history scope and temporal/ordinal selection

**Problem:** root/thread inference and hash parsing can retrieve the wrong conversation; repeated ordinal numbers and “yesterday” are ambiguous without date/post scope.

**Implement:** use actual thread identity to distinguish a root from a reply. Separate channel mentions from numeric list labels. Represent requested date interval and timezone explicitly; preserve a stable request-time upper bound. Treat ordinal as relative to a post. Let the model resolve candidates or ask for clarification rather than silently choosing the newest match.

**Acceptance:** root “link 3” queries prior channel posts, `#10` never becomes a channel name, midnight/DST boundaries use the configured timezone, multiple matching posts remain distinguishable, and messages after the request are not silently included in its snapshot. **Status:** regression cases identified; verify current fixes before the next source-reference live test.

### 5. P1 — Close interrupted attempts without replay or duplicate delivery

**Problem:** successful child evidence can coexist with a started parent and indefinitely running Slack status.

**Implement:** reconcile orphaned queued/running attempts at worker startup; record outcome uncertainty without replaying providers or models. Bound queue/result waiting. Suppress late publication after timeout. Update the exact existing status message with idempotent retry when authorized; preserve completed projections.

**Acceptance:** restart during execution, timeout followed by late success, failed status update, repeated startup, and old orphan under a newer completed result cause no replay or duplicate message. End-to-end evidence separately identifies child result, parent terminal state, and Slack delivery. **Status:** the lifecycle workstream reports 304 passing offline bridge/lifecycle tests, one guarded worker reload, and the previously stuck projection reconciled to blocked with its existing Slack status updated. This review independently inspected the source, not that external result. Startup reconciliation marks interrupted queued/running local records blocked with execution outcome unconfirmed and performs no automatic replay. Slack interruption updates are restricted to exact existing status messages for recent requests within seven days; older local records can be reconciled without updating old Slack messages. Finalization is bounded by the configured command timeout plus 30 seconds (180 + 30 seconds at the code default). Late completion suppression and retryable exact-message updates remain part of the regression contract. This proves neither a successful article read nor universal delivery recovery.

### 6. P2 — Preserve root plus recent thread state over long conversations

**Problem:** one early reply page and successive tail caps can exclude the latest clarification or correction.

**Implement:** bounded pagination with root identity plus latest relevant replies; stable cursors; deduplication; explicit partial coverage. Retain journal root recovery, while later authoritative user revisions can modify the active task. Do not treat old approvals or bot prose as new authority.

**Acceptance:** a 25-plus-message thread keeps the original task and latest URL/correction; root unavailable from Slack but present in journal is labeled as local evidence; duplicate events do not duplicate state; changed task direction is preserved without recursive follow-up envelopes. **Status:** proposed follow-up.

### 7. P2 — Define cache, freshness, retention, and permission lifecycle

**Problem:** bounded context files are persisted, but a shared retention/freshness policy was not established by this review. A local snapshot may outlive access or source changes.

**Implement:** a read-only cache policy keyed by workspace/channel/message/revision and retrieval scope; seven-day query availability does not imply indefinite body retention. Record coverage and expiry. Handle edits, deletions, access revocation, and refresh failures explicitly. Resolve the intended local-artifact retention policy before implementing cleanup; follow repository deletion rules for any bulk cleanup.

**Acceptance:** stale/edited/deleted messages cannot be represented as verified current content; loss of access blocks new disclosure; a cache hit identifies retrieval and source times; permission boundaries survive cache reuse; redaction and sensitive-content exclusions are tested with synthetic fixtures. **Status:** proposed follow-up; no migration or cleanup authorized by this report.

### 8. P2 — Unify evidence, cost, and completeness telemetry

**Problem:** receipt presence, message transport, model visibility, source reading, and Slack delivery can be conflated. Text truncation can remove its own warning.

**Implement:** separate counters for query/read attempts, cache hits, pages/messages returned, bytes/tokens admitted, model-selected sources, successful article reads, parent/child outcomes, and delivery. Carry omission/error metadata outside truncated content. Correlate by exact attempt and source IDs without logging secrets or unnecessary bodies.

**Acceptance:** busy-channel tests exceed each cap and preserve accurate warnings; optional retrieval failure does not become “no messages”; model-visible source IDs can be audited; a source read cannot be inferred from a search receipt; latency/cost measurements include acquisition and prompt overhead. **Status:** proposed follow-up, building on existing run journals and tool receipts.

### 9. P2 — Prove consistent behavior across entrypoints and specialist routes

**Problem:** app mentions, replies, message actions, WorkItems, and feed tools currently expose different context shapes and policies.

**Implement:** one cross-repo contract fixture suite and an explicit mention/engine matrix. Exercise both `@KNI` and `@KNI CoS` through non-graph/direct execution and graph execution. Cover general operational discussion, research links, discovery candidates, and task continuation. Route semantics stay specialist-owned; the same source identity and access restriction must survive every supported path.

| Mention form | Non-graph/direct engine | Graph engine |
| --- | --- | --- |
| `@KNI` | Orchestrator preflight → scoped context query/read as needed → validated specialist ownership → specialist execution → review → terminal delivery. | The same context and ownership contract, with graph state/checkpoints carrying selected references and receipts through execution and resume. |
| `@KNI CoS` | Orchestrator preflight → scoped context query/read as needed → Chief of Staff coordination when appropriate → owning specialist execution → review → terminal delivery. | The same permitted Chief of Staff coordination and specialist ownership, with graph state/checkpoints preserving evidence and preventing repeated completed acquisition. |

**Pilot verification:** `tests/test_lazy_slack_context_backend_matrix.py` exercises all four cells with actual SDK tool loops, direct/LangGraph execution and SQLite reload, using scripted models, mocked Slack and article extraction, and guards against unstubbed network/provider/subprocess calls. Twenty-six current lazy-context controls plus eight existing context controls passed. The model sees query results and exact reads before choosing the source. These tests establish wiring and enforcement; they do not prove default live model choice, actual Chief of Staff coordination, session-enabled behavior, or live Slack delivery. The pilot now has two successful live Orchestrator direct-answer cases; it does not add live coverage to every cell of this matrix.

This matrix specifies required acceptance, not four already-verified production paths. Chief of Staff may legitimately coordinate additional context or synthesize an operating answer. Business Research Analyst remains responsible for substantive public-article research when selected as the owner. Synchronization does not require identical agent counts, identical prose, or bypassing Orchestrator preflight for either mention form. A graph wrapper must not silently replace a specialist's reasoning with a deterministic summary.

**Acceptance in every cell:** run the same synthetic scenario family and inspect acquisition → admission → relevant model use → validated output, including these common assertions:

1. The original root task, current authoritative request, source identity, active-channel scope, request-time boundary, and freshness/completeness limitations survive preflight, any Chief of Staff handoff, specialist input, and review. Historical bot prose or approvals cannot become new authority. A URL-only reply preserves the task; a later explicit correction updates it.
2. A self-contained request makes no channel-history calls. A selected-source request queries only the required channel-local window and reads the selected source. Cross-channel denial and explicitly authorized access behave consistently in all four cells.
3. Tool attachments, model-selected calls, completed tool execution, provider read receipts, cached-read provenance, and source-selection decisions are recorded separately and correlated to the same attempt/source. Search receipts cannot stand in for source reads.
4. A handoff, graph checkpoint/resume, retry, or renderer invocation does not repeat completed provider reads merely to pass context onward. Reuse retained evidence with its freshness label; any intentional refresh or recovery attempt is explicit, bounded, and separately receipted. Compare provider-operation counts rather than requiring identical coordination/model-call counts.
5. Successful child output is promoted to a terminal parent result and one final response in the intended thread. Timeout, restart, late child success, failed delivery, and repeat startup cannot replay acquisition or duplicate publication. An uncertain interrupted outcome remains labeled uncertain until independently reconciled.
6. Engine and mention identity are recorded in each result so a passing direct `@KNI` case cannot be reported as graph or `@KNI CoS` acceptance. Unsupported cells are explicitly reported as unsupported. Offline fixtures prove wiring; model-choice behavior and visible delivery require separately authorized runtime evidence.

Extend this matrix to replies, message actions, WorkItem continuation, and specialized feed contexts after the core four cells pass. Test scheduled runs only when they actually need the same context, without granting implicit cross-channel access. **Status:** a local [backend wiring matrix](../tests/test_slack_channel_context_backend_matrix.py) now parameterizes both mention forms and both WorkItem engines. Its scripted Orchestrator selects Business Research Analyst for each mention. The real selected-URL bundle helper uses an injected extractor; the real research SDK path uses a scripted native model and real output schemas. These controls are designed to exercise the actual direct/graph branches and check one extraction, one synthesis, selected identity in input/state/artifact, and graph node evidence. Root/current request plus the supplied active-channel seven-day fixture remain visible to preflight. SDK sessions are explicitly disabled, and unexpected provider/HTTP/socket attempts fail the test.

The final strict run reports **eight scenarios passing**: each of the four mention/engine cells with a readable article and with an unavailable article. The readable cases retain deduplicated structural Slack provenance in the WorkItem, set the exact target URL, and pass only extracted substantive sources into research validation and artifact attribution. They verify root/current request, channel/window labels, complete extraction snapshot, one extraction, one scripted research synthesis, no broad search, and no external write. The unavailable cases retain the exact read blocker, produce no research artifact or research SDK call, and use the actual final-synthesis SDK path with its scripted model. Graph cases assert actual LangGraph runtime/node events; direct cases assert their absence. Strict guards observed zero unstubbed provider/network attempts, and the matrix file passed Ruff. This resolves the source-ID collision exposed by the first strict run: a structural URL mention is no longer competing substantive read evidence.

The implementation workstream ran `.venv/bin/python -m pytest tests/test_slack_channel_context_backend_matrix.py -q` and reports eight passing scenarios in 3.42 seconds; this review inspected the updated source/assertions without repeating the run. This is accepted offline wiring coverage for these eight controls, not actual model-choice behavior, a live provider read, Slack acquisition/delivery, an executed Chief of Staff handoff, session-enabled behavior, or the complete acceptance matrix above. The broader matrix remains follow-up; passing a mention spelling does not certify every permissible coordination path behind it.

### 10. P1 — Enforce approved-memory retrieval at the model-facing boundary

**Problem found and locally repaired:** `tools/memory_tool.py::retrieve_memory` previously permitted a model to set `approved_only=False`, contrary to its shared prompt policy. `StorageTool` passes that option through and SQLite retrieval conditionally omits approval filtering.

**Implement:** enforce approved-only behavior in the runtime tool, removing the model's ability to weaken it or rejecting `False` while preserving wire compatibility deliberately. Keep historical/unapproved inspection in a distinct operator-review path. Do not globally rewrite agent memory instructions or change unrelated storage inspection functions.

**Acceptance:** a temporary store containing pending, rejected, approved, expired, and superseded memories returns only eligible approved records through the actual model-facing tool, including an attempted `False` argument. CoS/context-pack results remain unchanged. Historical review retains its explicitly authorized scope. Confirm final attached tool schema and focused memory/tool contract tests. **Status:** implemented locally by forcing approved-only retrieval while preserving the existing argument/schema. The implementation workstream reports all 21 focused memory tests passing, including the actual tool's attempted opt-out regression. This review inspected that code and regression; it did not repeat the suite or prove live consumption. Broader route/session parity remains issues 9 and 12.

### 11. P2 — Synchronize references and revisions across context, state, and memory

**Problem:** separate stores already have useful roles, but equivalent information can arrive through different paths without a common revision/freshness comparison. Source IDs alone do not establish current truth, and a saved summary may outlive its evidence.

**Implement:** reuse source IDs, WorkItem IDs, execution/attempt IDs, and memory IDs in a compact reference manifest; add source snapshot/revision and retrieval/expiry metadata where missing. Define which owner updates each reference and when a new request refreshes it. Keep immutable accepted execution input distinct from mutable current WorkItem state and superseding approved memory. Deduplicate by referenced evidence identity, not by copying bodies into a shared blob. Promote only compact reusable facts/feedback through existing memory approval and sanitization boundaries; never mirror raw seven-day channel history.

**Acceptance:** an edited/deleted source, a new user correction, expired memory, and a superseding memory yield explicit, consistent outcomes in the issue-9 matrix. A graph resume preserves its accepted snapshot and detects relevant state/version conflicts; a new request can retrieve fresh evidence. Session/memory copies cannot override a current request, resurrect an old approval, or silently replace a failed provider receipt. A single evidence item retains one identity across projections without multiplying provider calls. **Status:** proposed synchronization work; no new unified store or broad schema migration is prescribed.

### 12. P2 — Verify actual session and memory use across the mention/engine matrix

**Problem:** direct, WorkItem, graph, and CoS paths have different session defaults and memory acquisition points. Availability or policy text is insufficient to claim synchronized use.

**Implement:** extend issue 9 with explicit session-enabled/disabled cases, route changes within a Slack thread, relevant-memory hydration, and memory-absent cases. Record session scope/hash and loaded item count, memory query/result IDs and eligibility, context snapshot identity, WorkItem version, and evidence actually admitted to each model stage. Keep telemetry free of raw message bodies and secrets. Measure duplicate prompt content before adding compaction or changing defaults.

**Acceptance:** all four cells preserve current/root/source/freshness boundaries with sessions both on and off; no cross-thread or cross-channel session reuse occurs; an enabled session on route A cannot make route B treat old bot text as authority; memory hydration is observed at the intended consumer; disabled or empty sessions are represented truthfully; session/tool-call truncation retains valid call/output groups. A narrow source-summary run may proceed without completion of this broader matrix, but cannot certify it. **Status:** proposed parity/observability work; review does not establish universal session availability or recommend enabling sessions globally.

## Limits, privacy, and cost controls

- **Availability is conditional.** Seven days is a requested retrieval window, not a promise that Slack retains or exposes all messages. Workspace retention, deletions, edits, token scopes, channel membership, thread availability, pagination, and service throttling can shorten coverage. This review did not query current Slack account policy or service limits; do not hardcode an assumed provider quota from it.
- **Scopes remain explicit.** Existing setup documents list public/private history permissions and separate assistant/DM configuration. Do not expand installed scopes or treat a public-channel implementation as validated for private channels or DMs. Configuration defaults also differ: code can inherit history enablement from live Slack configuration, while the setup table describes it as disabled by default; reconcile documentation and effective settings.
- **Minimize disclosure.** Query descriptions and compact IDs first; read only relevant bodies. Keep private messages out of public research and externally usable artifacts unless their use is authorized. Treat historical content and linked webpages as evidence, not executable instructions or new approval.
- **Bound work at every layer.** Enforce pages, messages, result size, time, tool calls, and model budget. Reuse a valid scoped read within the request. A cache hit is not automatically fresh. Stop pagination when the requested scope is satisfied; expose incompleteness if the budget ends first.
- **Measure before infrastructure changes.** Evaluate the existing no-new-dependency adapter first. Python/Pydantic are retained for contracts and validation; SQLite and its existing FTS facilities are retained where already suitable for local records. A new vector database, message mirror, broker, or service is deferred because no measured need was established. This was an architecture review, not a comparative package benchmark or installation task.

## Immediate work and live-test decision

Changes already observed in the checkout include empty-thread fallback preservation, a larger specialist context projection, channel source indexing before prose truncation, exact journal-root recovery, selected-source projection and read contracts, and enforced approved-only model memory retrieval. These are narrow repairs, not the target lazy retrieval architecture. The lifecycle workstream reports its worker reload and exact prior-status reconciliation separately above. The latest diagnosed research child had SDK sessions disabled; session implementation/configuration must not be described as history loading in that run.

**Current decision: backend checks pass; the single live diagnostic ended blocked.** The main workstream reports 493 integrated regression checks plus eight strict matrix scenarios passing. In the subsequent Computer Slack diagnostic, the live Orchestrator identified the correct source URL, selected Business Research Analyst, and said no clarification was needed. Python then rejected the source because URL validation scanned serialized JSON and treated an escaped newline as part of the URL. The check now inspects decoded request/context fields; an offline replay of the recorded decision and saved context admits the same URL as a bounded source summary. The final affected gate passed 44 tests; no second live attempt was made. Public preflight blockers now prefer the available specific reason over a generic message. Successful article acquisition, summary, and Slack delivery remain unproved for this final version. Earlier interrupted-run reconciliation is verified separately and does not make the old attempt successful. The lazy active-channel query/read interface remains proposed.

For simple summaries, a compact Orchestrator source-read-and-answer capability is now implemented and verified offline. The model can select itself plus one exact source URL from the supplied request/context. Python acquires bounded text through the shared public extraction boundary, then the Orchestrator writes the short response without search tools or a specialist child. The visible citation must match that selected URL; unavailable text stops before synthesis and partial text adds an explicit evidence limit. Six offline execution tests exercise the real extraction bundle/snapshot boundary with an injected provider and scripted model; they do not prove live selection or page access. Business Research Analyst remains available for deeper analysis or domain-specific evaluation. The combined affected gate passed 730 tests, Ruff, and diff checks; no new live attempt was made for this capability.

The operator's subsequent research-specialist retry reached an SDK input guardrail. Its parent reported a retrieval failure because the raw SDK exception escaped the structured child envelope. Reconstruction of the saved Slack context exposed a false secret match inside a harmless public URL's `ask-ai` slug. A token-start boundary now prevents that embedded-word match while standalone key detection remains tested. The research script also preserves SDK guardrail failures in its structured envelope, and failure classification names the guardrail rather than guessing retrieval failure. Historical complete specialist input was not retained, so the underlying trigger is a high-confidence reconstruction. Seventy-one focused and existing safety tests passed; the repair has not been live-reproved.

Before an authorized live test, inspect the final integrated diff and focused tests, confirm the worker loaded the affected source after active work has ended, and use the existing explicit small budget/stop condition. Run one exact-source request followed by one URL-only continuation if within that authorization. Verify selected source at model input, successful read or honest access limitation, preserved task, terminal parent state, and visible final response. Do not silently resume an interrupted provider operation merely to produce a successful-looking result.

## Validation and source index

### Pilot recovery and compact-continuity boundary

The exact-source graph recovery subsequently completed live on the original
saved task. The canonical task and selected source stayed bound, a research
brief was attached, prior execution blockers were resolved without deleting
their history, and the final response reached the Slack thread. Bold sections,
the labeled source link and task-oriented natural-language continuation were
visibly confirmed. No broad discovery or new extraction occurred in that run.

This operational pass did not prove fresh full-article coverage. An audit found
that the configured six-item SDK tail expanded to nine dependent items. It
contained valid reads of the same immutable source alongside an unaccepted
answer and an orphan prompt from a later stage whose model call had not run.
Research prompt-cache accounting showed no hit; reuse came through the local
session/source paths. The retained valid reads and stale narrative must not be
treated as equivalent evidence.

The next implementation therefore replaces raw session tails with compact
canonical task handles and optional approved facts in this channel pilot. It
retains scoped source readers and cached snapshots. Automatic legacy memory and
retrieval-metric hydration is omitted; memory/profile retrieval is an explicit
model-chosen local query with a two-fact shared limit. Capture requires a current
affirmative request and verified local read-back. The implementation and exact
byte limits are documented in the [continuity plan update](COMPACT_CONTINUITY_IMPLEMENTATION_PLAN.md#implementation-update-scoped-pilot).
Offline tests establish these boundaries; natural model choice, useful tailored
actions, and live continuity still require the authorized serial Slack tests.
No workspace-wide enablement or cross-channel pooling is implied.

The earlier review pass ran 15 focused KS tests (plus three subtests) and two focused KBA tests successfully. KS's documented system `python3` lacked pytest; its tests used the existing KBA virtual environment without installation. Synthetic mocked calls reproduced the failures listed above. The final report audit rechecked current source and regression definitions, local document links, and section anchors; it did not repeat those tests. Concurrent implementation results are explicitly attributed above. No live provider/Slack calls, posts, restarts, external tracker changes, or code edits were performed by the review workstream. Only this report was edited.

Relevant sources, authoritative for their named boundary:

- [KBA Orchestrator projection](../src/keystone_agents/agents/orchestrator.py), [Orchestrator schema](../src/keystone_agents/schemas/orchestrator.py), and [CLI context admission](../src/keystone_agents/entrypoints/cli_impl.py): inspect before changing plan/context transport.
- [Research agent contracts](../src/keystone_agents/agents/business_research_analyst.py): tool admission and required execution evidence.
- [Chief of Staff context tools](../src/keystone_agents/tools/chief_of_staff_tool.py) and [announcement context tools](../src/keystone_agents/tools/announcement_context_tools.py): actual existing query surfaces and their restricted scope.
- [KS Socket Mode](../../keystone-slack/kni_integrations/slack_socket_mode.py), [bridge](../../keystone-slack/kni_integrations/business_agents_bridge.py), [lifecycle router](../../keystone-slack/kni_integrations/assistant_mode_router.py), and [run store](../../keystone-slack/kni_integrations/business_agent_run_store.py): acquisition, handoff serialization, bounded finalization, interruption reconciliation, and attempt/projection lifecycle.
- [KS discovery store](../../keystone-slack/kni_integrations/discovery_store.py): canonical persisted candidate records and existing FTS facilities.
- [KS setup](../../keystone-slack/SLACK_APP_SETUP.md) and [configuration](../../keystone-slack/kni_integrations/config.py): intended and effective local flags/scopes; source wins when these disagree.
- [Earlier KS-to-KBA access review](../../keystone-slack/docs/KS_KBA_CONTEXT_ACCESS_REVIEW.md): ownership map and adapter-based access rationale; this review updates the selective-retrieval target and concrete regression backlog.
- [KBA context tests](../tests/test_cli.py) and [KS app-mention tests](../../keystone-slack/tests/test_app_mentions.py): focused transport/continuation coverage, not independent live acceptance.
- [Context pack derivation and memory hydration](../src/keystone_agents/work_items.py), [workflow execution](../src/keystone_agents/workflow_runner.py), and [graph execution](../src/keystone_agents/langgraph_workflow.py): business-state ownership and direct/graph projection.
- [Accepted context snapshots](../src/keystone_agents/runtime/context_snapshot.py), [session helpers](../src/keystone_agents/sdk_sessions.py), and [SDK session adapter](../src/keystone_agents/sdk.py): replay identity and actual optional session construction.
- [Memory schema](../src/keystone_agents/schemas/memory.py), [memory derivation](../src/keystone_agents/memory.py), [SQLite eligibility](../src/keystone_agents/storage/sqlite_store.py), [runtime memory tools](../src/keystone_agents/tools/memory_tool.py), and [memory policy](../src/keystone_agents/prompts/memory_policy.md): approval, expiry, supersession, and prompt-safe reuse boundaries.
