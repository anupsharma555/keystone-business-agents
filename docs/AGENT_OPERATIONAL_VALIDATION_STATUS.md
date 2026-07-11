# Agent Operational Validation Status

This is the current evidence matrix for natural-language agent operation. It
does not use the deferred Promptfoo suite as a readiness gate. A green
framework test proves contracts and execution shape; it does not substitute for
live tool selection, provider read-back, useful model reasoning, or graph job
completion.

Status vocabulary:

- **Proven:** current direct evidence covers the named dimension.
- **Partial:** useful evidence exists but does not cover the complete dimension.
- **Pending model:** the next proof requires a separately approved model run.
- **Blocked:** the required context/provider state is currently absent.
- **N/A:** the dimension does not apply to that control-plane or read-only agent.

## Current Matrix

| Agent family | Interpretation / route | Typed tool / provider | Useful reasoning | Lifecycle or read proof | Safety / continuation | Current boundary and next proof |
|---|---|---|---|---|---|---|
| Orchestrator | Proven across direct and graph entrypoints; the corrected source-bundle CLI selected deterministic preflight plus the WorkItem/LangGraph path live | N/A control plane | Proven for bounded supplied-material orchestration into one specialist SDK synthesis | N/A | Target/source identity, typed handoffs, approval checkpoint, one-request ceiling, usage receipt, and no-write boundary passed. Connector preparation now proves bounded Gmail selection promotes exact thread identity without persisting raw bodies, and its request guard blocks before preflight/provider access | Fixed-source graph proof passes. The prepared connector-backed Gmail graph requires fresh approval for 4-6 expected requests, hard ceiling 8, and $0.50; after that, validate the same graph entry from Slack without broadening writes. |
| Chief of Staff | Proven offline for manager ownership and structured handoffs; complete Calendar CRUD asks use a direct deterministic fast path | Partial: typed context lanes and agents-as-tools are covered with fakes; live Calendar CRUD and joined Gmail→Calendar execution are proven without graph/model overhead | Calendar execution intentionally needs no model. The joined Gmail path locally selected and extracted one unique future deadline with time while blocking multiple-date ambiguity | Proven live: dedicated KBA OAuth plus all-day and timed create/update/delete contracts; joined Gmail selection created/read back/deleted one marked timed event and verified absence; natural title-based modification works without an event ID in the ask | Joined run inspected one bounded Gmail candidate, retained only hashed source/participant identities, preserved July 24 at 23:59 in `America/New_York`, sent no invitations, modified no Gmail state, and used zero OpenAI requests | Reuse Calendar and joined Gmail evidence. Private weekly-packet synthesis remains outside the Codex launch path until a trusted operator/MAM/ZDR/local-model boundary is available. |
| Gmail Triage | Proven live for selected-thread interpretation, today-only grouping, same-session revision, sent-style comparison, and exact natural create/update wording | Model reasoning over bounded batches/thread is proven; connector reads, reversible drafts, bounded `SENT`, draft resolution, derived-slide attachment selection, and the dedicated synthetic send tool pass | Selected-thread, batch, continuation, style, and joined create/revision quality pass. The joined runs retain exact synthetic identity, approved facts, approval state, concise copy, and `Sincerely, Anup` | Live provider lifecycles cover marked draft CRUD, derived-slide attachment CRUD, one authorized synthetic send, natural model create/update cleanup, and a selected provider thread → Outreach → verified Gmail draft → exact cleanup handoff | The ANU-192 joined pass used one successful `gpt-5.4-mini` request, 34,034 input/166 output tokens, no retry, and a `$0.0262725` estimate. Exact draft read-back and absence verification passed; no send occurred and the sanitized receipt omits recipient/body content | Primary Gmail R/W/M and the selected-thread Outreach provider-draft bridge are complete. Reuse existing evidence; no second test send, style comparison, or joined lifecycle is needed. |
| Business Research Analyst | Proven for single, comparison, research-to-Doc, public-contact, and current-funding asks; supplied-material graph routing passes live | Agent-selected source deduplication/ranking is proven with a fake SDK model. Public contact combines first-party reads with bounded Exa corroboration; funding validation combines one Exa discovery request with deterministic entity/date filtering before one no-tool synthesis | Latest-article synthesis, Zotero-to-NeuroFlow relevance, three-company comparison, NeuroFlow research-to-Doc, current commercial-contact selection, and Headway funding relevance all pass | Direct official-source extraction covers NeuroFlow, Headway, and Spring Health plus Zotero/Crossref metadata. Headway funding retained five correct-company sources and rejected unrelated Headway entities | Headway pass used one model request, 33,491 input/1,473 output tokens, zero retries/tools, and `$0.03174675` estimate. It correctly treated the July 2024 round as 718-day-old background, not fresh opportunity evidence. Source-publication dates now override Exa crawl dates. No draft/send/write occurred | Direct, fixed-source graph, comparison, research-to-Doc, public-contact, and freshness-sensitive funding proofs pass. Reuse them; new live search needs a materially different freshness question and new API allowance if model synthesis is required. |
| Opportunity Scout | Proven offline for source-provided filtering/ranking and live over a recovered exact two-page packet | Agent-selected deterministic scoring is proven with a fake SDK model; the final supplied-source run used explicit no-tool mode with `tool_count=0`, no search, and no writes | Proven live for the bounded Hack for Humanity assessment: correct event type, unknown geography, upcoming timing, conditional prototype/portfolio fit, missing eligibility, and explicit non-consulting/non-revenue/non-cash boundaries | Direct extraction plus a persisted exact packet and live-model judgment prove the current public opportunity path; no connector lifecycle applies | PASS after offline revalidation of two overly literal acceptance checks. Record, bundle, and result each normalize the two first-party Devpost pages to one independent domain; search metadata is empty. One request, 38,883 tokens, `$0.04699725` maintained estimate, no retry/tool/search/outreach/write | Reuse this proof. Next Opportunity work should be a materially different current-source or graph job, not another same-packet normalization rerun. |
| Outreach Composer | Proven offline for approved-context drafting/revision and live for fixed-source graph, direct compact revision, and selected Gmail-thread drafting | Agent-selected unsupported-claim validation is proven with a fake SDK model; supplied-context no-tool mode is explicit. The joined live bridge consumes one unique dual-marked Gmail `SENT` thread, uses bounded source IDs and an approved aggregate style profile, then hands exact copy to deterministic Gmail draft execution | Proven direct live for source-backed revision and joined live for a natural receipt-confirmation follow-up with one CTA, no invented relationship, and exact `Sincerely, Anup` signoff | Selected provider-thread identity, model draft, exact approved Gmail create/read-back, marker-gated delete, and absence verification all pass without persisting recipient or draft copy | Four serial learning requests were used: two exposed a redundant literal-test-language validator, one exposed style-profile IDs being misclassified as factual sources, and the post-fix request passed with 34,034 input/166 output tokens and a `$0.0262725` estimate. No run sent email; only the passing run created a draft, which was deleted and confirmed absent | ANU-192's remaining joined provider proof is complete. Keep style/template/example IDs advisory while continuing to reject unknown factual sources; reuse this pass. |
| Airtable Context | Proven live for natural create/update/delete execution, both attachment modes, and provider-backed synthesis | Natural `gpt-5.4-mini` execution selects live record/attachment tools correctly; a separate no-tool synthesis mode consumes provider-prepared packets while Python owns identity, arithmetic checks, approvals, and cleanup | The joined synthesis pass interpreted one real provider-backed synthetic expense, computed `$42.00 + $3.36 = $45.36`, identified Software/debit-card context and a missing receipt, and recommended exact follow-up | Create/update/delete, HTTPS link attachment, private local upload, exact-ID read, bounded synthesis, and marked cleanup all pass. A fresh broad-marker query found zero synthesis test records | The synthesis pass used one request, 29,209 input/745 output tokens, zero retries/tools, and a `$0.02525925` estimate. Python verified create/read, kept any review plan approval-gated, deleted the marked record, and confirmed absence; no raw record, send, or post persisted | ANU-198 record read/synthesis/write/modify/attachment/cleanup scope is complete; reuse it. ANU-211 structural creation remains resource-scope blocked because new-base creation requires `Add all resources` or a dedicated structural PAT. |
| Google Workspace Context | Proven for natural Drive/Docs/Sheets/presentation ownership, a live Sheet lifecycle, live selected-document synthesis, and joined research-to-Doc execution | Proven live: `gpt-5.4-mini` selected the exact `README.doc` through Drive search then Docs read; fake-model execution covers Doc/folder writes, presentation search→read, slide-copy previews, and derived-artifact destinations | Proven live for selected-file reasoning and for rendering an exact source-backed Business Research result into a concise internal Doc | Sheet, folder, Doc, selected-file read, presentation read/copy, Gmail attachment, Airtable attachment, and research-to-Doc lifecycles pass. The joined Doc used the same provider identity across create/read/update/read/trash and verified `trashed=true` | Joined research execution used one upstream model request and zero Workspace model requests; no search/send/share occurred. Both write gates are now required before upstream model spend. Google Docs inline-image insertion still requires a publicly accessible URI | Reuse selected-file and lifecycle evidence. True Google Doc image embed remains blocked until a reviewed private-to-public staging policy or another authenticated insertion mechanism exists. |
| Zotero Context | Proven for structural library actions, latest-item resolution, DOI enrichment, and provider-backed graph handoff | Proven live: `gpt-5.4-mini` selected versioned note create/update/delete tools; zero-model provider execution now also proves marked collection and webpage-item create/update/delete with tag and membership reads. A refreshed read resolved the newest top-level `journalArticle` by explicit `dateAdded` descending order, normalized its malformed presentation-suffixed DOI, and retrieved Crossref abstract metadata | Useful note-lifecycle reasoning, latest-article summary, and NeuroFlow relevance judgment pass. Natural collection/item tool selection remains an optional later model proof | Natural marked-note lifecycle passes. A separate marked collection plus one webpage item passed create/read/update/read, exact tags/membership, item-first delete, and independent absence verification; latest-item/Crossref and Zotero→Business Research handoff also pass | Exact keys/versions, separate note/library live gates, marker checks, non-empty collection refusal, cleanup-on-failure, no unrelated action, and zero OpenAI requests passed for the structural lifecycle | Provider-level note and structural collection/item R/W/M cleanup pass. Reuse this evidence; run a natural model sequence only if agent tool-selection evidence is still needed after the next approved batch. |
| RSS Context | Proven through a natural-language WorkItem route | Proven live-read: the first-class agent path retrieved bounded current KNI digests from configured `#announcements` through the typed Slack API tool | Proven without a model for Keystone-domain ranking, cross-item themes, research/opportunity implications, monitoring directions, source links, and historical-versus-current caveats | Five relevant digest items passed stable identity and direct-source checks after an initial off-topic ranking defect was repaired; selected IDs now enter explicit Business Research and Opportunity Scout handoff packets | Explicit CLI plus process dual gate, structured read-only API, manager review pass, zero OpenAI requests, and no-post/no-modify boundary proven | Contract complete. Compare model synthesis later only if it adds material judgment beyond the current structured insight packet. |
| Preprints Context | Proven through a natural-language WorkItem route | Proven live-read: linked Keystone discovery store returned persisted medRxiv/arXiv/PsyArXiv candidates through the typed tool | Proven without a model for bounded ranking, version deduplication, paper-set themes, item-specific research frontiers, opportunity implications, monitoring directions, source links, and preliminary-evidence caveats | A natural three-item job returned three unique current records with stable identities/direct URLs and now feeds explicit Business Research and Opportunity Scout handoff packets | Read-only SQLite mode, manager review pass, zero OpenAI requests, and no-post/no-modify boundary proven | Contract complete. Full-text model synthesis remains optional and must preserve method/publication-status caveats. |

