# Human Agent Execution Jobs

This is the user-centered validation catalogue for KBA. It complements the
basic smoke queue and framework tier gate by asking whether an ordinary human
request can be understood, executed by the correct owner, verified, revised,
and safely continued.

Every request enters through Orchestrator. A row is not operationally complete
because its route passes. Completion requires useful reasoning plus the
applicable provider lifecycle: read, create, read-back, modify, verify, cleanup
or explicit manual cleanup, and confirmation of no unintended side effect.

## Evidence Baseline

Framework execution and live tool execution are separate statuses.

- **Framework validated:** route selection, SDK agent construction, injected
  fake-model structured outputs/tool calls, WorkItems, context packs,
  simple-versus-graph selection, handoffs, checkpoints, gates, and renderers.
- **Connector primitive validated:** a repository tool/adapter can reach the
  provider and complete a bounded operation when called directly.
- **Agent tool execution validated:** a natural-language request is interpreted
  through Orchestrator and the owning agent, the agent selects and calls the
  correct typed live tool with correct arguments, consumes the result, and
  returns useful proof.
- **Agent lifecycle validated:** the same natural-language task completes
  create/read-back/modify/verify/cleanup as applicable and survives follow-up
  instructions without unintended side effects.

Before 2026-07-10, evidence was primarily framework-level. The first current
connector primitives validated on 2026-07-10 were bounded Gmail, Airtable, and
Zotero reads plus a complete unsent Gmail marked-draft create/read/update/read/
delete/absence lifecycle. The current offline approval
path also proves create/read-back and existing-draft update/read-back behavior,
including provider-ID reuse, content verification, evidence-only receipts, and
no-send enforcement. Live natural-language agent lifecycle completion remains
to be proven.

The Airtable provider lifecycle now has live reversible evidence in the
configured `2026 Finance & Tax Tracker`: one uniquely marked Business Expenses
row was created, read back, modified by exact record ID, read back again,
deleted through the test-only cleanup tool, and confirmed absent. The provider
record contained the mandatory `KBA_TEST_RECORD` marker, and no OpenAI request
was used. A second schema-aware lifecycle populated and then modified every
writable configured Business Expenses scalar type (text, date, select,
multi-select, currency, and checkbox), linked a synthetic PDF receipt through
the multiple-attachment field, verified the attachment count and every scalar
read-back, and removed the record with confirmed absence. Formula/computed
fields remain intentionally read-only. Computer/Airtable visual verification
was attempted afterward but the native control pipe did not start; it is
recorded as unavailable rather than passed.

The Zotero provider lifecycle now also has live reversible evidence: one
standalone note containing the mandatory `KBA_TEST_NOTE` marker was created,
read back, updated with the exact item key and current provider version, read
back again, deleted with the updated version, and confirmed absent. An initial
run exposed a non-JSON 404 response after successful cleanup; a regression now
covers that provider behavior, a search confirmed no orphaned marked notes, and
the corrected lifecycle passed. No OpenAI request was used.

Structural Zotero execution is separately proven without a model: one marked
collection and one marked webpage item were created and read back, both were
updated by exact provider identity/version, item tags and collection membership
were verified, and cleanup deleted the item before the now-empty collection.
Independent reads confirmed both absent. The runner refuses unmarked objects
and non-empty collection deletion and persists an atomic receipt.

The Google Workspace provider lifecycle has live reversible evidence through
the linked Keystone Slack Workspace configuration: one marked Sheet was
created under KNIOps, verified by Drive metadata, given a marked row, read,
updated by stable key, read again, row-deleted, confirmed absent, moved to Drive
trash, and confirmed trashed. Two safe preflight failures proved that KBA's
local `GOOGLE_TOKEN_FILE` is Gmail-only and the Workspace provider token must
come from the explicit linked context repo. No artifact was created during
either failed preflight, and no OpenAI request was used.

### Recent Slack Run Review

