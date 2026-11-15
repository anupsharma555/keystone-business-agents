# Cross-session memory and recent session-state review

The runtime already separates reusable memory, conversation history, business state, and execution recovery. The recent selective Slack source-summary pilot used channel query/read context and durable execution stages, but its four inspected completed answer envelopes explicitly recorded **no attached SDK session**. Those attempts did not create WorkItems or demonstrate durable-memory retrieval. The main memory database is populated, although almost entirely with older operational metrics rather than strategic, project, company, or feedback knowledge.

This is a read-only implementation and local-receipt audit, captured on **2026-11-14 Eastern / 2026-11-15 UTC**. No provider/model/Slack calls, restarts, configuration changes, database writes, or cleanup were performed for this review. Database counts are a time-bounded snapshot while other work continues. Private request text, account/channel/thread identities, run identifiers, and message bodies are omitted. A saved row establishes persistence; a configured or attached mechanism does not establish that the model read or used its contents.

## Technical index

- [What each state layer owns](#what-each-state-layer-owns)
- [What is implemented](#what-is-implemented)
- [What the inspected databases actually contain](#what-the-inspected-databases-actually-contain)
- [Recent source-summary attempts](#recent-source-summary-attempts)
- [Focused follow-up: historical channel readiness requests](#focused-follow-up-historical-channel-readiness-requests)
- [Follow-up: successful saved-source graph resume and SDK history](#follow-up-successful-saved-source-graph-resume-and-sdk-history)
- [What should become reusable memory](#what-should-become-reusable-memory)
- [Small continuity inputs that add value](#small-continuity-inputs-that-add-value)
- [Cost, duplication, and limits](#cost-duplication-and-limits)
- [Prioritized future work and acceptance evidence](#prioritized-future-work-and-acceptance-evidence)
- [Evidence and verification boundaries](#evidence-and-verification-boundaries)

## What each state layer owns

| Layer | Current role | Proper interpretation |
| --- | --- | --- |
| Selective Slack context | Host-bound active channel and at most seven-day snapshot, queried and read through bounded tools | Historical evidence for this request. An article link/message is not an article read, approval, or durable-memory capture. |
| WorkItem and typed context packs | Task objective, selected sources/artifacts, facts, blockers, approvals, next action and events | Canonical business state for WorkItem workflows. Direct answers can complete without a WorkItem. |
| Durable execution journal and graph checkpoints | Accepted input identity, stage results, model-start accounting, recovery position and terminal state | Execution/replay evidence. A checkpoint is neither reusable knowledge nor fresh permission to repeat an effect. |
| `MemoryItem` / `memory_index` | Typed source-aware facts, strategic guidance, feedback, style, deduplication and operational lessons | Relevant approved guidance, independently retrieved. It does not establish current provider state, current access, or authorization. |
| Local SDK session | A separate SQLite history of model conversation items when a selected call receives a session | Bounded conversation continuity. It is optional and is not the WorkItem, approved memory, a full channel mirror, or universal Slack history. |
| Slack bridge attempt/projection records | Per-attempt routing/status/telemetry plus the latest thread result and delivery linkage | Frontend execution and delivery evidence. Latest projection alone cannot reconstruct every previous attempt. |

These layers should share stable references and provenance rather than duplicate complete transcripts. The current request and current task/provider state should determine what the operator wants now. Selected source context supplies evidence; approved memory supplies relevant reusable knowledge; session history supplies a small conversation tail. Conflicts and uncertainty must remain visible.

## What is implemented

### Memory creation, safety, and retrieval

`MemoryItem` supports typed summaries/content, object keys, source IDs, approval state, confidence, sensitivity, timestamps, expiry, and supersession. Default records are pending. Flags indicating raw email bodies, secrets, PHI, sending enablement, or sent state must remain false. Secret/PHI sensitivity disables prompt safety; `sensitive` is not automatically excluded, so correct classification, sanitization, and explicit prompt-safety decisions still matter.

Creation helpers exist for company profiles/facts, opportunity signals, outreach deduplication, feedback, approval decisions, style, operator references, strategic/project guidance, retrieval performance, and manager-loop efficiency. Some source-backed helpers default to `approved_for_research`; the approval label is therefore not proof that a human individually reviewed every record. The Chief of Staff also has explicit operator-request capture helpers for references and strategic memory. A saved reference URL is not a fetched/verified source.

The model-facing `retrieve_memory` tool forces approved-only and prompt-safe retrieval, retaining its legacy approval argument for compatibility without allowing that argument to widen eligibility. Storage APIs retain historical/operator inspection capabilities. The store removes expired records and suppresses approved superseded records before scoring and limiting results; an expired replacement does not resurrect the superseded record. Ranking is local token matching with confidence and recency-by-ID tie breakers, rather than a semantic/vector retrieval service. No new retrieval package is needed to describe or preserve this behavior.

Chief of Staff strategic retrieval selects relevant memory types and returns at most twelve records (normally eight). WorkItem hydration uses approved prompt-safe retrieval and attaches bounded references to typed packs without changing readiness gates. It deduplicates records within each retrieval result, but a record can still appear in both a general field and a specialized field. General memory results normally default to five and are capped at twenty-five.

Shared instruction composition includes a memory policy; **policy text is not memory data**. Actual records enter a model only through supplied context packs, the Chief of Staff context, or a tool result in that model loop. Route-only Orchestrator calls and the pilot's two-tool channel capability must not be described as automatically consuming the full memory toolbox. The recent pilot receipts below do not establish memory retrieval or substantive use.

### SDK session identity and limits

Session identity is hashed. Slack sessions derive identity from workspace/channel/thread; this key does not include the agent or route, permitting within-thread cross-route reuse when sessions are enabled. Unscoped CLI sessions use operator, working directory and route; non-Slack WorkItem sessions use WorkItem identity. Explicit flags/environment can override defaults.

The generic history setting is **24 items**. Direct Slack ask and WorkItem helpers normally request **six items**, because the typed context already carries root/recent turns. The separate Slack-action session helper does not explicitly request six and therefore resolves the generic configured/default limit. These are conversation-item settings, not time windows or total-retention limits. The SDK adapter may widen the suffix to keep dependent reasoning/function-call/output units intact, so the setting is not a strict maximum number of returned items or tokens.

Defaults vary by route and execution path. Ordinary direct research asks normally default off; scoped Chief of Staff/context/Gmail paths may enable continuity, while those unscoped calls default off. WorkItem session defaults follow live-SDK execution. A session object's existence and audit metadata establish attachment/configuration; current audit metadata does not record an actual history item count, retrieved-history hash, or per-item usage.

### Recovery and accepted input

Accepted workflow JSON can be frozen independently of an ephemeral context file and restored only for the same accepted request/snapshot identity. A new user correction should become a new accepted request rather than silently modifying a historical execution. Graph checkpoints preserve execution position while WorkItems remain the business state. Frozen input preserves reproducibility, not indefinite freshness or access entitlement. Memory rehydrated on a later resume can differ from frozen task context; affected decisions need explicit provenance and freshness handling.

## What the inspected databases actually contain

The main runtime database inspected was the repository-root `keystone_agents.db`; alternate files with similar names were not assumed equivalent.

| Surface | Observed snapshot | Meaning and limit |
| --- | --- | --- |
| Main memory | **4,602** records: 4,449 `manager_loop_efficiency`, 150 `retrieval_tool_performance`, 3 `operator_reference` | All were approved-for-research, internal, prompt-safe. No company/project/strategy/feedback/style memory types were populated in this selected database. |
| Memory lifecycle | Latest creation **2026-08-03 UTC**; zero populated expiry or supersession fields | Lifecycle behavior exists in code/tests but is not being exercised by these stored records. Age alone does not prove a lesson is wrong. |
| Memory repetition | 4,449 efficiency rows have 1,619 distinct summaries; 150 retrieval rows have 39 distinct summaries | Repeated summaries are measured; full semantic duplication is not proved because content/run associations can differ. No deletion is proposed. |
| Memory text size | Approximately **4.18 million characters** across title, summary and content | Local stored size, not prompt tokens or model cost. |
| SDK session store | **1,001 sessions / 5,019 messages** at the inspected instant | The store exists and was recently written. Activity can include offline tests or other routes; it does not identify the pilot's history. |
| Durable journal | 35 executions; 115 model-start rows; 145 stage rows; 29 checkpoints across 3 graph threads | These are different counters. Starts are not successful responses; checkpoints are not proof every direct run used a graph. |
| Journal linkage | 3 completed executions linked a WorkItem; 26 completed and 5 failed had no WorkItem; 1 had running status | Global snapshot, not a finding that the running row was stuck. No reconciliation or replay was attempted. |
| Journal tool/delivery tables | Zero operation, provider-scope and delivery rows | Specific selective reads were persisted in stage outputs instead. These empty tables do not mean that no tool/provider/delivery ever occurred. |

The alternate `data/keystone_agents.db` had zero memory rows. Two additional local state files lacked a readable memory table. This reinforces the need to record the effective database in diagnostics; it is not evidence that every alternate file is an active runtime store.

## Recent source-summary attempts

The latest six inspected Slack attempt records occupied six distinct threads in one channel. They consisted of four completed Orchestrator source-summary answers and two clarification blocks. None linked a WorkItem. Their local timestamps span the evening of the audit date; raw identities and request text are intentionally omitted.

All four completed answer envelopes reported `session_attached=false`, history limit zero, and no memory-retrieval evidence. Each final synthesis recorded selected article extraction as pre-acquired context and zero attached/model-called tools in that **final synthesis stage**. That is consistent with a tool-free synthesis following earlier acquisition; it does not erase preflight reads.

The three newer completed selective-context executions separately retained query/read tool-result stages in the durable journal. Their model-start counts were four, four and five across the entry execution. Their preflight usage recorded three, three and four requests respectively; the additional final synthesis is a different model stage. Two recent clarification attempts also retained bounded query stages. These receipts establish persisted acquisition-stage evidence and terminal statuses. They do not prove default Chief of Staff coordination, enabled-session continuity, memory creation/retrieval/use, or every graph/direct route.

The four completed final synthesis stages reported approximately **7,677–8,142 input tokens** each. The newer selective-context preflight stages reported **58,816–77,318 aggregate input tokens** over three or four requests. Total request cost must include preflight and final synthesis rather than presenting final-answer usage as the whole execution. Monetary estimates were not recomputed in this audit.

This review inspected stored source-summary evidence and Slack attempt metadata, not the real Slack interface, source website, or running worker fingerprint. It therefore does not independently certify current loaded runtime, article fidelity, visible presentation, or delivery. The main task's separate live checks own those boundaries.

## Focused follow-up: historical channel readiness requests

A later read-only comparison examined two blocked readiness-note attempts against two earlier completed article summaries in the same channel. These were separate root Slack threads, not continuations of one SDK conversation. Each attempt had its own Slack attempt, KBA entry/final run pair, and durable execution. None linked a WorkItem. The current request remained the explicit instruction in each run; the two readiness requests limited evidence to one historical channel post and its link titles and prohibited external-page reads and other actions.

| Evidence surface | What these attempts actually loaded or retained | Boundary |
| --- | --- | --- |
| Preflight channel context | Each model preflight called `query_slack_channel_context` once and `read_slack_channel_context` once. The bounded read returned one historical post, with three source-link titles, coverage and limitation fields, a channel/time snapshot, and a retrieval timestamp. | This is cross-thread information sharing through Slack retrieval. The post is untrusted context; link titles are not article contents. |
| Repeated source identity | The same historical post payload appeared in the earlier completed article-two summary and both later blocked readiness requests (equal serialized payload hash in the stored preflight evidence). | Each run reacquired the post. The receipt does not show reuse of a prior answer, artifact, or memory record. |
| Task and decision state | The preflight retained the raw request, a manual request plan, routing result, and `workflow_state` with channel capability, evidence, and usage. `state_context_used=true` coexisted with `approved_context_present=false` and `requires_durable_state=false`. | Here “state context” denotes admitted channel evidence; it must not be interpreted as WorkItem or approved-memory hydration. |
| Reusable memory and history | Both preflight and final model request caches reported `session_attached=false` and history limit zero. Model-called tools were the two channel tools, with no memory tool call. No WorkItem ID was linked. The selected memory database still had 4,602 rows, none created during these attempts. | No receipt establishes `MemoryItem` retrieval/admission, SDK history loading, or WorkItem hydration. A shared memory policy prompt is instructions, not a retrieved memory record. |
| Final result | Both readiness attempts ended blocked. The first final envelope lacked a separate `channel_answer_evidence` field, although its preflight retained the post. The second final envelope included one channel message with three link titles and still blocked. | The first field's absence is an evidence-carrying/observability gap, not proof the preflight never saw the post. The second block shows that retaining source evidence alone did not produce an accepted readiness answer. See the separate input/acceptance diagnosis for the actual decision failure. |

The two blocked preflights each made three model requests and recorded **70,747** and **68,203** aggregate input tokens; their final synthesis requests recorded **6,639** and **8,179** input tokens. The earlier completed comparators recorded **58,816** and **58,831** aggregate preflight input tokens and **7,923** and **8,142** final input tokens. For the blocked runs, the first preflight request alone was **22,585** and **21,737** input tokens, before channel tool responses; later request inputs rose by about 790 and 1,412 tokens. The channel adapter reported 6,895 response bytes per blocked run across query and read. Cached-token counts are subsets of input usage, not extra tokens to add.

These receipts rule out SDK-session history and retrieved memory as the source of this pilot's large prompt usage. They do **not** allocate the remaining tokens among static instructions/schema, current request, dynamic planning, repeated model turns, and channel tool results. The journal retains tool outputs and model-start counts, not exact serialized per-request model inputs or token counts by field. A future cost change should measure those components before trimming evidence. In particular, a compact reference to the historical post could avoid repeated full retrieval only if a bounded, permission-checked read can resolve it when needed; its three link titles alone cannot substantiate article contents or a broader readiness claim.

## What should become reusable memory

| Suitable compact memory | Required context | Keep elsewhere |
| --- | --- | --- |
| Explicit stable operator preferences, project goals/constraints, priority decisions and avoidance rules | Who authorized capture, subject/project scope, rationale, effective time, source, review/expiry policy | The entire conversation or an inferred preference based on one request |
| Useful source-backed company facts or opportunity signals | Exact source identity, retrieved/source date, confidence, scope and refresh requirement | Full article text, raw search results or unverified claims copied from bot prose |
| Reusable reviewer corrections and retrieval lessons | Generalized lesson, evidence/run reference, relevant task class and validation status | Every tool trace, every failed query or repeated metric snapshot |
| Sanitized approved style/examples | Approval purpose and provenance, with factual reuse prohibited | Raw mailbox bodies, full private contact details or unrestricted email threads |
| A concise task outcome useful across sessions | Link to authoritative WorkItem/artifact; as-of time; clear status-at-capture label | Live task state, unresolved approval authority or provider state that must be read fresh |

For the source-summary pilot, ordinary channel messages should stay selectively retrievable. A useful article summary may become a source-linked artifact; it should become durable reusable memory only when explicit operator capture or a reviewed capture policy makes its future value and scope clear. Do not automatically memorize every tested answer. Avoid storing secrets, PHI, personal/customer details, raw private messages, entire prompts, stale approvals, or claims whose supporting source was never read.

## Small continuity inputs that add value

The aim is fewer repeated explanations and better decisions, with a small, optional context addition. A new source-summary request usually needs the selected source and current instruction, not an accumulated memory of prior summaries. A follow-up can benefit from the current task's source handles and latest correction. A stable preference can improve answer length or emphasis without dragging the conversation that established it into every prompt.

Use relevance selection before prompt admission. Skip a layer completely when it adds no useful information, and avoid sending the same fact through task state, memory and SDK history. Retain complete source/artifact references in local state; expand only the source the task actually requires. A memory claim that depends on a changing website must carry its as-of date and refresh requirement. An old preference can guide style; an old task approval cannot authorize a new operation.

The following are **candidate future pilot budgets**, not implemented settings or measured optimal values:

| Optional continuity input | Candidate admission budget | Value condition |
| --- | --- | --- |
| Current task summary plus source/artifact handles | At most **160 tokens** | Objective, latest correction, unresolved blocker and selected source identity are needed for the follow-up. Full state remains locally authoritative. |
| Relevant approved memory | At most **two records / 192 tokens total** | Each record answers a concrete current need: stable preference, project constraint or a compact verified prior fact. Omit unrelated and stale records. |
| SDK conversation tail | At most **128 tokens of admitted context**, conditional | Only when the task summary/selected references omit necessary conversation meaning. Prefer task state when either would supply the same fact. Existing item-count settings do not enforce this token proposal. |
| Combined continuity addition | **512-token hard admission ceiling**, normally less | The layer allocations total 480 tokens, leaving a small envelope margin. When over budget, remove redundant/low-value material; preserve current user instructions and explicit limitations. |

This candidate ceiling limits incremental continuity to roughly six to seven percent of the inspected final synthesis input sizes. It does not justify increasing every run by that amount, and it is not a cap on source evidence needed to complete a substantive request. Selected article excerpts should have their own task-specific evidence budget and completeness warning, rather than being disguised as memory. Preflight loops also need an independent whole-execution token/cost cap because each turn can resend policy, schema and evidence. A small memory cap alone will not solve that separate overhead.

For the same candidate pilot, perform local relevance retrieval without adding a model request. Each layer's monetary allowance is bounded by its token allowance multiplied by the selected stage's verified input-token rate; the combined incremental cost target is **at most ten percent of the matched no-continuity baseline**, counting every resend across turns. Do not add an independent LLM compression step merely to fit memory. If a layer cannot fit or pass the value/relevance check, omit it or use an explicit bounded read. These relative cost limits require current model pricing and run accounting before any live evaluation; this report does not claim they are enforced now.

The first value evaluation should compare otherwise identical cases with continuity off and with the bounded relevant input. Use synthetic/offline cases first: a source follow-up that must retain the exact article, a changed instruction that must supersede the old direction, and a new task that should reuse one explicit stable preference. Include negative controls where memory is irrelevant, stale, contradictory, belongs to another scope, or contains an old approval. Measure correctness, unnecessary clarification/retrieval, constraint adherence, wrong-source selection and privacy failures, alongside added input tokens and total model requests. Keep an injection only when the benefit is demonstrated for that task class; do not accept extra prompt text merely because retrieval succeeded. Any later live comparison needs its own authorized small budget and serial execution.

Small budgets can omit nuance; excessive compression can also hide uncertainty. A source handle without readable evidence cannot support a substantive claim. On an essential missing detail, perform a bounded read or ask a targeted clarification instead of guessing from a short memory. These tradeoffs favor selective retrieval with explicit missing-context signals over a universal prompt dump.

## Cost, duplication, and limits

**Later native-graph observation:** the retained context pack for the blocked article-review resume contained five `retrieval_tool_performance` memory references, including unrelated prior opportunity-search metrics; three also appeared under `retrieval_performance_notes`. This differs from the earlier direct-answer cases with no observed memory attachment. The saved pack proves hydration into execution context, not useful model uptake or the exact final serialized prompt. It reinforces the need to filter by destination and useful fact type before ranking, admit whole entries under a small combined byte budget, and avoid repeating operational metrics as task facts. No stored memories were edited or deleted. Compact continuity/profile implementation is still conditional on successful graph verification.

The same information can enter via typed Slack context, a WorkItem pack, a memory excerpt, and SDK history. This is a demonstrated set of parallel code paths, **not evidence that all paths duplicated data in the inspected pilot**. SDK sessions were off in the four summaries and no memory use was evidenced. The measured prompt overhead therefore should first be investigated in preflight/tool-loop inputs, tool returns, static policy/schema size and repeated turns.

Before adding more memory, record admitted character/token counts by surface, memory IDs and source revisions, actual loaded history counts, and deduplication results. Prefer one bounded authoritative excerpt plus references to it. At present, a memory retrieval can scan all eligible rows of selected types in Python before ranking; this is a possible growth concern, not a measured latency problem. The large ratio of metrics to substantive memory suggests prioritizing useful capture and retrieval quality rather than installing a vector database. No package was evaluated or installed for this diagnostic-only report.

No first-class workspace/channel/disclosure scope is enforced by the generic `MemoryItem` fields or retrieval signature. Object keys and free metadata do not automatically provide that guarantee. This matters before future cross-channel rollout, even though the current pilot is confined to one channel. Likewise, no per-type expiry policy, session-store retention policy, or comprehensive source-edit/deletion/access-revocation lifecycle was established by this audit. Do not enable additional channels or clean stored data as an incidental consequence of this review.

## Prioritized future work and acceptance evidence

| Priority | Work | Smallest trustworthy proof |
| --- | --- | --- |
| P1 for memory-enabled claims | Add privacy-safe telemetry distinguishing capture, stored record, retrieval, model-visible admission and output source association | Synthetic memory saved in a temporary DB, retrieved in a later independent process, exact ID visible before the model decision, resulting source association retained; missing memory clearly reported. Do not equate admission with causal influence. |
| P1 before broadening disclosure | Define and enforce project/workspace/channel scope plus destination permission, and source revision/freshness metadata | Two synthetic scopes, same keywords: unapproved scope never enters tool output/prompt; revoked access and edited/deleted source cannot appear as verified current evidence. Current pilot channel stays unchanged. |
| P2 | Establish useful memory capture and approval policy; distinguish operator-approved decisions from automatically research-approved summaries | Explicit synthetic capture survives process restart; pending/unsafe memory excluded; research approval cannot enable external use; unsupported facts rejected or marked unverified. |
| P2 | Specify expiry/review/supersession by type; preserve historical records | Boundary-time expiry and replacement tests, including expired replacement not resurrecting an old fact; model-visible provenance identifies as-of time and verification needs. No historical cleanup is required. |
| P2 | Exercise enabled-session continuation across routes, long threads and restart using isolated session files | Fresh thread gets no unrelated history; same-thread continuation loads bounded prior items; dependent tool units remain valid; latest correction wins; old bot prose/approval cannot become authority; no repeated completed effects. |
| P2 | Isolate session test persistence and measure history actually loaded | Temporary session DB for all relevant tests; production/default session file unchanged; telemetry records count/hash rather than raw history. The shared pytest setup isolates the business DB but does not globally redirect the default SDK session path. This is a test-isolation gap to verify, not proof every recent session was a test. |
| P2 | Measure and remove redundant prompt admission and repetitive operational-memory capture | Compare admitted bytes/tokens and retrieval relevance on representative requests; dedupe by record/source identity; preserve independent run receipts. Do not delete repeated summaries without reviewing full content and retention requirements. |

Existing bounded context retrieval, WorkItem ownership, accepted snapshots, memory eligibility ordering, dependent-item-safe session suffixes, and no-send/write gates should be preserved. There is no evidence here supporting blanket session enablement, universal automatic memory capture, a new storage backend, raw channel mirroring, broad cleanup, or changes to every agent prompt.

## Follow-up: successful saved-source graph resume and SDK history

A later saved-source graph resume completed the original WorkItem and persisted a
Research artifact. The entry recorded five model requests: four preflight requests
and one Research response. Research made no new article-window call. This is an
operational persistence result with explicitly partial source coverage, not fresh
verification of the whole article or evidence that reusable memory was retrieved.

This follow-up inspected the completed Research row, frozen graph request, current
source references, earlier read receipts, and the matching local SDK session through
read-only connections. Raw requests, source URLs, record identities, hashes, and
message bodies are omitted here. The current Research packet contained one source
identity, its retained initial excerpt, the exact current resume request, and the
current read-only/no-external-action constraints. Its canonical plan permitted only
reading that source, and its attached tool was the scoped local source-window reader.

The session audit recorded `session_attached=true`, derived Slack-thread scope, and
a configured history limit of six items. There were 32 older items before this
Research request. Reconstructing the current dependency-safe suffix from those
stored items returns **nine items, approximately 69,686 serialized characters**:

- One preceding reasoning item and two function calls with their two completed
  source-window outputs.
- Two additional reasoning items and a structured Research answer from an earlier
  attempt that subsequently failed its tool-execution contract.
- An approximately 21,410-character user-facing synthesis input from that blocked
  attempt. Its model start was rejected by the shared request budget, but its input
  remained in the session. It was a prior final-synthesis payload, not a successful
  response, a new operator command, or the tool-correction prompt.

The extra three items are explained by the adapter retaining call/output and
preceding-reasoning dependencies. The reconstruction uses stored item order and the
same suffix helper as the current runtime. No provider-side request-body capture or
per-call loaded-item receipt was available, so this is a source-backed reconstruction
of expected session admission rather than a separately captured provider payload.
An item limit is neither a strict item ceiling after dependency repair nor a byte
or token ceiling. Thread-only session identity also does not distinguish specialist,
final renderer, WorkItem, schema, or previously accepted versus rejected answers.

The two retained read outputs have the **same source identity and snapshot
fingerprint** as the current selected source. Their half-open ranges are
`[9084, 17001)` and `[17084, 21200)` of a 30,639-character snapshot. The current typed
packet separately supplies `[0, 1200)`. The numeric study finding used in the new
answer appears directly in one of these retained tool outputs, rather than only in
the prior narrative. This supports historical same-source evidence availability;
it does not establish causal model influence.

The preceding failed graph had five successful reads of this same snapshot. Together
with the initial excerpt, those five windows leave gaps `[17001, 17084)` and
`[29152, 29200)`. Only two of the five outputs are in the reconstructed current
session tail. Both gap descriptions appear in the **unaccepted prior assistant
answer** and the new accepted answer, but not in the current typed source packet.
Thus the current coverage narrative reused an earlier model's account; it was not
recomputed or freshly verified by a new read. Older same-snapshot reads also exist
outside this tail. They must not be treated as admitted evidence merely because
they remain somewhere in the session database.

Research usage was **49,917 input tokens, 2,998 output tokens, and zero cached input
tokens**, with 49,914 cache-write input tokens reported for that response. Cache-write
and cached counts are input subsets, not additional tokens. A stable cache key was
present, but this response does not demonstrate a prompt-cache hit. Session reuse,
local source-snapshot reuse, and provider prompt caching are separate mechanisms.

The current source-ID validator accepted the one declared source, and the tool
closure still prevented reading a different snapshot. Those gates constrain
selection and new tool execution. They do not independently validate every factual
or coverage assertion copied from old assistant text, and they do not remove old
user-role synthesis instructions from the provider history. No scope escape was
observed in this result; the raw history admission is a general contract risk and
an avoidable source of prompt size.

The smallest recommended pilot change is to omit automatic raw SDK history at the
host session-admission boundaries for opted-in saved-source/source-summary work,
while preserving the session database, global defaults, typed WorkItem state, and
scoped local snapshot reader. WorkItem callers share
`_sdk_session_spec_for_work_item`; direct asks use `_sdk_session_spec_for_ask` and
the existing child-session environment transport. An explicit denial must also
prevent `run_typed_sdk_agent` from inheriting an environment session when the
passed session is `None`. Existing `inherit_env_session=false` support supplies
that boundary when forwarded by the relevant agent wrappers. Setting an item
limit to zero or merely passing `None` is insufficient.

If historical reads are later projected as evidence, admit only completed,
scope-authorized source windows matching the exact source identity, snapshot,
coverage and as-of time. Keep rejected assistant answers and prior user-role
synthesis payloads out of that evidence projection. Omit a complete optional
history unit when it cannot fit the shared byte budget; do not split provider
dependencies or silently truncate source identities. This audit recommends that
contract but does not implement it, change session settings, call a provider, or
clear stored history.

## Evidence and verification boundaries

Current implementation sources: [memory schema](../src/keystone_agents/schemas/memory.py), [derivation/capture helpers](../src/keystone_agents/memory.py), [model-facing memory tools](../src/keystone_agents/tools/memory_tool.py), [store eligibility/ranking](../src/keystone_agents/storage/sqlite_store.py), [WorkItem memory hydration](../src/keystone_agents/work_items.py), [Chief of Staff capture/review](../src/keystone_agents/agents/chief_of_staff.py), [shared memory policy](../src/keystone_agents/prompts/memory_policy.md), [instruction/session adapter](../src/keystone_agents/sdk.py), [session specification](../src/keystone_agents/sdk_sessions.py), [direct ask defaults](../src/keystone_agents/entrypoints/cli_impl.py), [WorkItem defaults](../src/keystone_agents/workflow_runner.py), [Slack-action session helper](../src/keystone_agents/slack_actions.py), [session audit metadata](../src/keystone_agents/run.py), [accepted snapshots](../src/keystone_agents/runtime/context_snapshot.py), [bounded channel tools](../src/keystone_agents/tools/slack_channel_context_tools.py).

Read-only local evidence: repository-root `keystone_agents.db` (`memory_items`, `agent_runs`, `work_items`); companion `keystone_agents.db.execution.sqlite3` (`executions`, `execution_model_attempts`, `execution_stages`, checkpoints); `.keystone/sdk_sessions.sqlite3` (session/message counts and times); adjacent Slack runtime `.local/workflow-state.sqlite` (per-attempt and latest-projection metadata). These are local operational stores, not report artifacts to publish or commit.

Relevant regression definitions were inspected in [memory tests](../tests/test_memory.py), [SDK session tests](../tests/test_sdk_sessions.py), [lazy context/backend matrix](../tests/test_lazy_slack_context_backend_matrix.py), [Chief of Staff/graph backend matrix](../tests/test_slack_cos_graph_backend.py), and [pytest isolation](../tests/conftest.py). Tests were **not rerun by this audit**. Scripted tests establish wiring, not natural model choice or actual memory influence. The wider [context review](CROSS_CHANNEL_CONTEXT_REVIEW.md#context-execution-state-memory-and-sessions) complements this report; current rows and source inspection, not that earlier document's historical results, support the findings above.