## Current Repeatable Evidence

### Representative natural-language workflow evidence

The following five workflows already satisfy the reusable natural-ask evidence
bar without requiring the operator to paste provider IDs into follow-ups:

| Job | Evidence | Safety and cleanup |
|---|---|---|
| HJ-015 RSS context | Natural WorkItem route, typed live read, useful cross-item synthesis, source links, and manager review | Zero model requests; no post or mutation |
| HJ-016 Preprints context | Natural WorkItem route, three unique ranked records, caveated synthesis, source links, and downstream handoffs | Zero model requests; no post or mutation |
| HJ-022 Zotero note | Natural create, same-note revision, version-aware verification, delete, and absence in one SDK session | Marked test object only; no duplicate or orphan |
| HJ-023 Google Sheet | Natural create, append, same-row update, verification, and trash while session context retains identity | No share, send, post, duplicate, or orphan |
| HJ-026 Calendar | Natural Slack reference resolves one active event without an ID, updates the same event, and returns one provider-verified receipt | Zero model requests; no Gmail/Outreach detour or duplicate Slack final |

These five proofs establish the specialist/provider baseline, not the whole KBA
operationalization goal. Remaining acceptance is manager-led stateful breadth,
delegated provider execution through the manager entrypoint, and one direct plus
one connector-backed graph-worthy Slack proof. Existing provider primitives
should be reused rather than rerun.