A read-only review of the 15 newest usable root runs in
`#ai-agents-workflow` covered four repeated ask families. Slack output is
rendered evidence rather than a complete backend trace, so tool proof was
credited only when provider diagnostics, selected source URLs, run receipts,
or explicit execution results were visible.

Observed strengths:

- one direct non-graph Business Research run showed strong live search-provider
  execution and substantive source-backed synthesis
- inline email workflows consistently produced useful concise draft-only copy
  and preserved no-send/no-write boundaries
- graph runs exposed WorkItem, route, run, blocker, and continuation receipts
  and generally preserved state after failure

Observed gaps:

- graph workflow completion was often reported without completing the human job
- graph research/opportunity runs frequently used generic or fixture-only
  evidence while describing the result as source-backed
- opportunity recommendations were weakly tied to company-specific evidence,
  direct opportunity URLs, active status, deadlines, sponsors, or eligibility
- visible provider statements sometimes contradicted each other within one run
- repeated graph runs varied among generic completion, timeout, maximum-turn
  failure, and partial blocker states
- no reviewed run proved live Gmail search/read, agent-selected provider draft
  creation or update, Airtable execution, or a repaired substantive answer after
  `continue`

The first shared regression from this review is now fixed in the current
workflow contract. A reproduction asking for one active U.S. behavioral-health
grant/RFP previously returned `done` and described a `fixture://` SAM.gov row as
source-backed. It now returns `blocked` with
`manager_loop_current_opportunity_evidence_missing` until a direct non-fixture
URL, sponsor, active status/deadline, and eligibility are verified. Explicit
source-provided internal advisory asks and strict no-match results are not
blocked by this rule.

This baseline supports separate validation for direct specialist calls and
graph workflows. Direct success cannot be assumed to prove graph handoffs, and
graph state receipts cannot be assumed to prove tool execution or useful job
completion.

## Hybrid Interpretation Contract

KBA uses deterministic planning as a fallback and constraint-preservation
layer, not as a replacement for model reasoning.

- Deterministic extraction owns explicit dates/windows, requested counts,
  named agents/providers, exact object identifiers, approvals, negations such
  as `do not send`, create-versus-update distinctions, and hard safety gates.
- The configured planning model owns ambiguous entity resolution, semantic
  route and workflow interpretation, relevance judgments, tool mapping,
  synthesis, and revision intent that cannot be resolved from explicit fields.
- The merge step must preserve explicit user constraints and Python safety
  gates while allowing a structured model plan to enrich or correct the local
  fallback.
- Typed tool execution returns provider evidence. Model synthesis and review
  explain the result; deterministic checks verify identity, side effects,
  approval scope, read-back state, and cleanup.

Simple explicit asks may skip a separate model-planning call and use the local
plan plus specialist synthesis. Ambiguous or multi-step asks should use the
budgeted model planner before execution. No model may override no-send,
approval, exact-identity, or provider-side verification requirements.

## Execution Backend Contract

Task complexity and execution backend are related but not identical.

- Use the non-graph path when one durable agent owns the job and no context-to-
  specialist edge, cross-agent handoff, resumable checkpoint, or repair loop is
  required. The shape is Orchestrator preflight, one specialist call,
  deterministic validation, and answer/review.
- Use LangGraph when the validated plan requires multiple agent stages,
  context-agent staging before a durable specialist, research-to-opportunity-
  to-outreach transitions, approval/checkpoint pause and resume, or bounded
  repair/review loops.
- A simple, intermediate, or advanced label describes the user's job, not a
  forced backend. An intermediate one-agent comparison can remain non-graph;
  a short request that crosses context and specialist ownership may require the
  graph.
- The model may recommend routes and handoffs. Python selects the backend from
  validated plan structure and remains authoritative for state, gates, and
  side effects. User-visible words such as `graph`, `LangGraph`, or test labels
  must not select the backend.

