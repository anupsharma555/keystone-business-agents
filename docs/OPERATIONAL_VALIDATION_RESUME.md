# Operational Validation Resume Context

Updated: 2026-07-10

## Objective

Continue realistic natural-language validation of Keystone Business Agents in
direct single-agent and LangGraph execution. Keep Promptfoo deferred. Run live
model checks one at a time, fix between runs, preserve no-send/no-post and
approval boundaries, and record meaningful milestones rather than every
intermediate step in Linear.

## Current Gmail Evidence

- **Selected-thread summary and reply guidance: pass.** One bounded Gmail
  thread and one `gpt-5.4-mini` request produced an accurate thread summary and
  useful plain-text reply guidance. No Gmail draft or send occurred.
- **Today-only triage over sanitized fixtures: pass.** The final bounded
  `gpt-5.4-mini` run preserved the exact today/one-day scope, placed two
  relevant opportunities in `important`, the newsletter in `can_wait`, and
  the generic vendor pitch in `ignore`, with no draft or side effect.
- The approved one-request rerun improved the prioritization: the routine
  onboarding message moved from urgent to important. The run still failed
  structured validation because the model correctly returned reply text with
  `draft_created=false`, while the older GT-1 schema incorrectly treated that
  field as “draft text prepared” rather than “provider Gmail draft created.”
- No retry, Gmail mutation, label change, send, search, Slack post, or other
  external write occurred.

## Fixes Already Implemented

- Gmail priority prompt requires concrete security evidence before escalating
  a routine credential/account-setup link.
- Priority-grouping schema rejects contradictory bucket/priority pairs.
- GT-1 now treats `draft_created` as provider-artifact state. Plain-text
  `draft_reply` is allowed with `draft_created=false`; `draft_count` counts
  reply-text outputs.
- Test-pack reporting records the actual operator request, permits empty unused
  buckets and valid zero-draft cases, checks complete non-duplicated grouping,
  and labels provider-draft state clearly.
- SDK tracing now explicitly uses `KEYSTONE_OPENAI_API_KEY`, never the unrelated
  generic `OPENAI_API_KEY`.
- Live trace metadata scalars are converted to strings for the OpenAI trace
  exporter. This addresses the rerun's 400 error for boolean metadata.
- Priority guidance now requires a source-visible deadline, short time window,
  blocker, safety concern, or immediate consequence before using `urgent`.
  Relevance or opportunity value alone is insufficient.
- A one-request synthetic Gmail continuation retained the prior-only scheduling
  CTA, exact message/thread identity, and approved facts while producing a
  shorter, warmer draft with no provider action. The core revision behavior
  passed, but the model also contradicted itself by reporting the prior draft
  missing. Current-turn typed context now states that the session artifact
  exists, and the regression rejects this false-missing narrative; live
  confirmation of that narrow repair remains pending.
- Generic `keystone.work_item.source_bundle.v1` context files now promote
  bounded targets, Gmail identity, source refs, and research/drafting-approved
  facts into typed WorkItem state. A no-live Gmail -> Business Research ->
  Outreach graph preserves that evidence through the approval checkpoint.
  Context packets cannot self-approve external use or sending.
- Explicit `--agent orchestrator --context-file <source-bundle>` CLI requests
  now select the WorkItem graph rather than bypassing it through the direct
  Orchestrator SDK path. Source-bundle context also suppresses default live
  search.
- Live `ask` commands now support `--max-openai-requests`, which estimates the
  implicit manual planner, specialist/graph stages, configured SDK turn limits,
  and final synthesis before any model call. A command capped at one request
  now blocks the default two-request source-bundle graph with
  `openai_requests_made=0`. `--no-live-manual-plan` explicitly removes the
  planner call for bounded runs where deterministic preflight is sufficient.
- HJ-024 sent-mail style learning now passes end to end offline from the natural
  ask: bounded synthetic `SENT` samples produce a redacted pending profile;
  approval updates both the WorkItem artifact and stored profile; only the
  approved aggregate profile influences the subsequent Gmail draft. Raw bodies
  are absent from persisted profile/approval/run rows. Ordinary reply-tone
  instructions no longer trigger the style-learning branch.
- The dedicated provider path then sampled five real Gmail `SENT` messages
  read-only with **zero OpenAI requests** and no Gmail mutation. The first pass
  revealed sample-quality drift from long article/finance content. The shared
  profiler now excludes finance-review, long (>220-word), and quoted/forwarded
  samples. A ten-message signoff-only diagnostic confirmed `Sincerely` in seven
  sent messages; `Best` was an incorrect default after the parser missed inline
  `Sincerely, Anup` in normalized one-line text. The corrected pending v3
  profile uses three of five samples and records `Hi {name},`, `Sincerely,`, 13-word average sentences, varied length,
  medium directness, formal tone, and a soft-question CTA. Database inspection
  confirmed no raw-body keys and `raw_sent_email_bodies_included=false` for
  all pending audit rows. Human approval and draft comparison remain pending.
- HJ-016 Preprints now passes through the natural agent framework without a
  model call. Orchestrator selected `preprints_context_agent`; its first-class
  WorkItem route consumed the linked read-only discovery store, removed older
  duplicate versions, returned three source-linked papers with visible
  preliminary-evidence caveats, and passed deterministic manager review at
  88/100. The receipt records zero OpenAI requests, no Slack post, and no source
  mutation.
- HJ-015 RSS now passes through the analogous natural agent framework and the
  explicitly dual-gated read-only Slack history provider. Its first live-read
  result was correctly held partial because recency admitted weak cancer and
  genomics items. A shared Keystone domain-relevance score repaired the ranking;
  the rerun returned five source-linked clinical-AI, behavioral-health,
  implementation, funding, and neuroinformatics signals with themes, dates, and
  visible current-status caveats. Deterministic manager review passed at 88/100;
  zero OpenAI requests, no Slack post, and no mutation occurred.
- HJ-011 Airtable now passes as a joined live-model/provider lifecycle using
  synthetic data only. The direct `gpt-5.4-mini` agent selected create, update,
  and the dedicated marked test-record delete tools; all used the same exact
  provider ID and separate approval references. Tool receipts verified both
  writes and `record_absent_after=true`; an independent exact-ID lookup returned
  zero records. No existing finance record was read, no duplicate was created,
  and no unrelated mutation occurred. The request guard enforced six maximum
  SDK turns. The completed run's three identity-dependent tool calls imply four
  serial model requests, but the legacy CLI missed context-wrapper usage, Admin
  Usage showed zero, and Chrome remained at `$3.86`; exact tokens/cost are
  therefore provider-pending. Offline repairs now extract SDK usage correctly
  for future runs and report bounded tool receipts, approval references, and
  actual external-write state without leaking record content.