For this scorecard, "without developer intervention" means the passing path did
not require code edits, manually injected provider IDs, direct state repair, or
manual artifact cleanup between its successful natural-request steps. Normal
operator follow-ups in the same retained session are part of the natural ask,
not developer intervention. HJ-015 and HJ-016 are single natural reads; HJ-022
and HJ-023 retain same-object identity in their sessions; HJ-026 resolves the
event from a natural Slack reference. Their recorded passing paths meet this
definition, while the remaining manager and Slack rows stay explicitly open.

ANU-222 now has a separate executable 12-scenario manager gate. The first
2026-07-11 run passed all scenarios with zero OpenAI requests, live connectors,
or external side effects. It proves low-friction direct ownership, broad-goal
Chief ownership, explicit route correction, ambiguous-object blocking,
selected Gmail identity propagation, newest-instruction precedence, saved-state
resume without repeated specialists, contradictory-source review, scoped
Airtable write gating, stale-write reversal, typed context handoffs, and useful
completion review without false blocking. Live model comparison remains a later
bounded quality proof, not a prerequisite for these deterministic invariants.

ANU-223 now has a separate five-provider pre-live delegation registry and
scorecard. Calendar and Gmail have joined manager-entry plus provider-operation
proof and should be reused. Airtable, Google Workspace, and Zotero each have
green manager-owner selection and green provider lifecycle/identity/cleanup
evidence, but the manager-to-provider join is not yet proven; they remain the
only candidates for a bounded live delegation batch. This 2/5 joined result is
intentional evidence separation, not a partial pass disguised as completion.