## Initial Job Catalogue

| ID | Tier | Natural request | Expected owner | Human capability | Current proof boundary |
|---|---|---|---|---|---|
| HJ-001 | simple | `Can you summarize my emails from today?` | gmail_triage | find, read, summarize | Offline date/query and batch-plan contract |
| HJ-002 | intermediate | `Find the latest email from Example Health and draft a reply for review. Do not send it.` | gmail_triage | find, disambiguate, reason, draft | PASS: a selected provider thread was promoted without persisting raw bodies, interpreted through the Gmail/Outreach path, converted into one exact provider draft, read back, verified, and cleaned up with confirmed absence. The passing joined run used one model request; no send occurred. |
| HJ-003 | intermediate | `Update the existing Gmail draft to be shorter and warmer without sending it.` | gmail_triage | resolve, modify, verify | PASS: natural subject/recipient wording resolves one draft, keeps the provider ID internal, revises the same draft, reads it back, verifies `sent=false`, and cleans up the exact marked artifact. Zero or multiple matches still block before mutation. |
| HJ-004 | simple | `Research NeuroFlow and explain why it may matter to Keystone.` | business_research_analyst | research, reason, recommend | Source-backed offline path plus fake-model selection/consumption of source deduplication and ranking |
| HJ-005 | intermediate | `Compare NeuroFlow, Headway, and Spring Health and explain the important differences.` | business_research_analyst | compare, distinguish, explain | Multi-company natural routing and source-provided regression |
| HJ-006 | simple | `Review these source-backed opportunities and recommend the best one for Keystone.` | opportunity_scout | filter, rank, recommend | Source-provided ranking path plus fake-model selection/consumption of deterministic opportunity scoring |
| HJ-007 | intermediate | `Find three current behavioral-health AI opportunities worth considering.` | opportunity_scout | discover, filter, rank | Offline route; current-source retrieval later |
| HJ-008 | intermediate | `Draft an outreach email to Example Health using only the approved facts. Do not send it.` | outreach_composer | synthesize, draft, validate claims | Approved-context/no-send path plus fake-model selection/consumption of unsupported-claim validation |
| HJ-009 | intermediate | `Make the existing outreach draft shorter and warmer without adding facts.` | outreach_composer | modify, preserve facts, verify | Offline revision artifact path |
| HJ-010 | simple | `Summarize our current-quarter finances from Airtable.` | airtable_context_agent | read schema/records, calculate, explain | PASS at the provider/deterministic reasoning layer: a natural Chief request resolved the current year/quarter, read the live schema and selected income/expense tables, calculated totals and expense categories, reported uncategorized gaps, and proved no mutation with zero model requests. A live-model prose comparison is optional, not required for this exact arithmetic capability. |
| HJ-011 | intermediate | `Using the Airtable context agent, add a sample expense with a receipt link, update its fields after approval, verify the change, and remove the sample.` | airtable_context_agent | resolve schema, create, attach, modify, verify, clean up | PASS for record and attachment scope: natural execution selects guarded create/update/delete with same-ID verification and cleanup. Separate provider lifecycles prove every writable scalar type, HTTPS link attachment, private local upload, read-back, and confirmed absence. Structural base/table creation remains a separate least-privilege credential boundary. |
| HJ-012 | simple | `Find the latest company proposal in Google Drive and summarize it.` | google_workspace_context_agent | search, read, summarize | PARTIAL for the exact proposal wording because no matching Doc exists in the direct KNIOps root. The joined capability itself is proven on the available validation target: one live run uniquely selected `README.doc`, retained its provider identity, read all 1,096 characters without truncation, and summarized purpose, folder roles, the Slack-to-Drive operating model, and safety boundaries. Three `gpt-5.4-mini` requests and two read-only provider operations ran; the unchanged output passed semantic revalidation. Reuse that selected-file proof and return an exact no-target result for proposal asks until a proposal exists. |
| HJ-013 | intermediate | `Update the identified Google Doc brief with this new approved paragraph.` | google_workspace_context_agent | resolve file, edit, verify | PASS: a natural Chief request selected the bounded Google Workspace lifecycle, created one marked Doc in KNIOps, read it back, updated the same provider identity, read the revised content, moved that same Doc to trash, and verified `trashed=true`. The live Slack run used one `gpt-5.4-mini` planner request; provider and UI evidence agreed, the write gates were disabled after cleanup, and no test Doc remained. Offline fake-model coverage separately preserves the existing Doc identity for same-document updates and blocks duplicate creation. |
| HJ-014 | simple | `Find the latest Zotero paper about clinical AI and summarize it.` | business_research_analyst | resolve Zotero evidence, read, synthesize | Business Research owns synthesis after Zotero evidence resolution |
| HJ-015 | simple | `What recent RSS announcements matter to Keystone?` | rss_context_agent | read, deduplicate, reason | PASS without model: natural WorkItem execution selected the explicitly dual-gated read-only Slack history provider, ranked five items using the shared Keystone domain profile, returned cross-item themes, research/opportunity implications, monitoring directions, dates, direct URLs, current-status caveats, and explicit Business Research/Opportunity Scout handoffs; manager review passed with zero OpenAI requests and no post or modification |
| HJ-016 | simple | `Find three recent psychiatry AI preprints worth reading.` | preprints_context_agent | retrieve, rank, caveat | PASS without model: natural WorkItem execution selected the linked read-only discovery provider, returned three unique ranked papers after version deduplication with paper-set themes, item-specific research frontiers, opportunity implications, monitoring directions, direct URLs, preliminary-evidence caveats, and explicit downstream handoffs; manager review passed with zero OpenAI requests and no modification |
| HJ-017 | intermediate | `Act as my chief of staff and tell me what needs my attention today.` | chief_of_staff | aggregate, prioritize, recommend | PASS offline for honest context gating: the unscoped daily-attention ask remains Chief-owned, selects the portfolio-review capability, and stops at `needs_context` with an exact request for selected operational evidence instead of inventing priorities or citing unrelated SDK docs. The isolated CLI used zero model/provider calls. Live multi-source synthesis remains pending. |
| HJ-018 | advanced | `Read the latest email from Example Health, research the company, and prepare a draft reply for review without sending.` | gmail_triage | read, research handoff, draft checkpoint | PASS for the supplied-material graph path: deterministic preflight preserved bounded Gmail/source identity and approved facts through Gmail -> Business Research -> Outreach, one `gpt-5.4-mini` synthesis reached the approval checkpoint, and no search, provider write, Gmail draft, or send occurred. The connector preparation now separately proves that a bounded Gmail read promotes selected thread identity into the WorkItem and does not persist raw bodies. The joined future run is specified in `artifacts/test-pack/next-live-connector-backed-gmail-graph-plan.json`; Slack-entry acceptance remains separate. |
| HJ-019 | advanced | `Find the best opportunity, research it, create an Airtable record plan, and draft outreach for review without sending.` | opportunity_scout | discover, research, record plan, draft checkpoint | PASS offline: the canonical plan orders Opportunity Scout → Business Research → Airtable record plan → Outreach, preserves the raw ask, creates no provider write, and stops at the review-only outreach checkpoint. Without supplied or retrieved opportunity evidence, the no-live CLI returns an honest no-opportunity blocker instead of inventing a candidate. Live source/Slack acceptance remains separate. |
| HJ-020 | advanced | `Act as my chief of staff: review today's Gmail, open WorkItems, and current Airtable context, then recommend my top three actions.` | chief_of_staff | multi-source coordination, prioritize, explain | PASS offline: Chief remains the graph entry owner while Gmail and Airtable are staged as read-only context advisors; the run completes one Chief plan with no provider approval, write, or extra graph-stage accounting. Live multi-source synthesis remains separate. |
| HJ-021 | simple | `Which Zotero collection contains clinical AI papers? Include item keys and source metadata.` | zotero_context_agent | resolve collection, items, keys, metadata | Fake-model agent selected and consumed bounded Zotero metadata; live-model identity resolution pending |
| HJ-022 | intermediate | `Create a temporary Zotero note for this research item, revise it to be clearer, verify the change, and remove the temporary note.` | zotero_context_agent | create, resolve exact item, modify, verify, clean up | PASS: three natural `gpt-5.4-mini` checkpoints in one local SDK session created one marked item, updated the same internally retained key with version-aware verification, deleted it, and independently verified absence. Six requests; no provider ID was required in follow-ups, no duplicate or unrelated action occurred, and the maintained estimate was `$0.04717935`. |
| HJ-023 | intermediate | `Create a temporary Google Sheet in KNIOps, add one status row, update it, verify the change, and move the temporary sheet to trash.` | google_workspace_context_agent | create, append, resolve exact row, modify, verify, clean up | PASS: four serial `gpt-5.4-mini` checkpoints in one SDK session created one marked Sheet, appended one marked row, updated that row by stable key, and trashed the same Sheet. Follow-up asks omitted the provider ID; session context preserved it. All provider read-backs passed, no duplicate/share/search/send/post occurred, and cleanup confirmed `trashed=true`. Eight requests; maintained estimate `$0.0609651`. |
| HJ-024 | intermediate | `Review a small sample of my sent emails and draft replies in a similar style without copying them exactly.` | gmail_triage | read sent context, infer aggregate style, draft, compare, preserve safety | PASS: bounded live `SENT` sampling produced the corrected aggregate v3 profile from three usable messages without storing raw bodies. The profile was explicitly approved for drafting only, and one synthetic-context `gpt-5.4-mini` comparison retained the supplied facts, requested non-sensitive context, and ended exactly `Sincerely,\nAnup`. No tools, provider operations, Gmail draft, send, retry, search, or post occurred; approval remains required for external use. |
| HJ-025 | intermediate | `Using Gmail Triage, email this clearly labeled test response to my approved test address and confirm delivery evidence.` | gmail_triage | compose, resolve exact recipient, send, read sent copy, verify identity | PASS for one authorized synthetic send. The exact-recipient path created one draft marked `KBA_TEST_EMAIL` and `KBA_TEST_DRAFT`, read it back, modified the same draft ID, sent it once, read the exact SENT message, and verified recipient, subject, body, message identity, SENT label, and draft absence. Provider send count was exactly one; zero OpenAI requests, retries, other recipients, or cleanup errors. Ordinary email sending remains unavailable. |
| HJ-026 | simple | `On November 4th add an all-day calendar event for the Frontiers in Human Dynamics paper due date, with a note to submit the final paper.` | chief_of_staff | infer the next occurrence, use the configured primary calendar, create immediately, read back, later modify or delete by natural event reference | Proven live through the dedicated KBA OAuth client and Slack. Backend create/read/update/read/delete/absence passes. A Slack ask with no provider ID resolved one active event from its natural title, saved the note on the same internal event identity, returned one provider-verified receipt, and exact-ID read-back confirmed the active event. Zero OpenAI requests and no graph/Gmail/Outreach detour. |
| HJ-027 | intermediate | `Create a temporary Zotero collection, add a tagged webpage item, update both, verify membership, and remove the temporary artifacts.` | zotero_context_agent | create collection/item, tag, preserve membership, modify, verify, clean up | PASS at the provider/tool layer: one marked collection and webpage item passed versioned create/read/update/read, exact tags and membership, item-first delete, and independent absence checks. Zero OpenAI requests; no orphan or unrelated mutation. Natural live-model selection is optional later evidence. |
| HJ-028 | intermediate | `Find the latest reviewed Clinical Workflow AI PowerPoint, read its first three slides and speaker notes, and explain the key message without changing it.` | google_workspace_context_agent | search local presentation library, resolve one deck, read slide text/notes, preserve provenance | PASS at the typed-tool/provider layer: title/date terms uniquely resolved the newest reviewed deck from 12 candidates, then read 3 of 16 slides with notes, relative provenance, snapshot IDs, checksum, and immutable-parent proof. The completed residual also indexed 12 reviewed decks and 116 slides in bounded local SQLite; all-term lexical search returned slide/notes excerpts and typed `presentation_slide_evidence` refs for downstream context. Fake-model SDK execution consumes search and read results before structured synthesis. Zero OpenAI requests or writes. |
| HJ-029 | intermediate | `Copy slide 2 from the reviewed Clinical Workflow AI PowerPoint as PNG and PDF for later reuse without changing the deck, verify the copies, then remove the test copies.` | google_workspace_context_agent | resolve exact deck/slide, render derived artifacts, verify signatures/checksums and parent, clean up | PASS: slide 2 produced verified PNG and single-page PDF artifacts with exact parent checksum/mtime preservation. Both marked artifacts were deleted and confirmed absent. Fake-model SDK execution selects the typed copy preview from the natural ask. Zero OpenAI requests, external writes, sends, or posts. |
| HJ-030 | intermediate | `Attach slide 2 from the reviewed Clinical Workflow AI deck to a Gmail draft for my approved test address, update the note, verify the attachment, and remove the test draft without sending.` | gmail_triage | derive immutable copy, create draft, read attachment, modify same draft, verify, clean up | PASS: fake-model SDK execution selects the attachment-draft tool from the natural ask. The live provider lifecycle derived one PNG, created one marked draft, verified recipient/subject/body plus the provider attachment filename, size, and SHA-256, updated the same draft ID, repeated read-back verification, deleted the draft with absence proof, and removed the local PNG. The parent deck remained unchanged. Zero OpenAI requests and no send. |
| HJ-031 | intermediate | `Attach slide 2 from the reviewed Clinical Workflow AI deck to a temporary Airtable record, update the record, verify the attachment, and remove the test artifacts.` | airtable_context_agent | derive immutable copy, create marked record, upload private bytes, modify same record, verify, clean up | PASS: fake-model SDK execution selects `airtable_upload_attachment`, not the HTTPS-link tool. After a read-only schema check confirmed `multipleAttachments`, the live lifecycle created one marked record, uploaded the private PNG, read back exactly one new attachment with matching filename and byte size, updated the same record ID, deleted the record with absence proof, and removed the local PNG. The parent deck remained unchanged. Zero OpenAI requests, search, sends, or posts. |
| HJ-032 | intermediate | `Post a clearly marked test update in ai-agents-workflow, revise the same message, verify it, and remove the test message.` | chief_of_staff | resolve exact Slack channel, create, read, modify same timestamp, verify, delete, confirm absence | PASS at the provider/tool layer: one exact-channel `KBA_TEST_SLACK_MESSAGE` was posted, read back, updated on the same timestamp, reread, deleted, and confirmed absent. Separate live gate, channel allowlist, approval references, marker checks, and ambiguous-post recovery are enforced. Zero OpenAI requests or search. This proves reversible Slack writes, not ANU-60 answer-quality or multi-agent Slack execution. |

## Acceptance Record

For every job record:

- raw natural request and user-visible constraints
- Orchestrator route and rejected alternatives
- simple runner or LangGraph selection
- context sources requested, resolved, missing, and consumed
- typed tool calls and structured output type
- reasoning quality against the user's actual question
- provider artifact identifiers without secrets or raw private content
- approval decision and exact mutation scope
- read-back/modification verification and cleanup status
- no-send/no-post/no-publish confirmation
- model request count, usage, cost evidence, and trace only when a separately
  approved live-model run occurs