- HJ-011's expanded typed provider proof also passes with zero model calls. One
  marked expense exercised text, date, single-select, multi-select, currency,
  checkbox, and multiple-attachment fields; a synthetic PDF receipt was linked,
  all scalar values were modified and read back, and cleanup verified absence.
  Provider field-name whitespace is now reconciled without phrase- or
  field-specific shortcuts. Computed/formula fields remain read-only.
- HJ-011 natural attachment execution is still partial after two serial learning
  runs. The first used five requests and downgraded create to dry-run; the
  second used six requests after the live-write repair and successfully created,
  updated, and deleted the same exact record with read-back, but chose the
  local-file upload tool for the HTTPS receipt URL. Independent lookup confirmed
  zero records remained. Billing moved from `$3.82` before this pair to `$3.73`
  after it; local estimates were `$0.04605195` and `$0.0486969`. The shared
  direct-write policy and URL-vs-local-path tool descriptions are fixed offline,
  but do not run another model call until a new post-10-request stage is set.
- All registered write-capable agents now receive one shared rule: directly
  selected, exact, approved live execution asks call their typed write tools with
  `live=true`; Python identity/account/approval gates remain authoritative;
  nested/advisory calls stay read-only; sends/posts/publication remain separately
  gated. The shared prompt/registry gate passes 102 tests.
- Future validation should progress one real operating role at a time through
  diverse direct jobs, ambiguity/correction cases, continuation, modification,
  verification, and cleanup. After each agent is dependable, run genuine
  multi-agent jobs and then repeat the same asks in `#ai-agents-workflow`, using
  backend receipts as primary evidence and Slack output/permalinks as acceptance
  evidence. Promptfoo remains deferred.
- Gmail has operator authorization for at most two clearly labeled synthetic
  validation sends to one approved test recipient. Implement a reviewed exact-
  recipient send tool rather than bypassing the current no-send architecture;
  require draft/read/modify first, provider send receipt, sent-copy read-back,
  and no other recipient.
- HJ-022 Zotero now passes live-model execution end to end. Three serial
  two-request checkpoints in one SDK session created one uniquely marked note,
  updated the exact same item from provider version 3520 to 3521, deleted it,
  and verified absence; an independent exact-key read returned HTTP 404. Total
  usage was six requests and 194,688 tokens, with a maintained local estimate
  of `$0.04717935`. Chrome billing moved from `$3.31` before the lifecycle to
  `$3.27` after it. No search, import, Workspace write, send, post, or unrelated
  mutation occurred. A focused fix now classifies `delete_test_note` as an
  external write and preserves exact key/absence evidence in bounded receipts.
- Zotero structural provider execution now also passes with zero model calls:
  one `KBA_TEST_COLLECTION` and one `KBA_TEST_ITEM` webpage were created,
  updated by exact keys/versions, verified for tags and membership, deleted
  item-first, and independently confirmed absent. The reusable atomic receipt
  is `artifacts/test-pack/zotero-test-library-lifecycle.json`; no orphan remains.
- Clinical AI presentation reads now pass without a model. Direct KNIOps Drive
  inventory correctly reports no Slides/PowerPoint target. A separate
  allowlisted local-library search resolved the newest reviewed Clinical
  Workflow AI deck from title/date terms, then extracted a bounded three-slide
  sample with speaker notes, snapshot IDs, relative provenance, modified time,
  checksum, truncation status, and immutable-parent evidence. The exact read
  also verified all 16 slides. No copy, write, send, post, or model call ran.
- The first derived-slide lifecycle now passes. Slide 2 of that same deck was
  rendered into a marked PNG and single-page PDF with verified file signatures,
  output checksums, snapshot identity, and unchanged parent checksum/mtime.
  Both marked artifacts were deleted and confirmed absent. The initial renderer
  attempt created nothing because LibreOffice lacked a writable profile; the
  tool now uses a fresh temporary profile per run and the focused regression is
  green.
- The first cross-system slide destination now passes. A marked PNG derived
  from slide 2 was attached to one exact allowlisted Gmail draft, read back and
  verified by filename, byte size, and SHA-256, then retained while the same
  draft ID received an updated subject/body. The draft was deleted and verified
  absent, the local PNG was removed, and the parent deck remained unchanged.
  No email was sent and no OpenAI request ran. Google Doc embed and Airtable
  attachment remain separate destination boundaries.
- Google Doc image embedding is not currently a private-byte operation. The
  Google Docs API requires the inline-image URI to be publicly accessible, so
  KBA must not silently publish a Clinical AI slide or substitute a text link.
  This destination is blocked pending an explicitly approved staging policy or
  another authenticated insertion mechanism.
- The first Airtable derived-slide upload attempt stopped before attachment work
  because the initial marked-record create timed out during TLS negotiation.
  A read-only exact-marker query confirmed zero matching records, and the local
  PNG was deleted. The upload adapter now requires provider read-back of exact
  filename, byte size, and one-count increase; the lifecycle runner also
  performs exact-marker orphan recovery after ambiguous create failures. Do not
  hot-retry this write in the same validation window. In the next provider
  window, a read-only schema check passed and the bounded retry completed:
  one marked record was created, one private PNG upload increased the attachment
  count from zero to one with exact filename and byte-size matches, the same
  record ID was updated, the record was deleted and confirmed absent, and the
  local PNG was removed. Zero OpenAI requests, search, sends, or posts occurred.
- HJ-025 now passes for one exact authorized synthetic email send. The dedicated
  path created a dual-marked test draft, read it back, updated the same draft,
  sent exactly once to the configured recipient, read the exact SENT message,
  and verified recipient, subject, body, message identity, SENT label, and draft
  absence. The receipt persists no recipient/account address. Zero OpenAI
  requests, retries, other recipients, or cleanup failures occurred. Ordinary
  agent email sending remains unavailable.
- HJ-032 now passes at the Slack provider/tool layer. One exact-channel marked
  message in `ai-agents-workflow` was posted, read back, updated on the same
  timestamp, reread, deleted, and confirmed absent. The contract requires a
  separate live gate, exact channel allowlist, approval references, and test
  marker, and it can recover one exact marked message after an ambiguous post
  response. Zero OpenAI requests or search occurred. This does not satisfy
  ANU-60's separate answer-first/model/graph Slack probes.
  The new catalogue row initially exposed a routing defect: an internal channel
  mutation was sent to Outreach merely because it contained `post`. Internal
  `ai-agents-workflow` message/thread create, edit, and delete asks now stay with
  Chief of Staff; external outreach-message asks still route to Outreach.
- Google Workspace structural folder R/W/M now passes with zero OpenAI calls.
  One uniquely marked empty folder under `KNIOps` was created, read back by
  provider ID, renamed using that same internal ID, read back again, moved to
  Drive trash, and verified with `trashed=true`. The initial live verifier used
  the raw Google key `mimeType` instead of KBA's normalized `mime_type`; cleanup
  still succeeded and left no orphan. The corrected rerun passed all six
  create/rename/trash receipts. The reusable lifecycle runner persists its JSON
  receipt atomically and cleans up after mid-run failures. Focused Workspace
  validation passes 37 tests plus Ruff and `git diff --check`.