The 2026-07-11 Airtable Stage A run adds partial joined evidence: a natural
Chief request selected Airtable and completed one exact marked create/update/
delete lifecycle with provider read-back and confirmed absence. It is not yet a
joined pass because execution used six model requests after the CLI accepted a
four-request ceiling, and the original receipt exposed provider IDs. The
delegated-route budget estimator and provider-ID-free renderer are now fixed
offline. Airtable remains pending one separately approved capped rerun; Workspace
and Zotero remain unrun.

The remaining Slack goal now has an exact two-probe readiness registry. The
direct Business Research and connector-backed Gmail→Research→Outreach graph
pre-live evidence both pass, but live Slack evidence remains 0/2. The scorecard
requires permalinks, local run/WorkItem IDs, answer-first copy, visible sources
or source-limit language, one final response, trace/usage/cost evidence, and no
unintended side effects. No fixture result can mark these live rows complete.

- ANU-221 Gmail mailbox-state execution now passes on one real marked provider
  message. A zero-model lifecycle verified label add/remove, unread/read,
  star/unstar, important/not-important, archive/unarchive, trash/restore, and
  restoration of the original state across 12 provider writes. One direct
  `gpt-5.4-mini` request then interpreted a natural reversible star ask and
  drove verified star/unstar execution. A two-request Chief -> Gmail/Outreach
  workflow preserved role ownership, composed source-bounded copy, created and
  updated one exact marked draft, and sent it once to the configured approved
  test recipient with SENT read-back and draft-absence verification. No message
  body or recipient was persisted in receipts; ordinary send, bulk mutation,
  spam, and permanent deletion remain unavailable. The three-request batch is
  fully consumed.
