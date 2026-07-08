# Basic Agent Execution Smoke Tasks

ANU-174 tracks a lightweight operator smoke checklist, not a Promptfoo eval
suite. Use these tasks after orchestration, tool, prompt, or rendering changes
to confirm normal Keystone asks still route to the right agent, preserve no-send
and no-write boundaries, and return visible proof.

## Rules

- Keep each task small enough to run and review by itself.
- Keep the ask text natural. Do not put route names, harness labels, validation
  lanes, or live-test controls inside the prompt itself.
- Default to `--no-live-sdk`, dry-run, fixture, pasted-context, or blocker
  validation before any live connector run.
- Do not send email, create Gmail drafts, create calendar events, post to Slack,
  write Airtable/CRM rows, or create/update Drive files from this checklist.
- Live validation is read-only unless a separate scoped approval explicitly
  authorizes a staged provider write test.
- A passing result must show proof: route, WorkItem ID, source URL, message or
  thread ref, table/record ref, document ref, item key, evidence path, or an
  exact blocker.
- For repeatable Zotero smoke tests, use the offline fixture article
  `Measurement-based care AI evaluation` in
  `KNI Collections - Behavioral Health AI Validation` with item key `ITEM1`.
  Live Zotero probes should replace that with a resolved real library item key
  before execution.

## Validation Lanes

- `offline route/safety`: validate route choice, gates, and blocker text without
  live connectors or live SDK calls.
- `offline fixture/output`: validate useful output from pasted context, local
  fixtures, source-provided rows, or local caches.
- `later live read`: requires a configured read connector or live SDK read path
  to prove current provider access.
- `later live write-gated`: smoke output should stage a plan or blocker only;
  actual mutation requires a separate approval outside this checklist.

## Smoke Queue