- Google Workspace disposable Doc R/W/M now also passes with zero OpenAI calls.
  One `KBA_TEST_DOC` was created under `KNIOps`, read back by exact provider ID,
  replaced in place with modified body text, read back again, moved to Drive
  trash through the new exact scoped `google_doc_trash` tool, and independently
  verified with `trashed=true`. The dry-run write receipt was corrected from the
  misleading `list_folder` operation to `write_doc`. The Doc runner persists an
  atomic receipt and cleans up after mid-run failure. Combined focused Workspace
  verification now passes 187 tests plus Ruff and `git diff --check`.
- Fake-model Agents SDK coverage now bridges the same structural Workspace
  operations from natural asks: Doc create, same-ID update, exact Doc trash,
  folder create, same-ID rename, and exact empty-folder trash. Every call stays
  `live=false`, exposes its typed receipt to the next model turn, preserves the
  prior provider identity in SQLite session history, and rejects duplicate
  creation language. The combined Workspace/SDK gate passes 271 tests plus Ruff
  and `git diff --check`; no OpenAI or provider call occurred in this layer.
- Airtable's HTTPS-versus-local attachment regression now passes through the
  fake-model SDK path: a natural receipt URL ask selects
  `airtable_link_attachment`, supplies the exact record/field/approval context,
  and returns the typed preview to the next model turn without invoking
  `airtable_upload_attachment`. The tool now permits a safe no-live preview when
  default dry-run schema has no fields, recording
  `schema_validation=pending_live_schema`; live execution still blocks unless
  the provider schema confirms `multipleAttachments`. The focused SDK/Chief/
  receipt gate passes 239 tests with one optional skip plus Ruff and
  `git diff --check`. No API or provider mutation occurred.
- The joined live-model HTTPS proof now also passes. One marked synthetic
  Business Expenses record was created deterministically, then
  `gpt-5.4-mini` selected `airtable_get_base_schema` followed by
  `airtable_link_attachment`; it never selected the local-file upload tool.
  Independent provider read-back found exactly one attachment, and marked
  cleanup proved provider deletion plus record absence. Usage was 3 requests,
  101,756 input tokens including 65,024 cached, 954 output tokens, zero retries,
  and a maintained `$0.0367188` estimate. The first receipt rendered absence
  from the wrong key even though deletion verification had passed; the runner
  now requires and reports `record_absent_after` explicitly, without rerunning
  the model.
- Airtable structural base/table/field/view validation is explicitly separated
  from ANU-198 record/attachment scope and tracked under ANU-211. The corrected
  readiness probe now treats absent `/meta/whoami` scope enumeration as
  informational, verifies the configured base through `/meta/bases`, and labels
  the remaining write scope as unverified until a marked create is attempted.
  With the exact workspace and reversible manual-trash cleanup plan supplied,
  the preflight passed. Two exact `KBA_TEST_BASE` create attempts then returned
  Airtable HTTP 403 `INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND` before a base ID was
  issued. A refreshed authenticated workspace view confirmed zero marked bases;
  no cleanup was needed. The structural runner now returns a typed sanitized
  permission blocker instead of a traceback. Authenticated PAT inspection later
  confirmed that `schema.bases:write`, `schema.bases:read`, and
  `workspacesAndBases:read` are already present. The blocker is resource scope:
  the PAT can access only the existing finance base, while Airtable offers no
  target-workspace-only choice in this editor; new-base creation requires `Add
  all resources`. Do not broaden that PAT without an explicit operator choice,
  or create a dedicated structural-validation PAT with only schema/metadata
  scopes and all-resource coverage. Zero OpenAI requests and zero Airtable writes
  were recorded for structural validation.
- ANU-198's final read/synthesis dimension passes over one real provider-backed
  synthetic Business Expenses record. Python created and read back an exact
  marked row, passed a bounded record packet to Airtable Context with tools
  detached, and retained arithmetic/identity/write authority outside the model.
  One `gpt-5.4-mini` request correctly summarized `$42.00` plus `$3.36` tax as
  `$45.36`, identified the Software category and debit-card payment method,
  flagged the missing receipt, and recommended follow-up. Any review plan stayed
  approval-gated; the model executed no tool or write. Python deleted the exact
  record, and a fresh provider query found zero synthesis markers. Usage was
  29,209 input and 745 output tokens with a `$0.02525925` maintained estimate.
  All five newly approved OpenAI calls are now consumed.

## Verification And Cost Ledger

- Latest focused graph/CLI gate after the source-bundle and request-budget
  fixes: **455 passed**.
- Latest Gmail/style/WorkItem focused gate: **444 passed**.
- Reusable shared baseline rerun after those final fixes: **572 tests**, **7/7 graph scenarios**,
  **36/36 Slack routes**, **111 ANU-60 tests**, and **8 sibling bridge tests**.
- Targeted Ruff and `git diff --check` also pass. This complete baseline now
  includes the final `draft_created` and trace-metadata fixes. Do not rerun it
  for small follow-ups; use focused tests unless shared routing, graph, Slack
  bridge, common schema, or SDK runtime changes again.
- First two successful model responses: two requests, maintained local estimate
  `$0.060189`; billing balance refreshed from `$4.16` to `$4.10`.
- Task 7 fixture-validation sequence used four serial requests in this phase.
  The final passing run used 29,002 input and 851 output tokens with a local
  estimate of `$0.025581`; local trace IDs were
  `trace_b619632549a046deb60d5eea96d5a53f` and
  `sdk_run_83f5e3f1a0ab2a82`. Billing displayed `$4.04` before and after the
  final run. Four of the eight approved sequential runs remain.
- The continuation phase used two model requests after one credential-readiness
  attempt stopped before the network. The second model response passed all
  identity, revision, fact, CTA, approval, and no-side-effect checks, but the
  false missing-context narrative keeps the overall continuation proof partial.
  Across the initial Gmail sequence and these serial repairs, nine requests have
  now been consumed against the 10-request hard stop; do not start the
  multi-request LangGraph proof under the remaining one-request allowance.
- Chrome billing showed `$3.96` immediately before the final continuation run.
  Post-run Chrome refresh lost authentication and the Admin Usage fallback
  returned HTTP 503, so no newer provider balance is claimed. The final run's
  maintained local estimate is `$0.0254715` with trace
  `trace_769e07c9322e49be9c8bf83ed4eeb448`.