- `npm run test:agent-execution-tiers`: 17 simple, 22 intermediate, and 11
  advanced framework cases; zero model requests.
- Fake-model Agents SDK execution now proves typed read-tool selection and
  tool-output consumption for Airtable schema, scoped Google Drive search,
  Zotero metadata, RSS history, and Preprints history without provider model
  credentials. This is agent-execution evidence, not live-model reasoning.
- Airtable, Google Workspace, and Zotero fake-model SDK runs also select their
  guarded write-preview tools with exact markers and approval references,
  execute only `live=false`, consume the returned preview, and preserve the
  registered executed-result field. No provider write occurs in this proof.
- Local Agents SDK session regressions prove that Gmail revision receives the
  prior draft and preserves message identity, while Airtable, Workspace, and
  Zotero follow-ups preserve the prior record/file/item identity and select an
  update tool instead of a duplicate create. These are fake-model continuation
  proofs; live-model continuation remains in the approved batch.
- Business Research, Opportunity Scout, and Outreach Composer now have direct
  fake-model SDK proof that their natural asks select and consume a meaningful
  domain helper: source deduplication/ranking, deterministic opportunity
  scoring, and unsupported-claim validation respectively. Gmail, Chief, and
  the five context agents already had executable typed-tool proof; Orchestrator
  remains a control-plane routing/review contract rather than a provider owner.
- `npm run test:ai-agents-workflow:no-live`: 609 workflow and current-context
  contract tests, seven
  LangGraph scenarios, 36/36 Slack routes, and ANU-60 preflight; zero live
  model/search/connector calls.
- The 2026-07-11 baseline also proves the low-friction manager contract: a
  complete explicitly named read-only context-agent ask routes directly to its
  owner rather than receiving an unnecessary Chief-of-Staff wrapper. Missing
  provider context returns the owning agent's precise blocker. Manager review
  remains advisory for optional quality gaps and blocks only authoritative
  safety, identity, evidence, or materially off-target failures.
- Treat that 2026-07-11 result as the reusable shared offline baseline. Run
  focused tests for ordinary agent fixes. Repeat the complete no-live gate only
  after shared routing, WorkItem/LangGraph, Slack bridge/rendering, common
  schema, or SDK runtime changes; before broader live validation; or before a
  publish/readiness milestone.
- `scripts/run_airtable_test_record_lifecycle.py`: marked record
  create/read/update/read/delete/absence proof.
- `scripts/run_zotero_test_note_lifecycle.py`: marked note
  create/read/update/read/delete/absence proof with provider version handling.
- `scripts/run_zotero_test_library_lifecycle.py`: one marked collection and one
  marked webpage item create/read/update/read/delete/absence proof, including
  tag and membership verification, non-empty collection refusal, separate live
  gate, atomic receipt, and item-first cleanup. The 2026-07-11 live run passed
  with collection `RHV94RUX`, item `Q4ADE8SW`, zero OpenAI requests, and no
  orphan.
- `scripts/run_google_sheet_test_lifecycle.py`: marked Sheet and row lifecycle
  through Drive trash verification.
- `scripts/run_announcement_context_read_validation.py`: content-safe RSS and
  Preprints read receipts. On 2026-07-10 the base KBA database contained no
  matching records. The explicit linked Keystone context configuration then
  resolved the read-only discovery store: Preprints returned five bounded
  records with five identities and five source/publication references. With the
  separate explicit RSS live-read gate enabled, the same receipt returned five
  `#announcements` digest items with five stable IDs and five direct source URLs.