| ID | Natural operator ask | Primary route | First validation | Later live validation |
| --- | --- | --- | --- | --- |
| SMK-01 | `@KNI Research NeuroFlow first, then only if the evidence is strong enough prepare draft-only outreach I could review. Please do not send or save anything externally.` | Orchestrator | offline route/safety | Optional live SDK route review only, no writes |
| SMK-02 | `@KNI Please act as my chief of staff: summarize the thread above and tell me the safest next step. Keep the answer in this thread.` | Chief of Staff | offline route/safety with pasted thread context | later live read of a selected Slack thread |
| SMK-03 | `@KNI Research NeuroFlow and give me a short internal note: what they do, why they may matter to KNI, and the source basis.` | Business Research Analyst | offline fixture/output or source-provided context | later live read/search with visible source URLs |
| SMK-04 | `@KNI Research and compare NeuroFlow, Headway, and Spring Health in two short paragraphs: what they have in common and what makes each different.` | Business Research Analyst | offline route/safety for multi-target handling | later live read/search for current company facts |
| SMK-05 | `@KNI Based on these source-backed opportunity notes, which one looks most worth pursuing for KNI, and why? Please keep it as a recommendation only.` | Opportunity Scout | offline fixture/output | Optional live SDK synthesis over the same rows |
| SMK-06 | `@KNI What kind of behavioral-health opportunity should I look for this week? If you do not have enough current evidence, tell me exactly what source context is missing.` | Opportunity Scout | offline route/safety blocker | later live read/search only after explicit scope |
| SMK-07 | `@KNI Can you draft outreach I can review for Example Health based only on approved context about its AI validation workflow? Do not send or save it externally.` | Outreach Composer | offline fixture/output | Optional live SDK draft-quality check, no provider writes |
| SMK-08 | `@KNI Can you make this draft warmer and shorter while keeping the same facts and no new personalization?` | Outreach Composer | offline fixture/output with pasted draft | Optional live SDK revision-quality check |
| SMK-09 | `@KNI Here is an email I pasted below. Can you summarize it, pull out action items, and tell me whether it needs a reply?` | Gmail Triage | offline fixture/output | Not needed unless testing Gmail read access |
| SMK-10 | `@KNI Please look at my most recent non-newsletter email thread from the last week and suggest a reply for review, but do not create a draft.` | Gmail Triage | offline route/safety blocker when Gmail context is absent | later live read of Gmail thread refs |
| SMK-11 | `@KNI If my latest email includes a meeting time, can you extract the calendar details and show me the staged event details? Do not add it to the calendar.` | Gmail Triage plus calendar handoff/blocker | offline route/safety blocker or staged plan from pasted email | later live read plus write-gated calendar blocker or staged event details |
| SMK-12 | `@KNI Look up the Airtable finance tracker for the current quarter so far. Please include the table and field basis, and do not update anything.` | Airtable Context | offline route/safety or fixture schema summary | later live read of table and field basis |
| SMK-13 | `@KNI Here is a receipt. Can you map it to the Airtable finance tracker fields and show me the record plan for review?` | Chief of Staff plus Airtable Context | offline fixture/output with pasted receipt text | later live read of Airtable schema, write-gated only |
| SMK-14 | `@KNI Search Google Drive for a KNI ops document about onboarding or research notes and summarize the key points. Do not create or change files.` | Google Workspace Context | offline route/safety blocker when no doc is selected | later live read of selected KNIOps doc |
| SMK-15 | `@KNI Turn this pasted research note into a one-page brief outline I could put in a Google Doc later.` | Google Workspace Context | offline fixture/output | later live write-gated doc creation only if separately approved |
| SMK-16 | `@KNI In the KNI Collections - Behavioral Health AI Validation Zotero collection, summarize the article "Measurement-based care AI evaluation" in one paragraph and include its item key.` | Zotero Context | offline fixture/output with fixture item `ITEM1` | later live/local Zotero read of a resolved real item key |
| SMK-17 | `@KNI Use the Zotero article "Measurement-based care AI evaluation" from the KNI behavioral-health AI validation collection before you summarize NeuroFlow. Tell me whether it changes the company takeaways.` | Zotero Context plus Business Research | offline fixture/output with fixture item `ITEM1` | later live read/search after a real Zotero item key resolves |
| SMK-18 | `@KNI Look in the preprint history for three clinical AI psychiatry items worth noticing, and explain why each might matter for KNI.` | Preprints Context | offline route/safety or fixture feed output | later live read/search of current preprint sources |
| SMK-19 | `@KNI Check the RSS feed history for recent clinical AI announcements and any action items for us. Please do not post a summary anywhere else.` | RSS Context | offline fixture/output | later live feed read and Slack rendering check |
| SMK-20 | `@KNI Chief of Staff, search the local KNI documents for the latest capability statement and summarize the service areas with evidence paths.` | Chief of Staff plus local KNI evidence | offline fixture/output if local index is available, otherwise exact blocker | Local-only validation; no external live provider required |

## Coverage Map

Use this map to decide what needs another offline test before spending live SDK
or connector budget. `Automated` means an existing pytest, local eval, or
contract test covers the route family and safety boundary. `Dry-run` means the
current no-live path has been exercised but not with this exact natural ask.
`Fixture` means repeatable local data can produce useful output. `Manual` means
the case still needs a recorded operator smoke result.