- Before the supplied-material attempt, Chrome billing refreshed successfully
  to **$3.91**. The attempted command failed operationally: the explicit
  Orchestrator override bypassed the WorkItem graph, so the context packet was
  not consumed and the model incorrectly reported it missing. The attempt made
  two requests (manual planner plus Orchestrator). The planner used 13,374
  input and 316 output tokens; the main Orchestrator response used 44,121 input
  and 1,192 output tokens. Their combined maintained local estimate is
  **$0.04990725**. No search or external write occurred.
  The post-run balance still displayed **$3.91**. That historical batch reached
  **11**, exceeding its 10-request stop ceiling. The operator later approved a
  new eight-run sequential ceiling; two requests from that new batch produced
  the corrected fixed-source graph pass, with the clean second run persisting
  exact usage and maintained cost evidence.
- The prior approved eight-request batch was fully consumed by the recorded
  validation work. On 2026-07-11 the operator approved a new ceiling of eight
  OpenAI calls for future runs. One has now been consumed: the first attempted
  weekly-packet launch was rejected before process start because the bounded
  source bundle contains private Slack/Gmail summaries, Calendar events, and
  completed-run details. Explicit approval to transmit that bounded private
  packet to OpenAI is still required before the one-call synthesis can run.
- The first successful call under the new ceiling closed L174-03. A refreshed
  Zotero provider read again selected top-level `journalArticle` item
  `9RFK7932`; normalized DOI enrichment returned the Crossref abstract packet.
  One no-tool `gpt-5.4-mini` request produced a source-faithful claim/method/
  practical-relevance summary and explicitly limited the evidence to abstract
  metadata. Usage was 31,381 input and 1,062 output tokens; maintained estimate
  `$0.02831475`; Chrome billing remained `$3.20` before and after. No search,
  retry, send, post, connector mutation, or provider write occurred. Seven
  approved calls remained after that run.
- The second successful call under the new ceiling closed L174-18. A direct
  no-search extraction of NeuroFlow's official homepage supplied current
  first-party company context, while the exact Zotero/Crossref packet retained
  its abstract-only boundary. One no-tool `gpt-5.4-mini` request kept the
  company/customer/product takeaways unchanged, added only an adjacent
  interoperability/data-pipeline lens, and explicitly rejected the article as
  evidence about NeuroFlow itself. Usage was 31,527 input and 1,402 output
  tokens; maintained estimate `$0.02995425`; Chrome billing remained `$3.20`
  before and after. No search, retry, send, post, connector mutation, or
  provider write occurred. Six approved calls remain.
- The third successful call under the new ceiling closed L174-11. Direct
  no-search extraction of the official NeuroFlow, Headway, and Spring Health
  homepages supplied three current first-party packets. One no-tool
  `gpt-5.4-mini` request returned exactly two paragraphs with the shared pattern
  first and distinct company positioning second, included all three URLs, and
  clearly labeled the evidence as unverified first-party claims. Usage was
  32,257 input tokens including 25,344 cached and 1,016 output tokens;
  maintained estimate `$0.01165755`; Chrome billing moved from `$3.20` to
  `$3.18`. No search, retry, send, post, connector mutation, or provider write
  occurred. Five approved calls remain.
- The fourth call under the new ceiling exercised Opportunity Scout over two
  directly read official Devpost pages for `Hack for Humanity | Summer 2026`.
  The logged-in billing page showed `$3.13` at 2026-07-11 00:46 EDT before the
  request and `$3.13` after it. A local launch first stopped before network
  because the ad hoc process had not loaded `.env`; the corrected process used
  `KEYSTONE_OPENAI_API_KEY` and made exactly one `gpt-5.4-mini` request. The
  agent had zero attached tools, no search, no provider write, and no retry.
  Usage was 34,692 input and 3,914 output tokens; the maintained estimate was
  `$0.043632`, below the scenario's `$0.05` ceiling. The result correctly
  identified the event as upcoming rather than currently open, preserved the
  September 4 at 11:45pm EDT deadline, treated fit as conditional on a concrete
  prototype/portfolio objective, rejected consulting/revenue and cash-prize
  inferences, and listed missing eligibility evidence. It remains **PARTIAL**
  because the model called the hackathon an open-source repository opportunity
  and inferred likely U.S. relevance from Rutgers affiliation despite unknown
  geography. The shared schema/prompt now includes `hackathon or challenge
  opportunity`, forbids classifying an event from its required GitHub artifact,
  and forbids affiliation-based geography inference. The focused contract gate
  passes 218 tests plus Ruff and `git diff --check`. Four approved calls remain;
  the next bounded proof is one same-packet rerun after this fix, not a broader
  opportunity search.
- The fifth call under the new ceiling reran the same Opportunity Scout packet
  after the event-type and geography repair. Billing refreshed to `$3.09`
  before the request and remained `$3.09` afterward. Exactly one
  `gpt-5.4-mini` request used 34,849 input and 4,659 output tokens; the
  maintained estimate was `$0.04710225`, still below `$0.05`. The result fixed
  both target defects: it returned `hackathon or challenge opportunity`, kept
  geography explicitly unknown, marked the submission window not yet open,
  and retained the conservative prototype/portfolio-only recommendation. Zero
  tools, search, writes, outreach, provider mutations, or model retries ran.
  The result remains **PARTIAL** because it duplicated one identical source
  bundle and counted two pages on the same Devpost domain as two independent
  sources. The schema now deduplicates bundle identity and deterministically
  calculates independent sources by domain. The expanded focused gate passes
  221 tests plus Ruff and `git diff --check`. Three approved calls remain; do
  not spend another in this two-run stage. The next Opportunity proof is one
  later same-packet rerun requiring one bundle and one independent domain.
- The sixth call under the new ceiling tested that normalization. Billing was
  `$3.04` before and after. One `gpt-5.4-mini` request used 34,849 input and
  4,781 output tokens; maintained estimate `$0.04765125`. Type, geography,
  timing, fit, eligibility uncertainty, no-cash/no-consulting boundaries, and
  the record-level one-bundle/one-domain result all passed. The agent still
  emitted top-level planned query strings even though `search_provider=none`
  and `raw_search_result_count=0`, and its top-level bundle/result summaries
  claimed two independent sources for two pages on the same Devpost domain.
  No search, tool, write, outreach, provider mutation, or retry occurred. The
  result remains **PARTIAL**. Result-level normalization now clears unexecuted
  queries, deduplicates top-level bundle identity, and recalculates top-level
  source independence by domain. The focused contract gate passes 222 tests
  plus Ruff and `git diff --check`. Two approved calls remain. Do not run a
  fourth Opportunity call in this stage; reprove the complete normalized output
  only in a later validation batch. The prior ad hoc run did not persist its
  exact two-page input packet, so do not invent or approximate the Devpost URLs
  for that rerun. `run_opportunity_scout.py` now supports `--result-output` and
  atomically saves the complete SDK payload before console rendering. Recover
  or rebuild the exact packet from verified direct sources first, then use the
  persisted candidate contract in
  `artifacts/test-pack/next-live-opportunity-normalization-plan.json` under a
  fresh request ceiling and billing refresh. A later read-only open-tab recovery
  found one authoritative page identity,
  `https://hack-for-humanity-summer-26.devpost.com/details/dates`, titled
  `Hack for Humanity | Summer 2026: AI for Mental and Physical Health -
  Devpost`. The already-open tab timed out on bounded DOM and screenshot reads,
  no second Devpost source tab was open, and browser history was not inspected.
  Treat this as one-of-two identity recovery only, not a runnable source packet.