- HJ-016 now also passes through the natural agent framework: Orchestrator routed
  the request to `preprints_context_agent`, the typed linked-store read returned
  three unique ranked papers after version deduplication, and deterministic
  manager review passed at 88/100. The result included direct source URLs and
  preliminary-evidence caveats, used zero OpenAI requests, and performed no post
  or source mutation.
- HJ-015 now passes through the natural agent framework and live read-only Slack
  history provider. The first run exposed an off-topic recency fallback; the
  shared ranker now applies a Keystone clinical-AI, behavioral-health,
  implementation, governance, and market-signal relevance profile. The rerun
  returned five source-linked items, visible themes, dates and current-status
  caveats, passed manager review at 88/100, used zero OpenAI requests, and made
  no Slack post or mutation.
- HJ-011 now passes as one live-model-selected provider lifecycle over synthetic
  data. `gpt-5.4-mini` created one marked Business Expenses record, reused the
  exact returned ID for update, and selected the dedicated test-delete tool.
  All three tool receipts passed provider read-back, deletion reported
  `record_absent_after=true`, and a separate exact-ID lookup returned zero
  records. The run stayed within the six-turn ceiling and made no duplicate or
  unrelated mutation. The legacy CLI usage lookup missed SDK context-wrapper
  usage and two side-effect fields were stale; focused regressions now repair
  usage extraction, bounded tool receipts, approval refs, and write reporting.
  The completed run is inferred to have four serial model requests from three
  identity-dependent tool turns, but Admin Usage returned zero and billing
  remained at `$3.86`, so exact provider request/token/cost evidence is still
  pending rather than claimed.
- A follow-up zero-model HJ-011 provider lifecycle populated all configured
  writable Business Expenses scalar types, linked a synthetic PDF receipt into
  `Attachments`, changed every scalar on the same record, and verified deletion
  and absence. It exposed trailing whitespace in provider select/checkbox names;
  schema mapping and read-back now reconcile exact provider names generically
  while retaining cleaned prompt-safe labels. Six focused regressions pass.
- Two later natural HJ-011 learning runs captured complete SDK usage. Run one
  used five requests and correctly resolved schema/target but silently called
  create with `live=false`; no mutation occurred. After a prompt repair, run
  two used six requests and estimated `$0.0486969`: live create, same-ID update,
  marked delete, and final absence all passed, but the model passed an HTTPS URL
  to the local-file upload tool, so attachment remained incomplete. An
  independent exact-ID read returned zero records. The shared operating prompt
  now requires directly selected approved write asks to call typed tools with
  `live=true`, while Python gates remain authoritative, and attachment tool
  descriptions distinguish HTTPS URLs from local paths. The cross-agent
  prompt/registry gate passes 102 tests. No third model run occurred because
  this stage reached 11 requests, beyond the 10-request stop.
- HJ-022 now passes as a three-checkpoint natural Zotero lifecycle in one local
  SDK session. `gpt-5.4-mini` created one uniquely marked standalone note,
  retained its provider key and version internally, updated the same item with
  version-aware verification and no duplication, then selected exact-key
  deletion and verified provider absence with an independent provider read. The three runs
  used six requests, 194,688 total tokens, and a maintained local estimate of
  `$0.04717935`; the Chrome credit balance moved from `$3.31` to `$3.27`.
  Search, import, Workspace writes, sends, posts, and unrelated mutations were
  disabled. The run exposed one reporting defect: successful
  `delete_test_note` was not classified as an external write. The shared
  classifier and bounded receipt now include that operation, exact item key,
  and `item_absent_after`, with focused regression coverage.
- The current active-grant/RFP reproduction now blocks a retained
  `fixture://` candidate with
  `manager_loop_current_opportunity_evidence_missing`; it no longer reports
  the fixture as a completed source-backed match. Source-provided internal
  advisory tasks and strict no-match results remain valid.