| ID | Current coverage | Coverage lane | Next offline validation | Future live probe |
| --- | --- | --- | --- | --- |
| SMK-01 | Orchestrator decomposition, WorkItem handoffs, NeuroFlow research, and draft-only outreach gates have automated coverage, but this exact research-then-draft ask still needs a recorded natural-prompt result. | automated + dry-run | Add one no-live WorkItem smoke that routes research first, then allows draft-only outreach only if approved context exists. | Optional one live SDK route-review after offline pass; no Slack post, Gmail draft, send, or external write. |
| SMK-02 | Chief of Staff WorkItem and Slack-thread advisory paths are covered, but selected Slack-thread proof is still mostly manual. | automated + manual | Use pasted sanitized thread context and assert summary, safest next step, and no-post boundary. | Worth one read-only selected Slack-thread probe once a real test thread is chosen. |
| SMK-03 | NeuroFlow company research appears in automated route, context-pack, and source-attribution fixtures. | automated + fixture | Run the exact short-note ask through the no-live WorkItem or named-agent path and record source proof. | Worth one current live-search/read probe if source freshness is part of signoff. |
| SMK-04 | Multi-target research is covered by local evals and tests, but not for this exact NeuroFlow/Headway/Spring Health comparison. | automated + dry-run | Add/source a fixture comparison for the three named companies and assert distinct per-company takeaways. | Worth one live read/search probe because current company facts can drift. |
| SMK-05 | Opportunity Scout source-provided ranking, weak-match handling, and no-write recommendation behavior have automated coverage. | automated + fixture | Reuse source-backed opportunity rows and record recommendation-only output. | Not a priority; live SDK synthesis is optional over the same rows. |
| SMK-06 | Broad opportunity requests and missing-scope blockers are covered in no-live tests. | automated + dry-run | Record the exact broad weekly-opportunity ask and require a useful source-context blocker when no current evidence is provided. | Defer until the operator supplies geography/source scope or approves live search. |
| SMK-07 | Approved inline context, Example Health-style outreach, and no Gmail draft/send boundaries have automated coverage. | automated + fixture | Record the exact approved-context draft ask and require draft-only output plus review notes. | Optional copy-quality check only; no provider writes. |
| SMK-08 | Outreach revision and variant helpers are covered, including shorter/tone changes, but this standalone natural revision ask needs a smoke record. | automated + fixture | Use a pasted draft fixture and assert the revised copy keeps the same facts, removes unsupported personalization, and does not send. | Optional live SDK revision-quality check after offline pass. |
| SMK-09 | Pasted email fixture triage, action-item extraction, and reply-needed assessment have automated coverage. | automated + fixture | Record the exact pasted-email ask through no-live Gmail Triage. | Not needed unless testing Gmail connector read access. |
| SMK-10 | Gmail `get_thread` and read-only CLI behavior have fake-tool tests, but live most-recent-thread selection needs manual proof. | automated + manual | Add a fake GmailTool smoke for the date/window/non-newsletter selection and no-draft reply suggestion. | Worth one read-only Gmail probe; no Gmail draft creation. |
| SMK-11 | Calendar write blocking exists in Chief/Slack capability tests, while Gmail-to-calendar staged event extraction is not yet a focused smoke. | partial + manual | Use pasted email with a meeting time and assert staged event details or an exact calendar-integration blocker. | Defer until the offline staged-plan contract is explicit; later read-only Gmail plus write-gated calendar blocker is worthwhile. |
| SMK-12 | Airtable Context has read-only schema/table tests, no-live finance-topic routing, and prior live-read rows. | automated + dry-run | Record the exact current-quarter finance tracker ask with table and field basis from fixture or dry-run context. | Worth one read-only Airtable probe to prove current table/field access. |
| SMK-13 | Receipt-to-Airtable write-plan handling and receipt attachment/write gates have automated Chief coverage. | automated + fixture | Use pasted receipt text and assert field mapping plus review-only write plan. | Defer writes; a read-only Airtable schema probe is useful before any approved write-gated test. |
| SMK-14 | Google Workspace Context has read-only formatter/tool tests and prior KNIOps live-read rows, but exact doc selection still needs smoke proof. | automated + dry-run | Record blocker behavior when no document is selected, then fixture a bounded onboarding/research-notes doc summary. | Worth one read-only KNIOps doc probe; no file creation or edits. |
| SMK-15 | Workspace write boundaries are covered, but pasted-note-to-brief-outline is not yet a focused smoke. | partial + fixture | Add a pasted research-note fixture and assert one-page outline output with no Drive write. | Do not spend live write budget until the offline artifact-plan path is clean. |
| SMK-16 | Zotero Context has a repeatable local fixture for collection `KNI Collections - Behavioral Health AI Validation`, item key `ITEM1`, and article title `Measurement-based care AI evaluation`. | automated + fixture | Record the exact ask and require the item key plus evidence limitations. | Optional local/live Zotero read of a resolved real item key. |
| SMK-17 | LangGraph/context tests cover preprints/Zotero-to-research patterns, but this exact Zotero article plus NeuroFlow comparison is not yet recorded. | automated + dry-run | Add a no-live handoff smoke that passes `ITEM1` context into Business Research and compares changed vs unchanged takeaways. | Worth one live search/read after the real Zotero item key resolves. |
| SMK-18 | Preprints Context has route/output, parser, renderer, and Promptfoo no-live coverage. | automated + dry-run | Record the exact clinical AI psychiatry preprints ask with fixture feed evidence and preliminary-evidence caveats. | Worth one current preprints probe after RSS/preprints Slack rendering is confirmed. |
| SMK-19 | RSS Context has route/output, renderer, and Promptfoo no-live coverage; Slack rendering remains the main known gap. | automated + dry-run | Record the exact feeds/action-items ask through dry-run RSS context and assert no-post language. | Defer live feed/Slack probe until sibling Slack rendering supports `RssContextResult`. |
| SMK-20 | Chief/local KNI document policy and prompt-contract coverage exist, but capability-statement retrieval needs local-index proof or a clear blocker. | partial + manual | Add a sanitized local-document fixture or local-index availability check that returns evidence paths or an exact unavailable-index blocker. | No external live provider; validate local-only when the index/source folder is available. |