- A later bounded recovery completed the exact packet without search by reading
  the official event root and dates pages directly. The persisted packet records
  both URLs, one source domain, checked time, content hashes, bounded claims,
  and explicit evidence caveats. One approved no-tool `gpt-5.4-mini` run then
  used exactly one request (34,127 input and 4,756 output tokens; maintained
  estimate `$0.04699725`) with no retry, search, provider operation, outreach,
  or write. The output retained the correct event type, unknown geography,
  upcoming timing, conditional prototype/portfolio fit, and missing eligibility,
  while normalizing record, bundle, and result evidence to one independent
  domain. The initial deterministic receipt said PARTIAL only because it
  required the exact geography string `unknown` and treated explicit negated
  consulting/revenue/cash-prize boundaries as positive inferences. General
  validator regressions now accept bounded unknown wording and require explicit
  negated boundaries; offline revalidation of the unchanged saved output is
  PASS. Do not rerun this packet.
- The seventh call under the new ceiling tested Outreach Composer same-object
  revision with a fixed approved NeuroFlow/Keystone fact packet, one existing
  draft, no tools, no search, and no provider writes. Billing showed `$3.00`
  before and after. The generated copy itself met the human job: it was shorter
  and warmer, retained both approved facts and `Hi Alex`, removed the blocked
  overlap/implementation claim, used one low-pressure CTA, and ended exactly
  `Sincerely, Anup`. The run is **PARTIAL/FAIL at structured validation**:
  the full model-generated `OutreachDraft` nested an email style profile with
  `external_use` scope, while style guidance must remain drafting-only. Pydantic
  rejected the response before the SDK usage receipt was extracted; exact
  tokens/cost are therefore unknown, and no retry ran. No Gmail draft, send,
  Slack post, schedule, search, tool call, or external write occurred. The
  direct runner now has a compact constrained path matching the existing graph
  architecture: the model returns copy plus approved source IDs only, and
  Python deterministically constructs approval state, style scope, facts,
  unsupported-claim checks, and no-send controls. Focused compact/full Outreach
  coverage passes 70 tests. One approved call remains; do not spend it on an
  immediate retry after this missing-usage failure.
- The eighth and final call under that ceiling exercised the compact constrained
  Outreach path with the approved Curebase company/opportunity/contact/CRM
  fixture packet and one existing draft. The runner attached no tools and the
  request allowed no search or provider writes. The process completed, but the
  console result exceeded the calling tool's output limit and no atomic result
  artifact or usage receipt had been persisted, so the copy, source IDs, token
  usage, and deterministic wrapped output cannot be inspected. No process
  remains active and no new repository artifact was created. Billing refreshed
  at 2026-07-11 01:27 EDT and remained `$3.00`, matching the pre-call snapshot.
  Classify this as **PARTIAL/observability failure**, not a capability pass, and
  do not retry. The eight-call allowance is exhausted. A future compact rerun
  must atomically persist bounded structured output and usage before printing.
  That repair is now implemented: an optional constrained-run evidence path is
  written atomically first at `compact_received`, then replaced with a
  `completed` receipt containing normalized compact copy, deterministic full
  output, usage, cost, budget, and request-cache evidence. The full SDK
  execution file passes 78 tests; focused Ruff and `git diff --check` pass. A
  dedicated future entrypoint now wraps that contract at
  `scripts/run_outreach_compact_revision.py`: exactly one `gpt-5.4-mini`
  request, approved Curebase/context/style fixtures, zero tools/search/provider
  operations/retries, a `$0.05` ceiling, and explicit checks for shorter/warmer
  copy, approved facts/source IDs, blocked-claim removal, one CTA, exact
  `Sincerely, Anup` signoff, Python-owned approval scope, and no-send safety.
  Do not execute it until a fresh request ceiling is approved and the logged-in
  billing page is refreshed with a new balance/timestamp.
- The later persisted compact rerun is now **PASS**. Billing refreshed to
  `$2.93` at 2026-07-11T08:10:56Z, then one no-tool `gpt-5.4-mini` request used
  34,510 input and 225 output tokens with a maintained `$0.026895` estimate.
  The draft was shorter and warmer, preserved approved Curebase and Keystone
  positioning, removed the blocked overlap claim, used one low-pressure CTA,
  and ended exactly `Sincerely, Anup`. Approval remained pending; external use,
  Gmail draft creation, send, post, search, tools, provider operations, retry,
  and writes remained disabled. The initial receipt's only failed check was a
  phrase-specific demand for `clinical research`, despite the copy using the
  approved `decentralized clinical trial operations` plus `clinical AI and
  research operations` facts. The validator now checks approved source/fact
  fidelity instead of that literal phrase; the unchanged saved output passes
  offline revalidation. Do not repeat this fixture revision.
- The selected Workspace document run is now **PASS**. Billing refreshed to
  `$2.88` at 2026-07-11T08:15:49Z. Exactly three `gpt-5.4-mini` requests used
  94,446 input tokens including 61,952 cached, plus 790 output tokens; the
  maintained estimate was `$0.0325719`. The agent searched `KNIOps` for
  `README.doc`, selected the unique provider identity, read all 1,096 characters
  without truncation, and summarized the workspace purpose, folder roles,
  Slack-to-Drive operating model, and safety boundaries. No search, retry,
  provider write, share, move, trash, Gmail draft, send, or Slack post occurred.
  The initial receipt's only failed check required the literal word `purpose`
  or `organizes`; the summary's explicit `internal workspace for` wording is a
  purpose statement. A general semantic regression now accepts that form, and
  the unchanged saved output passes offline revalidation. Do not repeat this
  selected-document scenario.
- The bounded Gmail continuation recheck initially failed for a real reason:
  the local SDK session contained the exact prior structured draft, but the live
  model ignored that nested history and returned revision instructions plus a
  false missing-draft claim. The first attempt used one request and no side
  effect. Gmail typed input now accepts an explicit `existing_draft`, and the
  runner resolves that artifact from the bounded local session by exact message
  identity before model execution. After 64 focused tests passed, billing
  refreshed to `$2.82` at 2026-07-11T08:24:24Z and one repaired
  `gpt-5.4-mini` request passed: 31,573 input and 559 output tokens, maintained
  estimate `$0.02619525`. The revision is shorter and warmer, preserves the
  clinical-operations and short-advisory facts plus the exact scheduling CTA,
  keeps message/thread identity, and does not report the draft missing. No tool,
  provider read/write, Gmail draft, label change, search, retry, send, or post
  occurred. Reuse this continuation evidence.