- `scripts/run_gmail_test_draft_lifecycle.py`: on 2026-07-10, one marked Gmail
  draft passed create/read, same-ID update/read, exact-ID delete, and absence
  verification against the scoped agent mailbox. The receipt recorded
  `sent=false`, `send_enabled=false`, and `openai_requests=0`; no draft remained.
  This proves the provider lifecycle, but not yet model-selected execution.
- The first two approved Gmail model checks ran serially on 2026-07-10 with
  `gpt-5.4-mini`, live Gmail reads, no live search, and no provider writes.
  The selected-thread scenario passed immediately. The initial today-only
  batch exposed an over-escalated routine account-setup notification plus
  bucket/priority contradictions; shared prompt and Pydantic repairs followed.
  The final bounded stability rerun now also passes: all four fixture messages
  were assigned exactly once, routine content stayed out of urgent, actions
  remained useful, and no draft/write/send occurred. That final run used one
  request, 29,002 input plus 851 output tokens, and a maintained `$0.025581`
  estimate with local trace evidence. Same-session revision remains partial
  for the separate false missing-draft narrative described above.
- The two Gmail runs used exactly two OpenAI requests. Local maintained-pricing
  estimates total `$0.060189`; the refreshed organization credit balance moved
  from `$4.16` before the runs to `$4.10` after them. Current-day Admin Costs
  aggregation had not posted yet, so the exact invoice amount remains
  provider-pending.
- The read-only review of 15 recent Slack roots is summarized in
  `docs/HUMAN_AGENT_EXECUTION_JOBS.md`; its strongest positive evidence is one
  direct Business Research run, while its main negative evidence is weak graph
  job completion and source propagation.
- HJ-032 now proves reversible Slack provider writes independently of answer
  quality: one exact-channel marked message was posted, read, updated on the
  same timestamp, reread, deleted, and confirmed absent. Marker, approval,
  separate live gate, channel allowlist, and ambiguous-post recovery are tested.
  Zero OpenAI requests or search occurred. ANU-60's natural/model/graph Slack
  probes remain a separate acceptance layer.

## Priority Order

1. The latest eight-request sequential ceiling is fully consumed by the passing
   Google Workspace natural Sheet lifecycle. Do not make another OpenAI call
   until a new ceiling is approved and the logged-in billing page is refreshed.
2. Extend the new fixture-only current-opportunity completion gate to reject
   contradictory provider-status claims and weak generic portal pages in live
   traces.
3. Treat RSS and Preprints natural provider execution as proven without a model.
   Later model synthesis should be used only if a bounded comparison shows it
   adds useful cross-item or item-level judgment.
4. Airtable, Zotero, and Google Workspace Sheet joined natural-language
   lifecycles are proven. The Workspace run used four serial two-turn
   `gpt-5.4-mini` checkpoints in one SDK session: create, append, same-key
   update, and trash. It consumed exactly eight requests, 255,624 input tokens
   including 217,088 cached, 3,507 output tokens, and a maintained `$0.0609651`
   estimate. All four agent runs persisted as IDs 2437-2440. Billing remained
   displayed at `$3.27`; Admin Usage had not yet ingested the run window.
5. Workspace selected-file provider preparation passes with zero model calls.
   A bounded search found no Google Doc named like a proposal in the direct
   KNIOps root, so HJ-012 must not fabricate one. A scoped folder list resolved
   `README.doc`; the typed Docs read verified its exact identity and returned
   the complete 1,096-character body without truncation or mutation. Read-tool
   receipts now expose operation, safe identity, item/row/character counts, and
   truncation state while excluding file listings and body text.
6. Run bounded Business Research, Opportunity Scout, Outreach, and Chief model
   proofs, fixing the first shared failure before expanding the batch.

Every proof record must include the raw request, route, backend, typed tools,
provider IDs or source URLs, structured result, reasoning assessment, safety
outcome, cleanup, request/usage/cost evidence when applicable, and the focused
fix or next blocker.