## Major Milestones

- Covered route families: Orchestrator, Chief of Staff, Gmail Triage, Business
  Research Analyst, Opportunity Scout, Outreach Composer, Airtable Context,
  Google Workspace Context, Zotero Context, RSS Context, and Preprints Context
  all have at least route-family coverage in automated, dry-run, fixture, or
  documented prior live-read form.
- Main uncovered gaps: exact natural-prompt smoke records are still missing for
  several cross-agent asks, especially SMK-01, SMK-02, SMK-04, SMK-08, SMK-11,
  SMK-15, SMK-17, and SMK-20. These should be closed with normal route/gate
  assertions, not prompt-specific deterministic shortcuts.
- Next offline tests: add focused no-live WorkItem or named-agent smokes for
  research-to-draft decomposition, selected-thread summary, multi-company
  comparison, outreach draft revision, Gmail-to-calendar staged event details,
  pasted research-note outline, Zotero-to-Business-Research handoff, and local
  KNI document evidence/blocker behavior.
- Future live probes worth budget: one read-only Slack selected-thread probe,
  one read-only Gmail thread probe, one read-only Airtable current-quarter
  probe, one read-only KNIOps Drive document probe, one current source-backed
  company comparison/search probe, and one current preprints probe after
  RSS/preprints Slack rendering is fixed. Defer live write-gated probes until
  their offline staged-plan outputs are stable and separately approved.

## Review Checklist

For each smoke result, record:

- Prompt ID and exact prompt text used.
- Route and agent actually selected.
- WorkItem ID or agent-run ID when present.
- Validation lane used.
- Proof returned by the agent.
- Any unavailable integration, credential, source, ambiguity, or approval
  blocker.
- Confirmation that no send, post, schedule, provider-side draft, Airtable/CRM
  write, or Google Workspace write occurred.
- Whether the case is ready for a later live read or live write-gated probe.

## Resolution Boundary

ANU-174 can be considered ready for resolution when this queue has at least one
recorded offline result per route family, the write-gated prompts visibly stop
before mutation, and the remaining live cases are queued with exact connector
requirements and scoped approval needs.