- The live Gmail SENT aggregate profile `sent-style-live-20260710-v3` is now
  explicitly approved for drafting-only use through an audited local path. The
  approval read-back confirms source `live_gmail_sent`, `Sincerely,`, no raw
  sent bodies, and both send flags false. Billing refreshed to `$2.82` at
  2026-07-11T08:33:27Z before the final bounded comparison. One no-tool
  `gpt-5.4-mini` request used 28,997 input and 630 output tokens with a
  maintained `$0.02458275` estimate. The synthetic-context reply was concise,
  used the approved profile, retained clinical-operations/advisory facts,
  requested non-sensitive context, and ended exactly `Sincerely, Anup`.
  Approval remained required; no provider read/write, Gmail draft, search,
  retry, send, or post occurred. Historical checkpoint: that later batch once
  left four future serial API runs available after the named Gmail and Chief
  scenarios. The allowance was subsequently consumed; the current boundary is
  recorded below under Verification And Cost Ledger.
- The joined natural Gmail provider-draft proof is now **PASS**. Its bounded
  runner used two
  one-turn `gpt-5.4-mini` calls in one isolated local session: generate one
  synthetic marked draft, then revise the explicit prior draft shorter/warmer.
  Python applies exact account/recipient/approval gates, creates one marked
  provider draft, updates the same provider ID, reads back both mutations, and
  always invokes exact marked cleanup with absence verification. Persisted
  receipts omit recipient, subject, and body content. Fake-model/fake-provider
  execution passes the whole lifecycle and focused tests. The live run used
  exactly two requests, 60,276 input tokens including 29,440 cached, 1,289
  output tokens, zero retries, and a `$0.0311355` estimate. Both model outputs
  preserved identity/facts/approval/signoff; provider create/read, same-ID
  update/read, and marked delete/absence all passed. No send, search, label,
  Slack post, or unrelated write occurred. Do not repeat this lifecycle.
- ANU-192's selected-thread Outreach-to-Gmail proof also passes with a
  synthetic provider thread rather than private correspondence. Read-only Gmail
  preflight resolved one unique dual-marked `KBA_TEST_EMAIL` thread. Outreach
  used one bounded factual source set plus the approved aggregate style profile,
  produced a concise receipt-confirmation CTA and `Sincerely, Anup`, and handed
  the copy to deterministic Gmail execution. The exact marked draft passed
  create/read-back verification, then marker-gated deletion and absence
  verification; recipient and draft text were not persisted and no email was
  sent. Three learning attempts stopped before Gmail mutation: two exposed a
  redundant requirement to advertise artificial test language, and one exposed
  a shared bug where an approved style-profile ID was treated as a factual
  citation. The constrained composer now removes known advisory style/template/
  example IDs while still rejecting unknown factual sources. The passing request
  used 34,034 input and 166 output tokens with a `$0.0262725` maintained
  estimate. Historical checkpoint: one approved future call remained here; it
  was subsequently used by the ANU-198 Airtable synthesis pass.

## Resume Plan

The prepared Gmail revision, Workspace selected-Doc summary, and Outreach
compact-revision runners now create a privacy-safe validation identity before
execution. The same bounded `run_id` and `case_id` are passed to SDK trace
metadata and stored in each atomic result receipt, so future usage/cost/output
evidence can be correlated to the local trace summary without storing prompt,
provider, account, or recipient content in the identity. Treat missing identity
or trace linkage as a stop event.

### Weekly Chief packet formation

- The reusable workflow name is **Chief Prior Week Packet**. Its plainly named
  entrypoint is `scripts/run_chief_prior_week_packet.py`, with the npm alias
  `run:chief-prior-week-packet`. The date-stamped human artifact remains titled
  `KNI Weekly Operations Packet — YYYY-MM-DD`.
- The seven-day packet joins bounded Slack activity, Gmail activity, completed
  agent-run outcomes, and Calendar evidence before Chief synthesis. Non-recurring
  Calendar events are focus areas because they are more likely to represent
  exceptional work, deadlines, or decisions. Recurring events are summarized
  briefly afterward so routine cadence remains visible without dominating the
  packet.
- The durable artifact should be a reviewable Google Doc saved in the exact
  `KNIOps` Drive folder and titled `KNI Weekly Operations Packet — YYYY-MM-DD`,
  using the packet window end date. Its contents should include: executive focus
  areas; workstreams and decisions; completed runs and linked carry-forwards;
  one-time Calendar events; recurring Calendar cadence; and next actions with
  source-visible provenance. `#ops-finance` should receive only a concise
  answer-first summary and the verified Doc link.
- Packet synthesis remains read-only. Folder resolution must return one exact
  `KNIOps` match before creation; the created Doc must be read back from that
  exact parent folder before its link is eligible for Slack. Creating the Google
  Doc is a separately approval-gated Google Workspace write, and posting its
  link to `#ops-finance` is a separately approval-gated Slack write. A direct
  operator request may approve those exact scoped actions, but packet generation
  alone must not imply either write.
- `Operational health` and `Packet metadata` are the final two sections. Health
  includes only relevant provider failures, incomplete runs, receipt/usage gaps,
  or safety exceptions. Metadata is one human-readable footer line with the
  packet window, source coverage, model/request usage, and verified Doc
  destination; it excludes schemas, internal routes, graph nodes, tool traces,
  raw IDs, and diagnostic dumps.
- The new bounded Calendar-window reader passed against the live primary
  calendar for `2026-07-04T00:00:00-04:00` through
  `2026-07-11T00:00:00-04:00`: 30 active event instances, including 7
  non-recurring focus candidates and 23 recurring instances. The verification
  output exposed counts and safety flags only; descriptions and attendees are
  excluded by contract. Zero OpenAI requests and zero provider writes occurred.
- Exact-window refresh for the other packet lanes used the same July 4–11
  bounds: 58 Slack message markers in `#ai-agents-workflow`, 105 Gmail message
  identities across 77 threads, and 35 active WorkItems (31 blocked and 4
  needing approval). The validation receipts retained counts/statuses only and
  performed no post, send, draft, label, Calendar, Drive, or WorkItem mutation.
  These counts prove availability, not synthesis quality; the next packet layer
  must retain bounded themes/action identities and one-time event titles while
  excluding raw message bodies and private Calendar details.
- The prior-week packet uses completed agent runs from the exact seven-day
  window only, including run identity, agent, completion time, bounded outcome,
  usage/receipt evidence, and an explicitly linked carry-forward when present.
  WorkItem identity is optional provenance, not the packet's unit of activity.
  The current active-store count (35 rows: 31 blocked and 4 needing
  approval) remains diagnostic evidence and is not packet content because it is
  dominated by repeated validation artifacts. A carry-forward may appear only
  when it is explicitly linked to a completed run; unrelated active or stale
  blocked rows are excluded.
- Each selected completed agent run requires an explicit packet role:
  `primary`, `supporting`, or `operational_health_only`, plus a concise relevance
  reason. `dry_run=false` is not treated as human relevance because the local
  run store contains many live validation loops. Primary/supporting runs may
  appear in workstreams; validation lifecycles may contribute at most a succinct
  operational-health signal. Unclassified runs stay out.
- The one-purpose runner `scripts/run_weekly_chief_packet.py` now validates the
  private assembly offline by default. Live mode is fixed to Chief of Staff on
  `gpt-5.4-mini`, one SDK turn, zero tool calls, no specialist tools, no search,
  no provider writes, and a maximum `$0.05` ceiling. The July 4–11 assembly
  passes this offline gate with 7 Slack summaries, 8 Gmail thread summaries, 2
  relevant canonical completed runs, 7 non-recurring Calendar events, and 14
  collapsed recurring series. No OpenAI request has been made for synthesis.
- A follow-up audit found that `max_tool_calls=0` was budget metadata rather
  than an attachment gate. Chief now supports an explicit `attach_tools=False`
  build/run option, and the weekly runner uses it. Focused coverage proves the
  live packet agent has an empty tool list in addition to one SDK turn, so no
  read or write tool call can be initiated by the model.
- The current pre-call billing evidence is `$3.20` from the logged-in OpenAI
  Billing overview on 2026-07-11. After explicit private-data approval, a second
  attempted launch was still denied by the execution safety boundary before
  process start. There is no post-run usage receipt and the new eight-call
  allowance remains intact; do not retry or route around that control.
- The live boundary now builds a separate
  `keystone.weekly_ops.external_synthesis_bundle.v1` projection. It retains only
  business-topic summaries, day-level dates, next actions, relevance, and
  aggregate usage. Provider/thread/run/event/WorkItem identities, links, exact
  timestamps, sender/owner labels, routes, and receipt references remain local;
  email addresses and URLs are deterministically removed, and a local sidecar
  supplies personal-name redactions. Live mode also requires the explicit
  `--approve-external-business-synthesis` flag. The full local source bundle is
  never passed to the model.
- The identity-free projection was then attempted once with the explicit
  external-business-synthesis flag, one-request ceiling, and `$0.05` cap. The
  managed execution reviewer still denied it before process start because
  internal business-operational summaries are treated as private data even
  without PII. No OpenAI request, token usage, or spend occurred; all eight
  newly approved calls remain. Do not retry from Codex. The guarded command is
  ready for the operator to run directly in an appropriately approved local
  terminal environment.
- The named CLI supports `--preview-external-synthesis` to emit the exact
  identity-free payload without an API call, and `--output` to atomically save
  either that preview or the validated live result. This gives the operator a
  review-before-transmit step and leaves a durable usage/cost/safety receipt
  without shell redirection.

### Immediate calendar proof

- A reproduced Slack ask took eight replies and still produced only a plan. It
  incorrectly requested obvious year/calendar/timezone details, routed toward
  Outreach/Gmail, and never performed the write.
- The repaired Chief/Orchestrator fast path now resolves the same complete ask
  directly to Calendar create, infers November 4, 2026 from the current date,
  uses the configured primary calendar and America/New_York, preserves a note,
  and returns an exact event ID for later modification or deletion.
- Focused verification passes 168 tests. A direct no-live CLI execution used
  zero OpenAI requests and did not enter the planner, graph, Gmail, or Outreach.
- The operator approved `https://www.googleapis.com/auth/calendar.events` and
  OAuth refresh now succeeds. The first request exposed a missing bounded 401
  refresh in the Calendar wrapper; that regression is repaired and tested.
- Google Calendar API is now enabled in the dedicated `kni-business agents`
  project, and the `gmail-agent` OAuth token was reauthorized with Gmail modify
  plus Calendar event scopes. The marked lifecycle passed live: create, exact-ID
  update of title/note/date, provider read-back, delete, and absence. Google
  exposes deleted events as `status=cancelled`; the shared verifier now treats
  that provider tombstone as absence, with focused regression coverage. Both
  the initial tombstone and the clean rerun are inactive, and OpenAI usage was zero.
- Slack acceptance now also passes. A bounded read resolved the one active
  operator event by title/date, exposing that the prior approval loop had
  polluted its title with `Follow-up: write approved`. One natural exact-ID
  Slack update restored the intended title, added `submit the final paper`,
  and returned provider verification with zero OpenAI requests. The first
  post-fix run exposed a redundant background completion preview; explicit
  deterministic Calendar writes now bypass that lifecycle. After restart, the
  idempotent acceptance rerun produced exactly one final Slack receipt, and an
  independent exact-ID provider read confirmed the title, date, note, and
  `confirmed` status.
- The operator no longer needs to copy a Calendar event ID for normal
  modification/deletion asks. The planner now extracts a natural event title,
  performs a bounded provider lookup, proceeds only for one active match, and
  keeps the exact ID internal for mutation and verification. Zero or multiple
  matches stop before a write. A live Slack ask naming only the Frontiers event
  updated the same event in one reply with zero OpenAI requests; independent
  read-back again confirmed the intended title, date, note, and active status.
- Gmail direct-write approval propagation is now implemented offline. An exact
  natural request such as `find the latest email from Example Health and create
  a Gmail draft reply; do not send` retains the sender query, runs bounded live
  Gmail selection plus one specialist synthesis, hashes the exact authenticated
  operator command into the scoped approval reference, checks the configured
  Gmail account, creates a thread-linked draft, reads it back, verifies
  recipient/subject/body/ID and `sent=false`, and returns a bounded receipt. The
  CLI no longer forces this explicit write ask into `--no-live-gmail` or a
  second approval-button loop. Text-only `draft a reply` requests remain
  non-writing unless the operator explicitly asks to create/save the Gmail
  artifact. Focused Gmail/CLI/provider coverage passes 173 tests. No API or
  provider write was used for this implementation proof. Natural update is now
  wired too: subject and/or recipient hints drive a capped draft listing, one
  match keeps its ID internal for SDK revision and the existing verified update,
  while zero or multiple matches stop before mutation. A zero-model live probe
  used an intentionally nonexistent synthetic subject; it reached the Gmail
  draft provider and returned `draft_target_not_found`, zero candidates, no ID,
  and no match receipts. No private draft metadata or mutation occurred. The
  joined fake-model/fake-provider create and update proofs now pass through the
  real `run_gmail_triage.py` flow, including synthesis, approval propagation,
  thread-linked creation or same-ID update, provider read-back, bounded receipts,
  and no-send assertions. The expanded Gmail/SDK/CLI/provider gate passes 332
  tests. Real joined create/update should run only from exact operator target asks.
  The dedicated two-turn natural draft lifecycle runner is now fail-closed
  before provider mutation when model identity, supplied clinical-operations/
  advisory facts, the exact `Sincerely,\nAnup` signoff, approval state, or
  provider-draft claims drift. It also rejects any SDK retry, a non-shorter
  revision, or changed provider identity. Focused negative cases prove the
  exact marked draft is still deleted and verified absent after model failure
  or retry evidence. The later live lifecycle passed with exactly two requests,
  zero retries, same-ID provider mutation, exact cleanup, no send, and complete
  sanitized usage/cost evidence. Reuse that receipt; no rerun is needed.

1. Treat the post-fix `npm run test:ai-agents-workflow:no-live` result as the
   current reusable offline baseline.
2. Re-run only the focused model-provider/Gmail/SDK/reporting tests and targeted
   Ruff plus `git diff --check` only if subsequent edits occur.
3. The pending diff has been checked: `draft_created=false` consistently means
   no provider Gmail draft across the GT-1 prompt, schema, report, and tests.
4. Keep the same-session Gmail reply revision marked partial until the
   false-missing-context repair is live-confirmed or superseded by stronger
   graph evidence.
   The next bounded candidate is now persisted in
   `artifacts/test-pack/next-live-gmail-revision-plan.json`: synthetic inputs,
   `gpt-5.4-mini`, one expected/maximum request, retries disabled, `$0.05`
   ceiling, no tools/search/provider calls/writes. Admin Costs exposed a raw
   prior-day `$0.69142858` bucket that the monitor summary did not aggregate;
   Admin Usage returned a zero-request partial page with `has_more=true`, so it
   is not a complete current baseline. Chrome control was unavailable in this
   tool session. The prior eight-call allowance remains exhausted; do not run
   this candidate without a fresh approval ceiling and, if required, a manual
   Chrome billing refresh.
5. Treat the corrected supplied-material Gmail -> Research -> Outreach graph as
   a live pass. The clean receipt records one `gpt-5.4-mini` request, 40,173
   input tokens, 226 output tokens, 40,399 total, and a maintained local estimate
   of `$0.03114675`; live search and provider writes were off. The first pass
   exposed missing ordinary-Outreach usage persistence, which was repaired and
   regression-tested before the clean rerun. Two requests have been used from
   the newly approved eight-run sequential ceiling. Next graph proof should be
   connector-backed or Slack-entry acceptance, not another fixed-source rerun.
6. Add bounded live `SENT` sampling and human style-quality comparison after
   the aggregate profile is reviewed and approved. Raw sent bodies must remain
   in memory only and must not enter durable artifacts or traces.
7. Keep Slack as the human-entrypoint acceptance layer and Computer/UI checks
   as visible confirmation; retain backend receipts as the primary execution
   evidence.
8. Keep RSS and Preprints model synthesis deferred unless a later bounded
   comparison shows material improvement over the now-proven natural read-only
   paths. Continue with the next unproven natural provider lifecycle.
9. Treat the marked Calendar create/read/update/read/delete/absence lifecycle
   and the exact-ID one-response Slack update as reusable passes. Do not rerun
   provider CRUD for unrelated fixes.
10. Treat the natural Zotero marked-note and Google Workspace marked-Sheet
   lifecycles as live passes. Workspace used one SDK session and four serial
   `gpt-5.4-mini` checkpoints—create, append, same-key update, trash—without
  requiring the provider ID in follow-up asks. All bounded provider read-backs
  and cleanup passed. Historical checkpoint: that earlier eight-request batch
  was fully consumed and a later allowance was then approved. All currently
  approved OpenAI calls are now consumed; the weekly packet remains outside the
  Codex-managed private-data boundary. Next Workspace proof after that stage is natural
   selected-file read/summarization, followed by disposable Doc/folder mutation
   coverage.
11. Selected-file Workspace provider preparation now passes without a model.
   No direct-root KNIOps Google Doc matched `proposal`; `README.doc` was then
   resolved and read completely through the scoped typed tool. The next exact
   live scenario is: “Find `README.doc` in KNIOps, read it, and summarize its
   purpose, folder roles, operating model, and safety boundaries without
   modifying anything.” Use `gpt-5.4-mini`, one isolated session, three SDK
   requests maximum for search → read → final synthesis, no search outside
   Drive, no writes, and a proposed `$0.05` cap after a new request ceiling and
   fresh billing snapshot.

## Stop Rules

L174-04 is now a joined live PASS. Direct extraction of NeuroFlow's official
page produced ten claims without broad search; one no-tool `gpt-5.4-mini`
request produced the structured brief with 31,564 input and 635 output tokens,
zero retries, and a maintained `$0.0265305` estimate. The persisted result then
drove a zero-model marked Google Doc create/read/update/read/trash lifecycle on
one provider identity with final `trashed=true`. Chrome credit moved from
`$2.64` to `$2.62`. The first provider attempt stopped before document creation
because only the lifecycle-specific write gate was enabled; the saved research
artifact was reused rather than spending another model request. The runner now
requires both Workspace gates before model execution, and source aggregation no
longer creates a synthetic fixture source when real website/search/profile
inputs exist. Working allowance: one future API run remains.

L174-12 is now a zero-OpenAI live provider PASS. The exact natural ask routes
to Business Research as `contact_discovery` with a `contact_candidates`
artifact and read-only/no-draft policy. The bounded tool read NeuroFlow's
official About and Contact pages and used one Exa request for semantic
corroboration. It selected Michael Mucha, Chief Commercial Growth Officer, at
0.98 confidence, retained the official contact path and public LinkedIn URL,
and persisted only structured evidence. Five Exa results yielded one selected
corroborated candidate; no personal email was inferred and no raw source text,
draft, send, or write occurred. The first provider pass exposed missing
Exa-contribution evidence, so the receipt now distinguishes provider execution
from actual candidate corroboration. The remaining OpenAI allowance is still
one future run.

L174-05 is now a current-source/model PASS and consumes the last approved
OpenAI run. One Exa request returned seven candidates; deterministic filters
retained five Headway mental-health funding sources and rejected unrelated
Headway organizations. One no-tool `gpt-5.4-mini` request used 33,491 input and
1,473 output tokens with zero retries and a maintained `$0.03174675` estimate.
It identified the latest verified financing as the July 23, 2024 $100M Series
D at a reported $2.3B valuation, connected the planned Medicare Advantage,
Medicaid, and clinician-operations expansion to KNI's strategic interests, and
correctly refused to call the 718-day-old event recent or an immediate
opportunity. Exa's crawl date initially appeared as a false 2026 funding date;
the shared runner now prioritizes source-publication dates and deterministically
removes superseded metadata-conflict commentary. Chrome remained `$2.62`
before and after. No tools, draft, send, or write ran. Approved OpenAI allowance:
zero remaining.

Stop on the first retry, shared failure, missing usage/trace evidence,
unexpected side effect, request-limit breach, or budget breach. Do not run
Promptfoo, live search, connector writes, Slack posts, or another OpenAI request
without the applicable explicit approval.
