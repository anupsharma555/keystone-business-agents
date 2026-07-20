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

## 2026-07-19 Gmail-to-Slack Draft Acceptance Checkpoint

Five Slack/backend runs isolated the current boundary. Five runs from the
renewed ten-run allowance were then used; five remain. The realistic ask is to
find one exact email in the connected mailbox, interpret it, and return a short,
copyable response draft only in the Slack thread. Gmail must remain read-only
for this case.

The first two runs used an exact subject that belongs to another account. After
the shared route repair, KBA selected Gmail correctly and returned an honest
provider no-match instead of inventing content. The replacement subject
`RUAIH Certification` exists exactly once in the connected account. Independent
Gmail inspection confirmed the selected provider message and zero drafts.

Three runs against that valid message narrowed the remaining boundary:

1. The first reached Gmail and produced a useful model result, but the
   deterministic output parser treated the task-local phrase “either ask one
   question or make one useful point” as an exact one-item requirement for the
   entire response and blocked it.
2. After that repair, backend run 6375 completed with a useful assessment and
   full `draft_reply`, but the Slack renderer exposed only the summary and
   omitted the copyable draft.
3. After the renderer repair, Slack run
   `sbar_5b25aa1b55534dbc867c179be0ef4f6f` reached the correct Gmail owner and
   selected-message input, then failed during model structured-output
   completion. Its SDK session contains one bounded user turn and no assistant
   turn. No WorkItem or LangGraph route was selected; Slack's generic
   `Business Agents WorkItem Failed` title was therefore misleading.

The renewed allowance then tested both the original wording and a natural
rephrase:

1. Backend run 6376 passed the original request. It selected Gmail Triage,
   read one exact provider message, used one specialist model request, created
   no WorkItem/graph, and Slack showed the useful assessment plus full copyable
   draft.
2. The rephrase said “find the email titled,” “write a short reply here,” and
   “leave Gmail exactly as it is.” The old email-object and thread-copy
   detectors missed that equivalent shape. One planning request retained Chief,
   the budget estimator priced a generic seven-turn Chief path, and execution
   stopped before Gmail.
3. After the general email-object/thread-copy repair, the unchanged rephrase
   reached Gmail but failed schema validation. The model marked request
   coverage complete while writing `unmet_dimensions: ["None"]`. The bounded
   structured-output retry repeated the semantic placeholder. Redacted failure
   persistence recorded the exact Pydantic error in backend run 6377.
4. The shared request-coverage schema now documents the empty-list contract and
   normalizes known empty sentinels. Backend run 6378 passed the unchanged
   rephrase with one Gmail read, zero Gmail writes, no WorkItem/graph, and a
   useful one-line Slack assessment plus reply.
5. Offline hardening now treats request coverage as an advisory model-authored
   audit rather than an execution gate. A contradictory audit status is
   conservatively downgraded to `unassessed`, `partial`, or `blocked` and
   remains visible through `requires_attention()` without discarding the
   specialist's otherwise valid result. Core schema corruption and the
   authoritative safety, approval, provider-identity, and mutation-receipt
   checks remain hard failures.
6. Run 6378 also exposed a separate output-contract defect. The planner turned
   “leave Gmail exactly as it is: no draft, send, label changes, or archive”
   into hard forbidden response words, so an unnecessary repair pass removed
   the useful draft label. The Gmail schema also made an operator-requested
   optional reply structurally incompatible with `needs_reply=false`. The
   shared repair keeps provider-action constraints out of hard response
   validation and permits a copyable, review-required reply when the triage
   judgment is “optional.” Backend run 6379 then passed the same unchanged
   rephrase with one model request, no constraint repair, one provider read,
   zero provider writes, no WorkItem/graph, and a separately labeled
   `Draft response` in Slack.

The shared repairs are intentionally not prompt-specific:

- output constraints now distinguish advisory LLM interpretation and style
  from deterministic requirements;
- only explicit, objectively measurable response-shape rules can block or
  trigger repair;
- LLM planner output cannot invent hard count, section, forbidden-phrase,
  punctuation, or URL requirements that are absent from high-precision
  operator wording;
- provider mutation prohibitions remain safety and tool constraints, not bans
  on ordinary response vocabulary;
- ambiguous task-local wording remains available to the Orchestrator and
  specialist but cannot become a deterministic veto;
- structured Gmail/Outreach results render the model-authored assessment and
  full draft in a copyable Slack layout;
- Gmail triage may provide an explicitly requested optional copyable reply
  while keeping `needs_reply=false`, `draft_created=false`, and human review
  required;
- live structured-output execution gets at most one fresh-session retry for a
  recoverable schema/model-behavior failure; and
- terminal child validation failures are saved as redacted local diagnostics
  when the Slack caller provides the run database;
- positive email-object actions and current-thread copy/paste requests use the
  same bounded Gmail ownership contract across “Gmail email with subject” and
  provider-independent “email titled” wording; and
- shared request coverage treats `None`, `N/A`, and equivalent empty sentinels
  as `[]`; contradictory self-audit fields are downgraded and flagged rather
  than allowed to fail a read-only answer.

The current focused shared schema, registry, Gmail, instruction-following,
routing, and CLI gate passes 685 tests. The broader execution-request, SDK,
Orchestrator, WorkItem, context-pack, LangGraph, Slack-action, and coverage gate
passes 732 tests. Focused Ruff and `git diff --check` pass.

This exact Gmail-read/Slack-reply boundary now passes for the original request
and one meaning-preserving human rephrase. Gmail connector verification found
the same single Inbox message, unchanged labels, and zero drafts after both
successful runs; desktop Computer inspection confirmed the visible Slack
answers. Run 6379 now preserves the assessment and separately labeled copyable
reply without an output-constraint repair. The Slack connector and desktop
Computer view independently confirmed the visible result. Gmail provider
inspection before and after the run found the same one message, unchanged
`IMPORTANT`, `CATEGORY_PERSONAL`, and `INBOX` labels, and zero drafts. Five live
runs remain; stop here rather than broadening
until the next test is selected for a materially different continuation,
provider, or graph boundary.

## 2026-07-19 Evidence-Gated Owner Reconciliation Checkpoint

A direct Business Research canary exposed a shared owner-selection defect after
the earlier zero-tool profile repair. The deterministic Slack-operations hint
joined `Slack` in a supplied architecture fact with `meeting` in unrelated
time-pressure context. That Chief hint then replaced the explicit Business
Research owner before the specialist could interpret the ask. The first live
attempt used one planner request and stopped at the one-request ceiling; no
specialist, tool, provider, WorkItem, or graph stage ran.

The durable repair does not add a phrase exception. Direct CLI/Slack and
WorkItem/graph routing now use one evidence-gated owner resolver:

- an unnamed request may follow the semantic plan;
- a named owner is preserved unless a different capability is supported by
  bounded positive evidence and the semantic intent agrees;
- valid reassignment evidence is an operation bound to a provider/object, a
  capability-specific requested artifact, or a genuine multi-owner workflow;
- supplied facts, examples, negative constraints, time pressure, and generic
  operational words remain context only; and
- exact typed controls and deterministic safety/provider gates retain their
  existing authority.

The Slack detector no longer joins generic words such as `meeting`, `calendar`,
or `gmail` with `Slack` across clauses. Read/status operations require a bounded
Slack object such as a channel, thread, message, workflow status, or socket;
post/send/update/delete wording remains routed to the existing blocked or
approval-gated Slack action path.

Offline verification covers the same nuisance prompt across Business Research,
Opportunity Scout, Gmail Triage, Outreach Composer, and Chief; genuine
Opportunity, Gmail, Outreach, Airtable, browser-diagnostics, Calendar, Slack,
and graph handoffs remain intact. The full repo passes 3,945 tests with one
skip, and targeted Ruff/diff checks pass.

The second live attempt passed. Slack thread
`1784510520.297089` rendered exactly two bullets with no heading, route
metadata, workflow metadata, or action button. Backend run 6368 selected
`business_research_analyst`, used one `gpt-5.4-mini` request (13,042 input and
53 output tokens; local estimate `$0.01002`), admitted zero tools, and recorded
no provider write, WorkItem, graph stage, or tool event. Slack API and desktop
Computer inspection independently confirmed the public output. This proves one
direct named-agent/no-tool row.

The unchanged prompt then passed live through the other operating-agent
entrypoints:

| Requested owner | Backend run | Model requests | Input / output tokens | Slack thread |
| --- | ---: | ---: | ---: | --- |
| Opportunity Scout | 6369 | 1 | 12,942 / 50 | `1784510987.256479` |
| Gmail Triage | 6370 | 1 | 14,251 / 53 | `1784511131.172709` |
| Outreach Composer | 6371 | 1 | 14,583 / 53 | `1784511191.142329` |
| Chief of Staff | 6372 | 1 | 11,662 / 350 | `1784511238.647099` |

Every row preserved the named owner, completed without a repair request,
attached zero tools, left WorkItem and tool-event counts unchanged, performed
no provider write, and rendered exactly two visible Slack bullets without a
heading, route/workflow metadata, or action button. Slack API reads and desktop
Computer inspection independently confirmed each result. Together with run
6368, this proves cross-agent parity for this response-only supplied-context
contract. It does not prove graph, provider lifecycle, or same-thread
continuation parity.

## 2026-07-19 Cross-Agent Slack Continuation Checkpoint

The latest same-thread natural-language CoS formatting follow-up failed before
the shared Chief contract could interpret the newest turn. Slack did retrieve
the correct human root, but the untyped digest mixed it with an incorrect
historical bot answer and the human correction. Deterministic owner inference
then treated incidental research language in the bot answer as routing
authority. A second shared parser defect expected a period after “Continue the
same agent task” even though the live adapter used a comma, so bridge
instructions were folded into the user ask. The unresolved route was then
incorrectly admitted to a WorkItem/LangGraph clarification path. This is a
shared entrypoint/context-authority failure, not evidence that the planner or
Chief model misunderstood the formatting instruction.

A post-repair canary then reproduced the same public blocker and revealed the
still-earlier decisive collision. The neutral Slack envelope begins “continue
this prior Slack thread.” The shared bridge command builder treated any input
beginning with `continue` as a deterministic WorkItem advance, so neither the
KBA planner nor the new direct-clarification guard ran. The fix now recognizes
the exact neutral wrapper and builds the canonical `keystone_agents.cli ask`
command; only explicit `workitem continue` syntax remains a deterministic
advance. The same fix removes connector attribution before constructing the
wrapper.

The next canary reached the canonical ask path and Slack reported that it
stopped at the request-budget gate before any model call. Later Admin Usage
evidence showed that one planning request had already occurred, so the original
Slack statement was not telemetry-truthful. The cost estimator priced a provider-free
formatting correction as a full Chief run with the generic six/eight-turn
ceiling, even though provider/search/tool use was prohibited. The shared
quality-budget and CLI estimate now recognize this as a bounded response-only
profile: one tool-free Chief synthesis turn, one planner turn, and at most one
independent output-constraint repair. The exact live-shaped request therefore
estimates 2 required and 3 maximum OpenAI requests under the five-request
ceiling. This is a cost/tool-profile hint only; planner/Orchestrator routing
remains unchanged.

The repair standardizes all ordinary root and follow-up requests on one neutral
continuation envelope:

- the newest user turn remains authoritative and unmodified;
- the human thread root and bounded messages retain explicit operator/agent
  roles;
- historical agent prose may inform repair but cannot select an owner by
  itself;
- a linked WorkItem, provider affinity, prior request/result, and operator
  feedback are bounded reference context only;
- the previous agent is never synthesized as a new explicit mention;
- ordinary natural language always re-enters the canonical planner and may
  select direct, WorkItem, or graph execution;
- only explicit WorkItem syntax or normalized typed actions bypass semantic
  planning;
- genuine missing-input clarification returns directly without creating a
  WorkItem; and
- prior-request extraction is bounded so nested continuation envelopes cannot
  grow across turns or retain connector attribution.

The current planner -> deterministic Orchestrator -> Chief separation is
retained for Chief-of-Staff asks. Optimization should reuse one bounded context
packet, narrow tools after planning, resolve safe uncertainty before asking for
clarification, preserve one structured plan/PublicResult, and eliminate
competing intent decisions in entrypoint adapters.

Offline verification passes across the sibling Slack entrypoint/run-record
surface and KBA ExecutionRequest/manual-planner/Orchestrator/CLI/graph parity
surface. Six same-thread canaries were ultimately attempted under the additional
ten-run allowance. The sixth passed: the newest CoS turn produced exactly the
requested three bullets with no title, metadata, note, clarification, action
button, tool, provider, WorkItem, or graph. Backend run 6363 used one
`gpt-5.4-mini` request, and desktop Slack inspection independently confirmed
the visible result. Two later distinct-root canaries consumed the seventh and
eighth runs, exposing the downstream status defect and the pre-Orchestrator
Calendar admission defect recorded below. The ninth and tenth canaries are now
also complete; no live run remains. Their title and tool-admission results are
recorded in the final checkpoint below.

## 2026-07-19 Post-Patch Human-Language CoS Acceptance Checkpoint

Three serial, natural-language `@KNI CoS` acceptance asks were run without
naming the owning specialist agents:

- **Gmail lifecycle: passed.** Casual wording such as “put,” “tighten,”
  “check,” and “throw away” was interpreted as one exact marked-draft
  create/read/update/read/delete/absence lifecycle. The provider receipt proved
  the draft absent after cleanup and `email_sent=false`; exact Gmail Drafts and
  Sent searches showed no residual marker.
- **Google Docs lifecycle: passed.** A request to “leave me one throwaway
  document” with a quoted sentence created and read back the exact marked
  document, then moved that same object to Drive Trash and verified it there.
- **Automatic multi-agent review: partial.** The planner correctly chose
  Business Research followed by Opportunity Scout, preserved the no-search and
  no-write constraints, and executed the LangGraph handoff without the operator
  naming either agent. The answer nevertheless failed acceptance because the
  phrase “supplied read-only note” was not admitted as source-provided evidence;
  Business Research used a generic fixture, Opportunity Scout then lacked the
  actual facts, and Slack appended internal retrieval metadata.

The post-run repair extends the existing source-provided evidence contract to
natural note-shaped inputs and extracts only the note body, stopping before
instructions such as “First assess” or “Then decide.” It is a general evidence
boundary, not a company- or prompt-specific route. A graph regression now
proves that the heuristic planner selects Business Research and Opportunity
Scout automatically, the note facts survive the handoff, and the instruction
text is not stored as evidence. Slack retrieval/run metadata is now hidden by
default and retained only for explicit diagnostics or requests to include
metadata.

Offline verification passed 739 workflow/planner/CLI tests, 192 Slack
entrypoint tests plus 33 subtests, and the focused automatic LangGraph handoff
regression. The OpenAI Admin Usage window observed six model requests, 98,304
input tokens including 28,160 cached tokens, and 3,093 output tokens across the
repair-and-acceptance window. The API omitted model, project, and key
dimensions, so this is organization-window evidence rather than exact
per-run cost attribution. No fourth live acceptance ask was made: the approved
three-test batch was complete, and the multi-agent post-fix proof was closed
offline instead of expanding the live budget.

All temporary Gmail and Google Workspace test-lifecycle gates were removed,
the normal Slack request ceiling was restored, and the Slack Socket Mode worker
was restarted on the restored configuration.

### 2026-07-19 Single Live CoS Renderer Verification

One additional natural-language `@KNI CoS` ask was run after the offline graph
repair. It supplied a synthetic read-only company note, asked what the note did
and did not establish, and requested the most credible KNI opportunity plus the
single highest-value validation gap. It named no specialist and prohibited web
search, provider writes, email drafting/sending, other Slack posts, and internal
workflow metadata.

The KBA backend passed its reasoning boundary. The live planner selected Chief
of Staff directly rather than a WorkItem graph, and the stored
`ChiefOfStaffResult.slack_display_text` contained a concise, useful answer
grounded only in the supplied note. That direct selection is valid for a bounded
single-owner synthesis, but it means this run does not add live proof for the
repaired multi-agent note handoff.

The end-to-end Slack acceptance result failed. The native Slack thread and the
Slack run record both show only `Business Agents WorkItem Ready` and `WorkItem
command completed.` The sibling Slack bridge recognizes specialist, Gmail,
Calendar, context-agent, and WorkItem result shapes, but it does not render a
top-level `ChiefOfStaffResult`; it therefore falls through to the generic
WorkItem completion response even though KBA supplied `slack_display_text`.
The next repair is a general direct-result renderer before that fallback, with
a regression proving that useful display text is surfaced and internal metadata
remains hidden.

The run used one `gpt-5.4-mini` request, 56,150 input tokens, 595 output tokens
including 138 reasoning tokens, and a local estimated cost of `$0.04479`.
The immediate Admin Usage snapshot had not yet ingested the run and covered
minute buckets only through 12:57 UTC, so the stored per-run SDK telemetry is
the primary cost evidence. No retry was made. The normal Slack request ceiling
was restored and the Socket Mode worker was restarted successfully.

### 2026-07-19 Follow-up CoS Semantic and Output Verification

Three later serial checks narrowed the remaining failure from routing to two
shared contract gaps:

- The first follow-up proved that Slack could render a direct Chief result, but
  only the `summary` field reached the user; the opportunity and validation-gap
  judgment remained in `synthesis`.
- After the Chief public composer combined `summary` and `synthesis`, the next
  check routed to Opportunity Scout but incorrectly treated “do not search the
  web” as live-search permission. It returned 30 results from the configured
  retrieval lanes and exposed run metadata in Slack. No provider write,
  outreach draft, approval item, or outbound action occurred.
- A shared negative-research gate now recognizes equivalent instructions such
  as “do not search the web” and “don’t browse the internet” in direct and
  WorkItem paths. The compact Opportunity result also exposes one explicit
  public display field rather than raw run metadata. Offline verification
  passed 203 focused planner, compact-output, Chief-output, and CLI tests.

One final approved API-backed CoS check used “don’t browse the internet” and
another synthetic supplied-only note. The safety boundary passed: live web
research was disabled, no search/tool/provider-write record was created, and no
email or additional Slack post occurred. User-facing acceptance still failed.
The older Slack-specific Chief renderer ignored KBA's explicit
`slack_display_text`, rebuilt a partial answer from `summary` and recommended
actions, and appended its legacy agent/planning metadata.

The sibling Slack renderer now prefers KBA's explicit public display field for
ordinary Chief results while retaining route/planning details only in internal
result metadata. Its complete focused run-record suite passes 44 tests plus four
subtests, and the restarted Socket Mode worker is connected on the restored
normal request ceiling. This last renderer repair is not yet live-proven: the
single approved API check was consumed by the failing pre-repair result.

The direct Chief Slack bridge still does not persist the child script's
per-request SDK usage/cost payload. The live path normally includes a compact
command-resolution model turn followed by the Chief turn, but exact post-run
token and cost attribution cannot be reconstructed from the Slack run record.
Persisting redacted child usage/cost telemetry is therefore a remaining
observability task, separate from the user-facing output fix.

## 2026-07-19 Human-Language CoS Provider Checkpoint

Three serial Slack asks were approved, but the batch stopped after the second
ask exposed a shared entrypoint failure.

- A human-oriented `@KNI CoS` request to create one marked Google Doc, read it
  back, and remove the disposable artifact passed. Slack returned one concise
  provider-truthful answer without an ID, link, WorkItem status, or raw receipt.
  The canonical KBA audit row records one Orchestrator planning request and the
  direct Google Workspace owner. Google Drive visibly showed the exact document
  in Trash under `KNIOps`.
- A human-oriented `@KNI CoS` request to save a marked reminder draft, make the
  same draft shorter and warmer, verify it, and remove the test draft failed.
  Slack returned a plan and explicitly admitted that no draft had been created
  or deleted. Exact Gmail searches confirmed that the marker was absent from
  both Drafts and Sent.
- The third planned multi-owner CoS ask was not submitted. This preserved the
  live-batch stop rule after a shared semantic-to-execution failure rather than
  spending another call on a path whose admission contract was already known
  to be wrong.

The root cause is before provider execution. The sibling Slack bridge's
business-system surface and owner checks include Airtable, Google Workspace,
and Zotero, but exclude Gmail mutation workflows. The natural Gmail request
therefore fell into the legacy Chief-of-Staff planning script and never reached
KBA's canonical `ask` path. Evaluated directly, the same raw request satisfies
KBA's guarded marked-draft lifecycle contract, so this is not a Gmail
credential, provider, deletion-gate, or lifecycle-helper failure.

The forward fix should make CoS a reliable canonical front door for structured
business-system actions. Slack should preserve the raw ask and send it to KBA's
Orchestrator/capability contract for owner and typed-tool selection; Python
should remain authoritative for account, exact identity, approval, no-send,
read-back, and cleanup gates. Do not repair this with a growing synonym list or
another Gmail-only natural-language lane. Acceptance should cover ordinary
human verbs across Calendar, Gmail, Docs, and Airtable, including create,
read-back, same-object modification, cleanup, and negative send/post
constraints, while proving that advisory-only requests still remain plans.

The Admin Usage window covering the stopped batch reported four model requests,
178,939 input tokens including 63,488 cached tokens, and 3,633 output tokens.
The usage response omitted model, project, and key dimensions, so the window is
supporting batch evidence rather than exact per-run attribution. All temporary
Docs/Gmail lifecycle permissions were removed after verification.

## 2026-07-18 CoS Front-Door Generalization Checkpoint

The architecture already places Chief of Staff inside the Orchestrator-first
path as the default time-saving manager. The latest varied natural-language
Slack batch showed that the provider capabilities themselves are not the whole
acceptance boundary:

- Calendar create, same-event update, idempotent update, and delete reached
  exact provider read-back, but early retries exposed native-command
  interception and a renderer that could contradict a successful receipt.
- Airtable completed one marked create/read-back/update/read-back/delete/absence
  lifecycle from an ordinary `@KNI CoS` ask.
- Google Docs returned a correct four-step plan without invoking the owning
  lifecycle helper after the live gate was enabled.
- Gmail handed a complete marked draft lifecycle to a WorkItem instead of
  invoking the owning draft helper.

The repaired contract keeps interpretation semantic: Orchestrator/LLM preflight
selects the owner from the raw ask and bounded thread context. Only after owner
selection does a marked, single-provider lifecycle bind to that owner's typed
helper. Python owns exact scope, approval/live gates, provider identity,
read-back, cleanup, and receipt-truthful rendering; it does not replace
natural-language interpretation with a larger phrase taxonomy. Stateful
multi-owner work still uses the WorkItem/LangGraph path.

An additional offline entrypoint check found one remaining pre-interpretation
budget defect: equivalent wording such as "take this marked test object through
its approved lifecycle" was still assigned the generic planner-plus-Chief
minimum and could be rejected before the planning model selected the direct
provider helper. Exact protected `KBA_TEST_RECORD`, `KBA_TEST_DOC`, and
`KBA_TEST_DRAFT` markers now admit one semantic-planning turn; they do not select
an owner or authorize execution. The interpreted plan must still select the
provider owner and satisfy the exact typed lifecycle contract. Ordinary Chief
asks retain the existing pre-call budget block.

The next offline layer now runs the natural `@KNI CoS` Google Docs and Gmail
cases through their real direct lifecycle wrappers with verified fake-provider
receipts. The public result retains route, operation, read-back, cleanup,
request count, and no-send evidence while removing provider IDs, links, and
approval references. The local `agent_runs` audit row retains the complete
provider receipt for diagnosis and safe recovery. Both tests fail if execution
falls through to a specialist plan or WorkItem.

The agreed live window ended after ten Slack asks, so Google Docs and Gmail
remain pending fresh post-fix live proof. Their test objects were not left
behind: provider searches found no matching Gmail draft or Drive file, the
Airtable receipt confirmed absence, and all temporary lifecycle permissions
were removed. Offline after the fix, 360 focused execution/routing tests,
20 diverse-ask tests covering 16 cases and eight agents, and all three
architecture-generator tests pass.

## 2026-07-18 Semantic-Equivalence Checkpoint

The manual-request planner now explicitly owns equivalence across prose-first,
object-first, passive, question, shorthand, Slack-mrkdwn, and reordered-stage
wording. A new static acceptance set contains 25 prompts across five goals:
marked Airtable, Google Docs, and Gmail lifecycles; Opportunity Scout discovery;
and Chief of Staff operational prioritization.

The offline gate injects the same correct LLM interpretation for each
equivalent form and proves that explicit-agent advice, fallback-plan merging,
and direct-owner lifecycle checks do not veto it or weaken approval, no-send,
read-back, and cleanup constraints. This deliberately adds no new keyword
router. It proves deterministic compatibility with good semantic
interpretation, not live-model paraphrase quality. Fresh Slack/model validation
remains pending because the approved ten-call window is exhausted.

The direct-versus-WorkItem audit then found one concrete typed-handoff defect:
WorkItem creation stored the LLM plan under `manual_constraints`, while the
Opportunity context-pack builder read `constraints`. The graph path could
therefore receive an empty hard-filter list even though the direct plan retained
it. All four specialist context packs now expose the exact stored planner
constraints through their common typed base contract. An ambiguous follow-up
such as "update it" with both Airtable and Gmail objects in bounded history now
returns clarification instead of selecting the first provider keyword. This is
a deterministic conflict gate, not a new intent router.

Focused planner/context tests pass 188, the complete WorkItem runner passes
295, and the SDK/architecture/semantic group passes 137. These are offline
schema and execution-path checks; they do not add live Slack proof.

The next context audit found that the LLM planner retained the oldest eight
Slack messages rather than the newest eight. Raw `thread_messages` using Slack's
`ts`, `user_id`, and `text` names could also compact into empty summaries, and a
long transcript kept only its head, dropping the newest operator follow-up.
Planner compaction now preserves chronological newest-message/prior-run tails,
normalizes both internal and raw Slack field shapes, and keeps the transcript
root plus newest tail around an explicit omission marker. A redacted receipt
records original, retained, and dropped counts/characters without message
content.

Full offline verification passes 282 planner/Orchestrator tests, 97 Slack action
and contract tests, and 247 CLI tests. The sibling live worker has not been
reloaded or probed in this slice.

Exact same-object identity was also absent from planner context when a
continuing WorkItem had no duplicated Slack metadata. A WorkItem holding an
exact Gmail draft target and selected artifact reproduced as an empty workflow
context. The CLI now projects a bounded `current_work_item` identity containing
only canonical route/status, prior request, target name/type/external ID, up to
three selected artifact references, and the next safe action. Slack context-file
loading merges rather than replaces that identity. The planner re-compacts the
allowlisted fields and records whether identity expansion occurred; arbitrary
metadata, provider payloads, draft bodies, command hints, and unselected
artifacts do not cross the boundary.

The complete CLI/manual-planner group passes 420 tests and the complete
WorkItem/context-pack group passes 315 tests after this repair. Live same-object
provider execution remains part of the next approved Slack batch.

The offline fallback also failed to retain some negative clauses in the
planner's constraint list, including variants such as "do not draft outreach or
save them." A general clause-preservation step now copies bounded `do not`,
`don't`, `never`, `without`, and `no` clauses into constraints. This is payload
preservation only: the clauses do not choose the owner, provider, operation, or
approval state. The LLM planner receives the same instruction, merged plans
cannot discard operator-negative constraints, and WorkItem packs retain the
result through the common constraint contract.

After this change, 279 planner/semantic/registry/prompt tests, 249 complete CLI
tests, and 315 WorkItem/context-pack tests pass. Live negative-constraint
interpretation remains pending the next approved Slack batch.

## 2026-07-18 Slack Payload Integrity Checkpoint

The selected-message Slack contract remains
`keystone.slack.selected_message_context.v1` for bridge compatibility, with an
additive `keystone.slack.payload_manifest.v1` inside it. The manifest records
the raw modal ask, selected and bounded thread-message lengths and SHA-256
digests, visible truncation state, attachment metadata, and whether attachment
bytes were supplied as checksum-verified local context. Private Slack file URLs
are reduced to a presence flag and are not persisted in the manifest.

The modal pointer now includes the exact local context-file SHA-256. A changed,
malformed, or missing file does not silently become empty context: the handler
uses the bounded embedded fallback when available, records an explicit warning,
and persists a fresh context file. The recovered raw ask becomes the
manifest's current-turn reference before Orchestrator preflight and is promoted
into bounded WorkItem Slack metadata.

Attachment materialization is now byte-verified rather than declaration-based.
A local path is accepted only when the file exists, its actual SHA-256 matches
the supplied checksum, and its actual size matches nonzero Slack metadata.
Natural asks such as "summarize the attached PDF" or "review this" with a
selected attachment stop before deterministic planning, an LLM call, WorkItem
creation, or provider execution when those bytes are unavailable. The result
uses `slack_attachment_not_found` or
`slack_attachment_bytes_unavailable`, preserves the same raw request and
context-file path for retry, and does not persist Slack private-file URLs.
Requests that explicitly ignore attachments still proceed to semantic
interpretation.

This is offline transport/admission proof, not a new live Slack/provider claim.
The focused Slack action suite passes 49 tests plus Ruff, compilation, and diff
checks. The previously recorded broader Slack action, contract,
Orchestrator-context, and WorkItem gate remains at 402 tests; authenticated
Slack PDF/image acquisition and retry in the sibling bridge remain the live
boundary.

## 2026-07-18 Newest Slack Instruction Checkpoint

A continuation-envelope regression remained after the original direct
context-agent bypass. Without a selected-context file, the first parse correctly
recovered a newest bare switch such as `CoS`, `Gmail triage`, or `OS`, but the
CLI's second parse allowed only bare context-agent aliases. The switch could
therefore become an unnamed clarification request and enter a blocked WorkItem.

Verified Slack continuation envelopes now receive the same full bare-agent
parsing policy as selected Slack context. An explicit newest agent/object ask
supersedes the outer historical route while prior results remain bounded
evidence only. The previously failing fixture now selects Chief of Staff,
stores only the current request in canonical WorkItem state, completes on the
Chief route, and uses zero model calls. Equivalent offline coverage spans
Chief, Gmail Triage, Business Research, Opportunity Scout, and Google Workspace.
The sibling Slack bridge's direct-switch and provider-affinity tests also pass
read-only. A worker reload and fresh live Slack proof remain required before
closing the stale-WorkItem issue.

## Current Matrix

| Agent family | Interpretation / route | Typed tool / provider | Useful reasoning | Lifecycle or read proof | Safety / continuation | Current boundary and next proof |
|---|---|---|---|---|---|---|
| Orchestrator | Proven across direct and graph entrypoints; the corrected source-bundle CLI selected deterministic preflight plus the WorkItem/LangGraph path live | N/A control plane | Proven for bounded supplied-material orchestration into one specialist SDK synthesis | N/A | Target/source identity, typed handoffs, conditional approval, request ceiling, usage receipt, and no-write boundary pass offline and live. The real Slack graph completes Gmail → Research → Outreach with provider IDs internal and one updated-in-place reply | Direct Business Research, the connector-backed graph, direct Zotero, and the exact natural-ask constraint probe all have live Slack evidence, so current readiness is 4/4. |
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

### 2026-07-13 direct Slack source-owner checkpoint

A user-origin Slack request for the most recently added Zotero journal article
with a stored abstract now passes through Orchestrator interpretation into the
direct `zotero_context_agent` owner. The provider read resolves the current
user library when a stale configured user-library ID returns 403, orders
top-level journal articles by `dateAdded desc`, requires a stored abstract, and
supplies the selected metadata before one tool-free synthesis turn. The final
approved run returned the exact title plus a substantive abstract summary in
the correct Slack thread with one specialist request, an estimated `$0.02862`,
about 24 seconds elapsed, and no web search, Zotero mutation, email, or
unrelated Slack post.

Same-thread Zotero follow-ups retain the selected article and expose bounded
read tools for missing author, publication, DOI/URL, and abstract metadata.
The compact direct runtime profile has also been synchronized across Business
Research, Opportunity Scout, Gmail Triage, Outreach Composer, and all context
agents. Runtime selection is request-aware: bounded prepared-evidence asks use
compact tool-free synthesis, exact source operations receive only their owning
tool tier, and broad or multistage asks retain the fuller profile. Prepared-
evidence WorkItem/LangGraph nodes now follow the same segmentation so planner
and reviewer nodes remain tool-free, retrieval and mutation nodes receive only
their stage-specific tools, and synthesis nodes consume compact evidence
packets. Offline cross-route and affected-agent gates pass; however, the latest
prompt reduction has not consumed a fifth live request, and this Zotero pass
does not by itself prove diverse user-origin Slack agents.

Current ownership boundary: Airtable is being validated in a separate session
and is intentionally excluded from this direct-call pass. Opportunity Scout is
also preserved as-is because its compact deterministic-retrieval plus tool-free
synthesis path already has a successful human-authored Slack proof and its
focused regression contract remains green. Neither agent should be changed here
without new regression evidence.

The approved current Slack/API batch is therefore not a fresh cross-agent pass:
Calendar proves provider-verified thread create/update/notes behavior with zero
model requests, and Zotero proves one post-fix direct source-owner model run.
The failed Airtable attempt is owned by the separate session, while older
Business Research, Gmail, Outreach, and Opportunity receipts predate the latest
shared prompt/profile synchronization. A new live request must not be inferred
from offline success; diverse post-change Slack acceptance still needs a new,
explicitly approved allowance.

A same-thread Zotero metadata follow-up at 16:02 ET exposed a separate
continuation-front-door defect. The Slack bridge supplied a valid nested
continuation envelope but no history-context file. KBA therefore skipped
operator-request unwrapping, treated `Previous` as a company name, and spent one
Business Research request on unrelated stale web evidence. The result violated
the explicit no-web constraint and is a failed acceptance run. Continuation
unwrapping is now independent of context-file presence, and bounded prior-result
identity is recovered directly from the envelope for same-object references.
Focused continuation/direct/Zotero coverage passes 64 tests plus Ruff and diff
checks. A clean Slack retry remains required; an Opportunity Scout ask entered
later in the same thread is cross-agent contamination and is not Zotero proof.

A separate novel direct Zotero projection passed at 16:54 ET. The operator
asked Business Research to select the most recently added Zotero journal
article and return only its title, authors, and publication title. Orchestrator
correctly delegated to `zotero_context_agent`; one authenticated provider read
selected one ordered article, and one specialist request produced the bounded
response. The successful Slack result returned the exact title and explicitly
marked authors and publication title unavailable in the provider metadata.
Persisted evidence shows one planner request, one specialist request, no repair
request, about 16 seconds end to end, an estimated `$0.0260085` total model
cost, and no provider write, email, or unrelated Slack post. Two preceding
acceptance failures exposed general contract defects: mapping three projected
fields to a three-item count, and relying on optional planner `required_terms`
instead of the preserved objective. The merge boundary now accepts item-count
constraints only when the deterministic parser found an explicit bullets/items/
results count, while exact field projection uses the preserved operator
objective. The related focused gate passes seven tests plus Ruff, compilation,
and diff checks.

The Zotero read contract now preserves the provider schema through synthesis
instead of reducing an article to title/abstract fields. A request can project
all available parent-item metadata or named fields, and missing author or
publication-title values can be enriched from DOI/Crossref metadata with
provenance. Notes and attachment metadata are acquired from the exact parent
item only when requested. PDF text is a separate, explicit read: the tool
verifies the attachment belongs to that parent, verifies its PDF type and byte
signature, extracts bounded text in memory, and does not persist or modify the
file. A no-PDF/full-text constraint keeps the run metadata-only. Focused source,
context-agent, architecture, planning, and CLI coverage passes 432 tests. When
multiple PDFs are attached, the direct path does not guess; it returns the
bounded candidates and asks the operator to identify one. This
is offline contract proof; notes/PDF retrieval still needs a future live Slack
acceptance run under a newly approved API-test allowance.

Direct external-tool catalogs are now request- and operation-scoped for both
reads and writes. A Zotero notes/PDF read receives four exact read tools; an
ordinary note-mutation request receives only parent metadata/children tools and
the explicit unsupported-native-mutation contract, never disposable test-note
tools. A marked test-note lifecycle receives the single lifecycle tool plus its
two prerequisite reads. Google Doc updates and Sheet row appends each receive
four target-specific schema/read/write tools instead of the full Workspace
catalog. The offline diverse-ask acceptance matrix now covers 16 read or
deterministic cases across eight routes, including Zotero and Google Workspace;
all 17 unique proof nodes pass. This is latency/capability contract evidence,
not a new live Slack acceptance claim.

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
That live quality boundary now also passes: one two-turn local SDK session kept
the initial cross-agent research/opportunity/outreach request Chief-owned, then
applied an exact correction to Business Research while rejecting stale scouting
and outreach. All 13 checks passed in two `gpt-5.4-mini` requests at a maintained
`$0.00573` estimate, with no tools, provider reads/writes, side effects, retry,
or persisted session.

Ask-shape preservation now has a typed cross-agent contract. Explicit breadth,
evidence depth, source preference, exact/strict filtering, output form,
selected/prior-context dependency, permission state, cost mode, and stop
condition survive deterministic planning, weaker LLM-plan merges, WorkItem
context-pack handoffs, and final response synthesis input. The policy constrains
execution but cannot grant approval. Exact no-padding, quick selected-thread,
table-output, and staged-approval cases pass offline.

ANU-65 source triage is now typed across the pre-synthesis boundary. Retrieval
retains the full `SourceTriageResult`; WorkItem context packs and final response
synthesis receive a bounded `SourceTriageSummary` with retained, review,
rejected, and deepen source identities/URLs, decision counts and rationales,
recall gaps, and the next retrieval action. Empty and populated context packs
preserve their prior JSON shape. Rejected sources and unextracted deepen-needed
sources remain unavailable as factual support.

Specialist request coverage now has one shared output-audit envelope across
Gmail, Research, Company Profile, Opportunity, and Outreach results. The model
is still asked to keep complete coverage consistent with unmet dimensions,
broadened scope, output form, and stop conditions, and to identify the unmet
dimension plus next safe action for partial or blocked coverage. The runtime
now normalizes contradictions conservatively and flags them for attention
instead of rejecting the whole specialist result; this audit cannot override
authoritative safety or provider gates. Final synthesis receives these typed
envelopes and cannot treat absent coverage as verification for exact filters,
requested output forms, or stop conditions.

Adapted handoffs now prove request-shape preservation separately from type
compatibility. The assessment reports missing, lossy, and defaulted policy
fields and distinguishes compatible, warning, clarification, and blocked
states. Losing an exact filter, selected-context dependency, permission
boundary, or stop condition blocks; losing ordinary output/depth shape requests
clarification. Orchestrator→Gmail, Gmail→Research, Opportunity→Research,
Research→Outreach, and Chief→context-agent transitions pass no-live coverage.

ANU-61 now has reusable exact-ask live reasoning proofs below the Slack
acceptance layer. The compact Opportunity assessment passed offline
revalidation with one request, 26,167 input and 998 output tokens, a maintained
`$0.02411625` estimate, both supplied URLs retained once, explicit fact versus
interpretation separation, bounded geography/timing uncertainty, and zero
tools/search/outreach/writes. The research-to-Doc plan used one request at a
maintained `$0.027426`, preserved ten official-page claims and the exact source,
and staged an approval-gated `KNIOps` plan without a Workspace write. Neither is
misclassified as a full pilot PASS because the Slack permalink/entry layer was
not exercised. The compact Opportunity prompt was subsequently reduced from
119,733 to 31,503 characters while preserving shared memory/writing policy,
safety, and the four relevant evidence/action skills. Its live usage fell to
8,029 input and 1,123 output tokens at a maintained `$0.01107525`; all checks
passed. A matched no-tool generic baseline passed at `$0.005883`, so ANU-175
correctly records `not_supported` plus an estimated-cost regression for this
simple one-turn case rather than inventing a KBA advantage.

ANU-175's broader weekly-operations baseline harness is now ready offline. It
binds the same natural-request hash to the saved KBA result and a no-tool generic
baseline, validates 32 privacy-minimized assertions and 15 KBA quality/safety
checks, enforces one request with a `$0.05` ceiling, and writes a zero-request
preflight receipt. The live baseline remains unproven: the execution environment
blocked the request before process start because the assertions are derived from
internal Slack/Gmail operations. No API request or provider write occurred.
Reuse the preflight, but run the comparison only within an explicitly trusted
data boundary or replace both sides with a broad synthetic matched fixture.

Outreach selected-draft revision is now structurally bounded offline. The
approved drafting context can carry one exact approved draft identity, recipient,
CTA, and revision word ceiling. Deterministic validation rejects CTA removal,
recipient substitution, and over-limit copy before an `OutreachDraft` is
accepted, while the output records its source draft id and remains approval-
required with sending disabled. The integrated approved-research-to-warm-draft
case also proves source ids, recipient persona, style use, review state, and the
no-send invariant. This upgrades DA-OC-1 and DA-OC-2 from partial to automated;
the diverse-ask matrix is now 8 automated and 4 explicitly partial rows.

Broad weekly Gmail follow-up now has a deterministic no-provider fallback.
Given a caller-bounded set of sanitized envelopes plus an explicit seven-day
window, it assigns every message exactly once across urgent, important,
can-wait, and ignore, preserves source identities and risk labels, sorts within
buckets by recency, and suppresses all draft, label, and send mutations. A
synthetic security item, consulting inquiry, and newsletter pass the integrated
DA-GT-1 proof. The diverse-ask matrix is now 9 automated / 3 partial.

The no-live diverse-ask matrix is now 12/12 automated. Chief of Staff audits
current local automation state and ranks failed runs first without writes.
Opportunity Scout now normalizes hyphenated compound topic terms, so the exact
`behavioral-health AI` ask returns deduplicated source-backed records instead of
an accidental empty result. Business Research can turn a validated fixture
profile into a complete focused brief with fact-level citations, explicit
unknowns, Keystone-fit inference, and no raw-source or send state. The executable
runner claims only an offline behavioral pass after all twelve unique proof
nodes pass; `live_pass_claimed` remains false.

ANU-61 specialist receipt replay is ready for the compact Opportunity,
NeuroFlow Research-to-reviewed-Doc-plan, and privacy-minimized Weekly cases.
All three saved passes are validated
against their no-write boundaries and catalog ask hashes, then rendered into
bounded 1,329- and 1,663-character answer-first Slack text with visible sources.
The replay uses zero OpenAI requests, repeats no synthesis, performs no provider
write or Slack post, and cannot claim a pilot observation. Real Slack permalinks
remain the sole transport evidence missing for these three packets.

The reusable ANU-60 Slack entrypoint gate is evidence-bound now. A live-proof
boolean cannot stand alone: each passing row must reference the existing
documented permalink section, local run or WorkItem identity, human review,
request count, estimated cost, and no-side-effect result. Tests resolve both
existing evidence-document sections. The runner reports all four rows proven,
including the existing exact Abridge constraint receipt, with zero new API
requests or posts needed for this reconciliation.

The ANU-61/ANU-125 scorecard now ingests receipt replay as a separate evidence
class. Research, Opportunity, and Weekly appear as `pending_slack_transport`, with their
zero replay requests/writes and visible-source counts, while observed and
passing counts remain zero for those rows. A replay cannot be submitted as an
observation or coexist with one for the same case. The refreshed scorecard stays
`fail`: the older Gmail observation exceeds the current two-request ceiling and
also failed human-review/intervention checks. Weekly is no longer simply missing;
its replay is privacy-bound and ready for authorized transport.

Scorecard rows now expose `reuse_policy`, `model_rerun_required`,
`transport_only`, and `next_required_evidence` without changing assessment
outcomes. All three replay rows explicitly forbid model reruns and request only
Slack transport/permalink/human-review evidence. The Gmail failure explicitly
rejects promotion and narrows a future rerun to the three failed dimensions:
human review, no developer repair, and the two-request ceiling.

The exact Gmail pilot rerun is now offline-ready. The dedicated readiness gate
passes five evidence-linked regressions covering selected-thread identity,
LangGraph ownership, recommendation-only output, the no-repair path, and
compact provider context. It is catalog-hash-bound and permits at most two
`gpt-5.4-mini` requests, `$0.10`, and zero provider writes. The generated
artifact deliberately records `human_review_passed=false` and
`developer_intervention_free_live_run_proven=false`; it does not replace the
required fresh live Slack observation.

ANU-66 SDK traces now retain a bounded ordered child-step summary rather than
only aggregate tool counts. The packet exposes safe step category, tool or
handoff name, status, duration, and error kind; tests prove raw tool arguments,
model output, prompts, and private content stay out of trace storage. Manual
Slack/CLI run summaries remain the next instrumentation boundary.

Slack direct-action reliability was reverified against the current checkout.
Two-phase action attempts permit a safe retry after failure between intent and
advancement, but still deduplicate a completed attempt. Chief WorkItem
continuation also preserves the same resolved live-search and live-SDK flags
through Orchestrator preflight, specialist execution, and the persisted
`advance_started` event while keeping sends and writes disabled.

SQLite storage lifecycle is now warning-clean across the main local execution
surfaces. A centralized managed connection retains transaction behavior and
closes transient file-backed connections, while persistent in-memory stores
support explicit/context-managed shutdown. The full storage,
automation-control, and CLI slice passes 180 tests with serializer and resource
warnings promoted to errors.

WorkItem child integrity is now fail-closed and auditable. Event and artifact
inserts reject missing parents inside the write transaction, and a bounded
read-only CLI audit reports legacy orphan counts/types plus strict pass/fail.
The current configured database reports the known 41 orphan events across 16
missing parents, no orphan artifacts, and no repair performed; cleanup remains
an explicit operator-reviewed action rather than an automatic mutation.

Blocked Slack evals are now diagnostically actionable after persistence. Their
manual trace summaries retain redacted block kind/reason, blocker codes, failed
readiness gates, and the next safe action. Dashboard rollups distinguish a
normal workflow blocker from a blocked row missing its diagnostic packet, and
both remain visible even when the source row recorded zero warnings.

Slack eval execution provenance is now a first-class additive WorkItem result
contract. Finalized runs identify fixture, live SDK, live search, or combined
mode and prefer actual recorded SDK/retrieval usage for model/provider identity.
CLI-context and selected-message eval writers persist the same packet. The
dashboard applies mode-aware requirements and flags the 98 irrecoverably blank
historical rows without fabricating model or provider values.

The generated architecture visual is now a guarded release artifact rather
than a best-effort documentation file. Its tracked SVG regenerates exactly from
current registry metadata, documentation references are tested, and the
generator supports both repo-relative and absolute output paths. The refreshed
diagram reflects the current 42-tool Gmail surface.

The eval Database tab now has a stable read-only inventory contract separate
from scoring and review detail. Operators retain explicit per-dimension human
and Orchestrator scores, compact run/evidence state, and direct Review form /
Case bundle links without loading full prompts, responses, notes, comments, or
rationales into the visible table. Complete exports remain unchanged.

Eval traces now expose bounded child-step timelines on both SDK and manual
Slack/CLI paths. The latest canonical WorkItem advance contributes ordered
orchestration, model, retrieval, gate, approval, artifact, and action events;
available tool summaries add tool steps. Trace Explorer details and readiness
show the timeline while excluding event prose, prompts, responses, tool I/O,
Slack text, secrets, and PHI. Historical rows remain visibly incomplete.

The no-tool Business Research focused-brief prompt received the same bounded
optimization while tool-enabled discovery retained its full contract. The
composed prompt fell from 141,342 to 31,530 characters; a matched NeuroFlow
official-page rerun passed with 9,279 input and 645 output tokens at a maintained
`$0.00986175`, versus 32,764 input tokens and `$0.027426` before. Ten extracted
claims, exact source identity, no-search/no-tool safety, and the approval-gated
no-write `KNIOps` plan all remained intact.

ANU-213's finite presentation residual now passes. A repo-local SQLite index
scanned 12 allowlisted reviewed decks and 116 slides, searched title/path/slide
text/speaker notes with all-term lexical matching, and returned bounded evidence
excerpts. Matches promote into typed `presentation_slide_evidence` refs with
relative provenance, slide identity, deck checksum, snapshot status, and
explicit no-send/no-parent-modification metadata. The final slice used zero
OpenAI requests and zero provider writes.

ANU-216's controlled workflow vocabulary is implemented and exercised on that
same typed handoff. Canonical `kba:<dimension>:<value>` labels cover object,
workflow, safety, evidence, and storage state; aliases are bounded, unknown tags
fail closed, and deterministic gates reject approval-less external effects,
sensitive hosted storage, read-only/write contradictions, and slides without
file/SQLite provenance. This adds interoperable context without granting
permissions or replacing typed identity/source fields.

ANU-174 is now 20/20. L174-14 passed with one privacy-minimized Chief request:
45,559 input tokens including 45,312 cached, 783 output tokens, and a maintained
`$0.00710715` estimate. The answer cited the sanitized source identity,
summarized five service-area signals, retained human-review/abstracted-evidence
caveats, and used no tools, search, writes, sends, or posts. L174-19 also passed
after deterministic local arithmetic derived non-identifying negative-margin
and high-expense-load posture assertions; one request selected the lower-cost,
faster-validation option with zero tools or writes. L174-16 and L174-20 use typed
assertion layers: the research-note preview carries five domain categories plus
explicit evidence-density, limitation, and next-action relationships; the
weekly preview carries aggregated source-family workstream, status, action, and
owner-role relationships across 38 one-way source hashes. Raw/trusted-private
execution is disabled. L174-16 now passes end to end: the final assertion-backed
brief used one request, then one exact marked Doc was created, read, updated,
reread, trashed, and verified absent. L174-20 also passes: one assertion-backed
synthesis produced a useful categorical weekly packet, one exact 4,151-character
durable `KNIOps` Doc was read back, and one concise verified-link post reached
the uniquely resolved `#ops-finance` channel without repeated synthesis or a
duplicate Doc. Reuse these receipts; the trusted-runtime prerequisite for the
controlled pilot is satisfied.

ANU-223 now has a separate five-provider pre-live delegation registry and
scorecard. Calendar, Gmail, and Airtable have joined manager-entry plus
provider-operation proof and should be reused. Google Workspace and Zotero each
have green manager-owner selection and green provider lifecycle/identity/
cleanup evidence, but their manager-to-provider join is not yet proven. This
3/5 joined result intentionally separates manager-entry proof from provider
primitives.

The initial 2026-07-11 Airtable Stage A run added partial joined evidence: a natural
Chief request selected Airtable and completed one exact marked create/update/
delete lifecycle with provider read-back and confirmed absence. It is not yet a
joined pass because execution used six model requests after the CLI accepted a
four-request ceiling, and the original receipt exposed provider IDs. The
delegated-route budget estimator and provider-ID-free renderer were then fixed
offline. Those initial control-plane gaps are superseded by the passing capped
rerun below; Workspace and Zotero remain unrun.

The first capped four-request Airtable rerun remained partial. It honored
the owner limit and stayed below the `$0.10` stage ceiling at an estimated
`$0.06577095`, but schema-aware validation rejected the model's invented
`Categories=Other` value before any provider write. No record or orphan was
created. A new bounded lifecycle tool now keeps minimal schema-safe fields,
same-record identity, read-backs, cleanup, and absence verification in Python;
the direct CLI supplies a request-scoped authenticated-operator approval
reference and removes it after the run. The affected offline suite passes, but
the repair then passed live in a second capped run: two requests, an estimated
`$0.0290406`, exact create/read-back/same-record update/read-back/delete/absence,
and no unrelated write, duplicate, or orphan. Saved run `2869` re-renders with
the provider identity absent from public output, receipt, and human summary.
Airtable is now a joined PASS and should not be rerun.

The first Zotero manager-join run was partial. A complete standalone-note
ask initially exposed a routing gap (`note` was missing from the generic
internal-write object class), which is fixed offline. The corrected live run
used four requests and an estimated `$0.0575301`, but the agent silently called
the test-note writer with `live=false` twice. Both calls were previews; no note,
key, mutation, or orphan existed. Zotero Context now has one bounded marked-note
lifecycle tool with Python-owned version/read-back/finally-cleanup behavior,
explicit direct `live=true` execution context, process-local operator approval,
and provider-key-free public rendering. The second capped run below supersedes
this partial result.

The second capped Zotero run passed: two requests, an estimated `$0.0307848`,
exact marked-note create/read-back, same-note version-aware update/read-back,
marker-gated delete, and confirmed absence. No retry, duplicate, orphan, import,
Workspace write, or unrelated action occurred. Deterministic public rendering
removes the provider key from output and receipts. Manager/provider readiness is
now 4/5 joined (Calendar, Gmail, Airtable, Zotero); Workspace remains the sole
unjoined row and is not required for the current additional-provider goal.

The Slack readiness registry now has four probes. Direct Business Research,
the connector-backed Gmail→Research→Outreach graph, direct Zotero, and the
exact Abridge 20-word constraint row all have live evidence, so readiness is
4/4. The scorecard requires permalinks,
local run/WorkItem IDs, answer-first copy, visible sources or source-limit
language, one final response, trace/usage/cost evidence, and no unintended side
effects. No fixture result can mark the pending live row complete.

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
- `npm run test:ai-agents-workflow:no-live`: 714 workflow and current-context
  contract tests, seven
  LangGraph scenarios, 36/36 Slack routes, and ANU-60 preflight; zero live
  model/search/connector calls.
- Direct ANU-60 Slack acceptance now passes through the real KNI app. The Suki
  ask produced one answer-first reply updated in place, visible source URLs,
  no metadata-first preview, no side effect, Slack receipt
  `sbar_613ad9e3000b4ef39b0323bf836685b4`, and KBA run `456`. One specialist
  request plus two capped hosted-search requests cost an estimated `$0.054459`.
- The connector-backed Gmail graph passes with WorkItem
  `wi_b8a3b23a97ed41a5804f4c56c7b3f852` and Slack permalink
  `https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783802711400939`.
  It completed Gmail -> Research -> Outreach over four messages, kept provider
  identity internal, treated resolved scheduling as historical, and returned a
  model-judged KNI collaboration recommendation. Because no immediate reply was
  useful, it presented no reply copy, created no approval item, and attached an
  `outreach_recommendation` with `approval_state=not_required`. Two OpenAI
  requests cost an estimated `$0.044811`; no search, Gmail draft/send, post,
  schedule, file write, or external provider mutation occurred.
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

### Multistep natural Slack asks — 2026-07-11

- Four distinct read-only asks were launched in `#ai-agents-workflow`: Eze
  Gmail -> NeuroBlu datasource research; today's Gmail -> opportunity ranking
  -> organization research; RSS signal selection -> organization/product
  research; and Eze Gmail -> Holmusk organization/market-position decision
  brief. No Gmail draft, send, label change, schedule, share, or other provider
  mutation occurred.
- The NeuroBlu run invoked the 11-node LangGraph Gmail -> Business Research
  path and preserved the correct thread, Holmusk target, and NeuroBlu product.
  Retrieval found the needed scale, longitudinal, care-setting, NLP,
  de-identification, and claims-linkage evidence, but the terminal selector
  initially displayed generic headings instead. Evidence ranking now favors
  product-specific scale/provenance facts, compacts multiline facts, excludes
  unused sources, and renders an explicit organization section.
- The first today-inbox run exposed planner instruction words in the Gmail
  query (`Exclude Select Compare Return`). The unchanged rerun removed those
  terms but skipped Gmail and researched the request phrase as companies
  (`Workspace`, `Google`, `Youtube`). Broad-inbox query sanitization and
  Gmail-first route precedence now have focused regressions; the corrected
  end-to-end sequence still needs a live confirmation.
- The RSS ask first exposed a false Calendar create route from informational
  `funding event` plus negated `do not ... schedule` wording. The calendar
  planner now removes negated action clauses before activation. The unchanged
  rerun then stopped correctly before model execution because its generic
  5-20-request estimate exceeded the Slack ceiling of eight.
- The organization-focused Eze ask returned unrelated xCures, Abridge, and
  Mercosur opportunity context instead of the selected Gmail thread and
  Holmusk. It did not expose a canonical WorkItem or graph receipt in the
  inspected database. This is a current-context/route isolation failure and a
  user-facing answer-quality failure, not a valid organization brief.
- Persisted WorkItem receipts account for five `gpt-5.4-mini` requests across
  the successful and failed Gmail runs. The final unrelated opportunity output
  additionally reports one successful `agents-web-search` request; exact
  aggregate model-versus-hosted-search accounting is partial because that run
  did not persist a matching WorkItem usage receipt.

### Opportunity Scout Slack quality runs — 2026-07-12

Four serial read-only Opportunity Scout submissions were run in
`#ai-agents-workflow` under an eight-request per-command ceiling. None produced
a completed opportunity answer, so Slack-visible response quality is currently
**FAIL at the execution/transport layer** rather than merely weak at synthesis:

- Broad run 1 (`sbar_b2b2ad44beb148be87e665f11ded988f`,
  `https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783875420058549`)
  blocked before any model call because the redundant live manual planner plus
  Scout turn policy estimated nine requests against the ceiling of eight.
- Broad run 2 (`sbar_a9719b1dab954303a647cacea93e4547`,
  `https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783875604291279`)
  exposed that the specialist child received only `Quality` as its topic. The
  malformed in-scope child was terminated to stop further API use.
- Broad run 3 (`sbar_f35b83ecedb64079a33ec5b450ead1a0`,
  `https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783875844397919`)
  received the complete raw request and no duplicate planner, but exceeded the
  child execution window during live retrieval. Its Slack run row remained
  stale in `running` after both child processes exited.
- Precise grant run 4 (`sbar_fb343231765447818243256b162ff8a1`,
  `https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783876137266479`)
  also received the complete raw request and correctly stayed in the grant-only
  plan, but timed out waiting for a live provider. Slack updated the root status
  to a useful failure while leaving a separate later `Still Running` notice in
  the thread.

The KBA path now skips the redundant live manual planner only for directly
named Opportunity Scout requests, preserves the complete raw operator request
as the child topic, and reuses page-verification results across retrieval
rounds. Verification is capped at four unique pages per Scout run instead of
four pages per phase. Focused CLI/Scout/retrieval validation passes 311 tests,
plus Ruff and `git diff --check`.

Remaining proof and improvement boundary: do not claim response-content quality
until one post-fix Slack run reaches a terminal answer. The next authorized run
should verify the unique-page cache under the precise grant case before trying
the broad portfolio again. The sibling Slack runtime should also reconcile
stale `running` rows after child exit and replace or suppress delay notices when
a terminal result arrives. KBA should persist bounded partial retrieval evidence
before timeout so a failed live provider can return verified/review candidates
instead of losing the entire result. No outreach, Gmail draft/send, scheduling,
application, or external business-system write occurred. Slack posts were the
only requested external writes.

OpenAI Admin Usage reported 11 organization-wide completion requests and
172,561 input/5,336 output tokens in the overlapping 20-minute window, but
concurrent KBA activity prevents attribution to these Scout runs. Admin Costs
remained on the lagging prior-day `$2.29533917` raw bucket; no exact session
cost delta is claimed.

The subsequent bounded retest added an overall 90-second Scout retrieval
deadline and structured partial diagnostics. A retrieval-only grant case then
proved that the existing default policy still fans an explicitly requested
SearXNG run into hosted Agents web search and optional Exa/Tavily deepening.
The exact 18:21-18:31 UTC Admin Usage window recorded five organization-wide
completion requests (75,153 input and 3,138 output tokens); two hosted-search
requests are directly present in the persisted retrieval receipt, while exact
attribution of the other three remains uncertain. Live testing stopped at that
point rather than broadening further.

That grant case also exposed two deterministic classification defects now
covered by tests: `remote-compatible` no longer converts a grant/fellowship ask
into a role search, and an explicit grant query lane now takes precedence over
incidental conference or clinical-trial wording. The retrieval returned bounded
review candidates instead of padding recommendations. A future live retest must
explicitly disable hosted/deepening providers when its budget counts total
OpenAI requests, and response-content quality remains unproven until a terminal
single-synthesis run succeeds.

A follow-up isolated retest applied those disables to one process only; repo
defaults were not changed. The resolved policy was SearXNG-only with no parallel
fanout or deepening. The run reached a terminal SDK result with exactly one
recorded `gpt-5.4-mini` request, 40,898 input tokens, 975 output tokens, and a
`$0.035061` maintained-price estimate. SearXNG returned no source candidates,
so Scout correctly produced zero recommendations and a blocked/partial coverage
result naming the missing current status, deadline, eligibility, and source URL.
This proves temporary provider isolation, terminal synthesis, and no-padding
behavior; it does not yet prove useful live opportunity recall.

Two additional Opportunity Scout runs were submitted and visibly confirmed in
Slack `#ai-agents-workflow` on 2026-07-12. Both reached a single clean terminal
status, proving the Slack execution/transport path no longer stalls for these
requests, but neither produced an accepted recommendation. Test 1 found official
NIH `PAR-25-310` and retained it for review; a legacy company-name gate rejected
the formal grant identifier as an article-style name. Test 2 found official NIH
and Grants.gov candidates, including `RFA-MH-27-180`, but a second company-fit
gate required literal AI keywords even though the request also allowed
behavioral-health implementation and evidence-generation grants.

Both company-centric gates are now removed for formal grant entities. Formal
page verification also retains bounded deadline, eligibility, and electronic
application windows from long source pages instead of only the first 1,000
characters. Local read-only validation now accepts `PAR-25-310` with active
status, an upcoming 2026-10-05 deadline, explicit for-profit/small-business
eligibility, and the official NIH application page. The two-run Slack ceiling
was honored, so useful Slack-visible opportunity recall still requires one
future proof run after these final deterministic fixes.

Cost review of the two Slack grant runs shows that they were larger than the
recent cross-agent median but not larger than historical Opportunity Scout
runs. They used 43,737 and 48,253 synthesis input tokens with maintained-price
estimates of `$0.04133025` and `$0.04462275`; the recent live-agent median was
31,422 input tokens and `$0.0257715`, while older Scout runs commonly used
49,755-53,945 input tokens and `$0.05786475-$0.07344525`. Each Slack run also
made two hosted-search requests, adding roughly `$0.0091-$0.0121`, plus
SearXNG/Exa/Tavily requests.

Retrieved Scout synthesis now attaches zero tools because deterministic
retrieval is already complete before the model turn. This removes 53 unused
tool schemas (about 35,065 prompt characters, roughly 8,766 tokens by a simple
character estimate) while preserving the full structured output, source
context, safety gates, and one-request synthesis contract. Provider deepening
remains unchanged until a source-recall eval proves that reducing Exa/Tavily or
query breadth will not weaken broad opportunity discovery.

A post-fix natural Slack proof at 2026-07-12 16:34 ET did not reach synthesis.
The concise grant request routed to Opportunity Scout, but the child exceeded
the Slack bridge's 120-second process timeout while waiting on live retrieval.
The persisted Slack record is `failed` for run
`sbar_2bfe9613467f4076b230277b8401ff84`; no Opportunity Scout `agent_runs` row
or synthesis usage receipt was written, and no matching child process remained.
The thread then visibly retained a contradictory `Run Still Running` message
after the running card was edited to the terminal failure. Delayed Admin Usage
resolved the cost: two hosted-search requests used 13,820 input and 311 output
tokens, then the uncapped synthesis request used 42,304 input and 21,009 output
tokens. At the maintained model rates plus two web-search tool calls, the run
cost was approximately `$0.158`; synthesis output was the dominant cost.

Retrieved synthesis now uses a dedicated compact agent instead of the full
tool-capable Scout prompt. Its static instruction surface is 13,922 characters,
down 90.5% from 146,072; it has zero tools, low reasoning and verbosity, and a
hard output cap of 1,800 tokens for a one-result run, scaling only to 3,000 for
five results. The model emits only bounded inclusion and operator-judgment
fields; Python merges those judgments onto the complete deterministic records,
sources, scores, deadlines, eligibility, and diagnostics before persistence or
Slack rendering. This avoids asking the model to regenerate a full record that
serializes to roughly 2,900-5,600 tokens by itself. The named Scout child timeout
now reserves 60 seconds after the configured retrieval deadline unless an
explicit child-timeout override is set.
The Slack integration updates the existing running card for delayed notices
instead of posting a second durable `Still Running` reply. Local verification
passes 516 focused KBA tests and 225 Slack routing/run-state tests, including a
fake-model compact-to-full record replay. A completed
live run is still required to prove useful output and actual post-change cost.

A one-request source-specific Slack proof then validated the post-change cost
and transport path without exceeding the earlier allowance. Hosted web search,
Exa, and Tavily were disabled only for this run and restored afterward; the
task-started KBA SearXNG/Colima runtime was stopped. Run `4747` completed with
one `gpt-5.4-mini` request, 5,877 input tokens, 121 output tokens, and a
`$0.00495225` maintained-price estimate. This is about 96% below the prior
`$0.1262685` synthesis estimate. Slack used one status card that became one
terminal result; connector and desktop inspection showed no duplicate or stale
`Still Running` reply.

Content acceptance remained partial: a direct local SearXNG query for
`PAR-25-310` returned the official NIH page first, but Scout's nine generated
queries omitted the exact identifier and returned zero candidates. Query
planning now extracts formal identifiers such as `PAR-25-310` and
`RFA-MH-27-180`, runs only the exact high-precision grant query first, and
broadens later only if that result is absent or unusable.

The offline portfolio gate now distinguishes safety from usefulness: an
all-empty portfolio is `incomplete`, not `pass`; case definitions specify
required lanes and minimum verified yield. This exposed and fixed an additional
routing defect where an industry collaboration, advisory-board, facilitation,
and consulting request collapsed into conference search because it contained
`workshop`. It now uses dedicated collaboration and consulting/advisory lanes,
including bounded follow-ups. The focused no-live gate passes 525 tests. A
future broad-recall run must still prove useful verified Slack recommendations
with the ordinary provider policy.

Founder/company personalization is now explicit rather than inferred only from
the generic Keystone prompt. The approved structured profile includes company
and founder identity, strategic priorities, all broad opportunity lanes,
remote-first access preferences, fit dimensions, exclusions, and public links.
The default Scout CLI path injects that profile into retrieved-evidence
synthesis, while receipts expose only safe profile metadata and a
`search_context_complete` check; raw CV text remains excluded. End-to-end local
SDK coverage confirms the default profile reaches the typed model input. The
expanded Opportunity/identity regression gate passes 422 tests with no API
calls.

A two-request professional-development batch then tested current remote
workshops, certifications, and networking communities. Call 1 used 10,110 input
and 219 output tokens with a `$0.008568` maintained estimate. It loaded the
complete approved founder/company search profile and correctly returned no
ranked record instead of padding. The trace exposed that `advisory` was
incorrectly activating company-news source lanes for professional-development
asks and that certificate/training sources were not recognized as institution
or program evidence. The shared source registry and query plan were corrected.

Call 2 used 5,519 input and 103 output tokens with a `$0.00460275` maintained
estimate, but returned zero raw results because every configured SearXNG engine
was suspended or CAPTCHA/access blocked. This is a provider-availability
failure, not useful-recall evidence. Scout now skips SDK synthesis entirely when
live retrieval returns zero raw results and no review candidates, producing a
structured no-evidence result with zero model requests instead. The combined
Opportunity, source-policy, SDK, identity, and architecture gate passes 472
tests. Admin Usage snapshots showed no immediate movement, which may reflect
ingestion lag; backend usage receipts remain the batch evidence.

The Slack renderer now presents mixed non-company records by `entity_name` and
`opportunity_kind`, with current status, access mode, deadline, eligibility,
official action path, fit, evidence, caveats, and suggested review. Its 172-test
offline acceptance gate passes.

A human-authored broad professional-development Slack proof then completed at
`https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783895108867709`.
It requested up to three current remote workshops, certifications, or networking
communities and returned one verified current result rather than padding: the
official Johns Hopkins AI in Healthcare certificate, with remote access, a
2026-07-16 application close, fit rationale, caveat, and direct action URL.
SearXNG broad recall used capped Exa/Tavily fallback; hosted Agents web search
was temporarily disabled and did not run. The compact synthesis used exactly
one `gpt-5.4-mini` request, 16,909 input and 419 output tokens, with a
`$0.01456725` maintained-price estimate and a `$0.25` budget guard. Slack
updated one status card to one terminal reply with no stale running notice.
Temporary test flags were restored and the task-started search runtime stopped.

This closes the post-change useful-output and cost proof for ANU-230 and proves
one expanded non-company lane through natural Slack. It does not prove every
portfolio lane live; grant, collaboration/advisory, conference, and additional
networking breadth remain covered by deterministic/eval gates until separately
selected for bounded live sampling. The immediate Admin Usage snapshot remained
empty because its returned buckets ended before the run; the persisted run
receipt is the attributable cost evidence.

Provider health accounting now treats SearXNG HTTP-200 responses with zero
results plus `unresponsive_engines` CAPTCHA, suspension, or access failures as
a typed provider failure rather than a successful empty search. This allows the
configured fallback ladder to run. Legitimate zero-result responses without
engine failures remain valid empty searches and use the zero-cost synthesis
bypass. A mocked end-to-end provider replay proves that the exact degraded
SearXNG payload now invokes the configured Exa fallback, retains the fallback
result, records SearXNG as failed rather than used, and marks
`provider_error_fallback_used=true`. The expanded retrieval, SDK, and
Opportunity gate passes 496 tests.

Two fresh root Slack asks on 2026-07-13 exposed a direct-call ownership and
response-shape gap. `OS, who is Abridge and summarize the company in 20 words`
was executed as an opportunity search and correctly found no opportunity, but
the company-identification ask belonged to Business Research. The equivalent
named BA ask reached the correct specialist, yet returned a full research card
instead of the requested 20-word answer. Its persisted run used a broad search
profile, including six queries, selected-page extraction, two hosted-search
calls, and 87.4 seconds of retrieval time before one specialist synthesis
request. This is failure evidence, not acceptance proof.

The shared direct-call boundary now hands company-profile asks from Opportunity
Scout to Business Research while preserving Scout for actionable opportunity
asks. Explicitly brief company identification uses the Orchestrator-interpreted
ask shape to select a two-query quick retrieval profile, disable semantic and
research deepening, skip selected-page extraction, and omit the separate LLM web
query planner. The raw operator request now reaches retrieval quality and source
lane selection instead of being replaced by the broader canonical company
research prompt. Focused company output includes a dedicated LLM-written
`answer`; deterministic rendering prefers that field and validates the exact
word contract without expanding it into generic sections. The subsequent clean
Slack BA run proved the corrected route and exact 20-word output in one model
request; its permalink, usage, cost, and review receipt are recorded in
`docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md`.

### 2026-07-13 direct lifecycle latency checkpoint

Three user-origin natural-language Slack lifecycles were run serially under a
12-request batch ceiling. Google Workspace created, read back, and trashed one
marked Doc successfully, with independent Drive Trash confirmation. Airtable
correctly stopped before mutation because the separate marked-record delete
gate was disabled. Zotero reached its owner but did not receive the existing
test-note lifecycle tool, so it returned a truthful no-write result rather than
false success. Actual model usage was 3 Airtable requests, 4 Workspace
requests, and 3 Zotero requests: one Orchestrator interpretation for each plus
2, 3, and 2 specialist requests respectively. The batch stopped at 10 requests
after Workspace exceeded the planned three-request per-command ceiling.

The general offline repair now recognizes natural create/change/delete wording
for the marked Zotero-note composite, exposes one marked Google Doc
create/verify/trash composite, and keeps guarded Airtable, Zotero, and Workspace
composite lifecycles on the compact direct profile. Each composite now budgets
one Orchestrator request plus one specialist tool-call turn and one specialist
synthesis turn. Calendar retains its separate one-turn LLM interpreter before
deterministic provider mutation. The affected four-provider acceptance gate
passes 97 tests, and the focused contract/registry gate passes 43 tests plus
Ruff and `git diff --check`. These are offline proofs only: Zotero, Workspace
request reduction, Airtable with its cleanup gate, and a fresh Calendar
lifecycle still require a newly approved Slack/API batch and provider-visible
confirmation.

### 2026-07-13 direct lifecycle variation batch

A newly approved serial batch exercised four natural-language direct-context
agent calls under an exact 12-request and `$0.20` stop condition. Admin Usage
recorded exactly 12 model requests: one Orchestrator preflight plus two
specialist requests for each of four Slack calls. The combined usage was
189,205 input tokens, including 62,464 cached tokens, and 5,179 output tokens.
At the maintained `gpt-5.4-mini` rates this is approximately `$0.12305`, below
the approved ceiling. Each call completed in about 23-39 seconds.

The first Zotero Context call selected the intended marked note lifecycle, but
Zotero rejected both the provider create and a separate read-only
`/keys/current` capability check with HTTP 403. No note was created. Direct
Zotero writes now run a secret-free key-capability preflight and require verified
user-library read/write access before the specialist call. A new write-enabled
key and fresh Slack proof remain required.

The first Google Workspace call exposed a natural-language admission gap:
`make` was not recognized as create, and the word `Drive` added generic read and
trash tools after lifecycle selection. No file was created and both generic
calls returned 404. The corrected direct contract recognizes `make`, keeps the
bounded profile, and exposes only `google_doc_test_lifecycle` for a marked
create/verify/trash ask. The single allowed retry then created the exact marked
Doc, verified title/body, trashed it, and verified `trashed=true`. An independent
exact-ID Drive read confirmed the title and trash state. Desktop visual proof
remains pending because the active macOS session showed only the lock screen.

The Airtable Context call selected only `airtable_test_record_lifecycle` and the
provider lifecycle passed create read-back, same-record update read-back, marked
delete, and absence verification. An independent read of Business Expenses
found zero records containing the marker. Slack nevertheless rendered a false
blocker because the post-run verifier recognized standalone `create` and
`update` receipts but not the successful composite receipt. The verifier now
accepts a lifecycle only when all three read-back checkpoints pass. A fresh
Slack run is still required to prove the corrected success rendering; provider
mutation and cleanup are already proven for this case.

The direct Calendar owner is Chief of Staff rather than a separate Calendar
context agent. An explicit `@KNI CoS` Calendar request enters the dedicated
fast path before WorkItem or manager-graph execution, uses one bounded
structured interpreter in live mode, and keeps exact identity resolution,
approval, mutation, and read-back in Python. Focused acceptance now covers an
actual named `@KNI CoS` create mention plus a named natural delete that resolves
one provider event and requires verified absence. The Calendar-focused CLI gate
passes nine tests. This is static/fake-provider evidence; a fresh natural Slack
create/update/delete sequence and visible Calendar confirmation remain pending
under a new live-test allowance.

### 2026-07-13 repaired direct lifecycle checkpoint and next tests

A fresh Airtable Slack lifecycle now renders success after create, same-record
update, exact marked-record delete, and absence verification. An independent
provider query found zero remaining `KBA_TEST_RECORD_ANU120_R9` records, and the
temporary delete gate was removed.

The Calendar Slack create completed in about ten seconds and provider read-back
confirmed the exact title, date, and 2:00-2:30 PM Eastern interval. The first
multi-change update moved the same event ID to 3:00-3:45 PM but silently omitted
the requested note. Root cause: a complete deterministic time plan was retained
without merging a source-anchored note extracted only by the LLM. The repaired
merge preserves verified deterministic fields and adds only model fields whose
value or evidence occurs in the operator request. A second Slack update saved
the note, and exact-ID provider read-back confirmed time and description. The
disposable event was then deleted through the already-approved zero-model
cleanup path and absence verification passed. The Slack bridge still hardcodes
the displayed Calendar request count to zero even when KBA metadata contains
the real count; that renderer defect belongs to the `keystone-slack` checkout.

The completed live window ingested seven OpenAI requests and approximately
`$0.06852` at the maintained test rates. Cost stayed below the `$0.10` stop
condition, but the six-request ceiling was exceeded by one because a diagnostic
interpreter call was tracked outside the serial batch budget. No further live
model calls were made. Future batches must reserve diagnostics inside the same
request ledger before execution.

Verified provider operations now carry a `provider_link` receipt field for
Google Calendar, Airtable, Zotero, and Google Docs. The direct context-agent
Slack summary renders only HTTPS links from successful receipts whose provider
verification passed; blocked, failed, or partially verified writes do not get
a success link. The dedicated Calendar renderer in `keystone-slack` now renders
the verified Calendar receipt link and actual KBA request count rather than a
hardcoded zero. It also treats a nominally successful but unverified provider
receipt as `Needs Attention` instead of false success. Focused Slack renderer
tests pass; a fresh live Slack receipt remains required.

The Calendar renderer patch is deployed to the running local Slack Socket
worker and the post-restart websocket connected successfully. The next live
acceptance batch still requires a fresh operator-approved budget because the
prior batch exhausted its request ceiling. Proposed batch boundary: run the
four source-owner workflows serially; allow at most nine OpenAI requests and
`$0.15` total, at most 60 seconds per Slack action, no automatic retries, and
stop immediately on false success, missing provider read-back, missing provider
link, or an unexpected side effect. Take cost/usage snapshots before and after.
For each success, open the link from the Slack response and visually confirm
the exact provider object in Calendar, Airtable, Zotero, or Google Drive rather
than treating API read-back alone as visible-app proof.

The same bridge contract now covers Airtable, Zotero, and Google Workspace.
KBA emits a bounded `verified_provider_links` field in addition to the human
summary, and the Slack bridge renders only HTTPS values from a successful
result. A blocked result suppresses even a declared link. Focused bridge tests
prove all three source families plus Calendar, and the worker was restarted
again with a successful websocket hello. This is deployed offline evidence,
not a substitute for the pending provider-backed Slack and visible-app runs.

The pending batch is now executable as a zero-model manifest rather than a
free-form checklist. `scripts/render_provider_slack_acceptance.py` validates
five natural-language cases across the four providers: four verified-success
workflows plus one unsupported Zotero deletion blocker. It pins route ownership,
expected operations, cleanup wording, provider-link host, visible app surface,
60-second per-action limits, serial/no-retry rules, and the nine-request / `$0.15`
batch ceiling. The generated artifact deliberately reports
`pending_live_approval`, `live_pass_claimed=false`, and
`visible_app_pass_claimed=false` until real evidence is recorded.

Run these remaining natural-language tests serially, stopping on the first
shared failure and verifying every requested field in the provider and visible
application:

1. **CoS / Calendar:** create one marked event, change title, date, time, and
   note in one ask, then delete it by natural title/date. Prove the same ID is
   modified, every requested field matches, absence is verified, and Slack
   displays the actual model-request count.
2. **ATC / Airtable:** create one marked record after a live schema read, update
   two differently typed fields on that same ID, and remove it through the
   marked cleanup gate. Prove no new select/group value is invented and no
   duplicate is created.
3. **GWC / Workspace:** create one marked Doc, append content in a thread
   follow-up, rename the same file, then trash it. Prove exact-ID continuity,
   content read-back, trash state, and visible Drive confirmation.
4. **GT / Gmail:** create, revise, read, and remove one exact
   `KBA_TEST_DRAFT` without sending. Prove authenticated-account scope, same
   draft ID, body/subject read-back, and final absence.
5. **ZC / Zotero:** after replacing the HTTP-403 key, select the newest journal
   article with a stored abstract, summarize only that abstract, then create,
   modify, and remove one marked child note. Prove live-library metadata rather
   than graph-supplied context and exact item/note IDs.
6. **BA / Business Research:** identify a company and obey a short word limit
   using the compact direct profile. Prove source-backed synthesis, latency,
   request count, and no opportunity-search detour.
7. **OS / Opportunity Scout:** ask for one current, founder-relevant
   opportunity with a deadline and source. Prove actionable-opportunity routing,
   current-source extraction, and no company-profile handoff.
8. **Thread follow-ups across owners:** use `this record`, `this file`, `this
   draft`, and `this event` after one exact prior result. Prove the owner reuses
   provider identity from thread context without phrase-specific routing or a
   manager graph.

### 2026-07-19 CoS semantic follow-up validation

Two serial natural-language `@KNI CoS` Slack runs exercised the same request
with a short reformatting follow-up. The root request passed its operator-facing
boundary: it used only the supplied company note, performed no web search or
provider write, and returned a complete answer without workflow metadata. The
run (`sbar_4db908bfabbd46858c9c7d2a0ace7347`, Slack parent
`1784470331.162919`) used one model request and recorded an estimated
`$0.04410075` cost. Its 47 admitted tools were unnecessary for the bounded
source-only task, so the user-visible result passed while tool admission did
not.

The follow-up asked for the same evidence and action boundaries in three short
bullets. It exposed the cross-entrypoint defect: the answer began correctly but
then rendered a suggested Slack route and a false Gmail-triage action button.
That run (Slack reply `1784470650.409249`) used one model request, recorded an
estimated `$0.0542205` cost, and admitted 56 tools. No search or provider write
occurred. The two-run total was `$0.09832125`, so testing stopped within the
approved two-call and `$0.10` ceilings.

The diagnosis was a divergent thread-continuation contract, not a failure of
the high-level Orchestrator-first design. Continuation boilerplate was being
mixed into the task text before tool admission; its wording accidentally
activated specialist tools, generic reply wording created an unrelated Gmail
button, the current follow-up appeared too late in the prompt, and synchronous
foreground follow-ups did not persist their final public result. The repair now:

- treats the current follow-up as authoritative while retaining prior evidence
  and safety boundaries;
- admits zero provider or specialist tools for source-only/no-live-research
  synthesis and semantic equivalents;
- renders explicit public answer fields without route or telemetry metadata;
- requires real draft/triage intent before offering a Gmail action; and
- persists the final result and allowlisted, secret-stripped telemetry for
  synchronous thread runs.

Focused KBA and Slack tests pass, including exact-prompt offline replay with
`attach_tools=false`, `specialist_tools=false`, and zero effective tools. The
final repair was then proven by the sixth same-thread live canary: one model
request, zero tools/providers/search, no WorkItem or graph, and exactly the
three requested bullets in Slack.

### 2026-07-19 Same-thread negative-capability routing checkpoint

Six same-thread CoS formatting canaries and two later distinct root canaries were
attempted before the final pair under the new ten-run allowance. The batch stopped
after each shared failure. The first two exposed mixed human/bot context
authority and implicit WorkItem admission. The third reached canonical
admission but was rejected by an over-broad Chief request estimate. The fourth
reached semantic planning, then incorrectly selected Outreach Composer because
an unbounded route regex joined the negated phrase “draft anything” to a later
occurrence of “note” across continuation sections. Outreach correctly enforced
its own prerequisites, but it should never have owned this response-only
request.

The shared planner now treats negative capability clauses as execution
constraints rather than positive owner/tool evidence. It preserves contrasted
positive instructions after “but”, “however”, or “instead”; recognizes
equivalent prior-answer correction and reformatting asks as Chief response-only
work; and rejects an LLM route override that depends on an explicitly forbidden
draft, provider mutation, or live-search capability. These boundaries reduce
the admitted capability set. They must never create clarification,
unsupported-route, WorkItem, graph, approval, prerequisite, or missing-context
blockers while the positive instruction remains feasible.

An offline replay of the exact fourth Slack envelope now selects
`chief_of_staff`, keeps `route_request`, requires no approved Outreach context,
and preserves the three negative clauses. The full manual-planner suite passes
209 tests, and the focused cross-layer negative-capability/context matrix passes
21 tests. Admin Usage
observed one completion request in the third canary's minute and one in the
fourth canary's minute; both were planning-stage calls, and neither run reached
a business provider. This also proves that the third Slack message claiming the
run was blocked before every model call was not telemetry-truthful and must not
be reused as acceptance evidence.

The focused offline gate reproduced the separate ANU-322 isolation defect: it
created no WorkItem and no tool/provider event, but it added six synthetic
`agent_runs` rows to the operator database and labeled the mocked cases
`dry_run=0`. Those rows were left intact rather than silently deleted. Broad
validation should remain gated on a temporary validation database plus
pre/post operator-table invariants.

The fifth canary separated the two concerns. It proved live that negated
capabilities no longer steer ownership or create prerequisites: Chief was the
selected owner, the effective tool set was empty, search and providers were not
used, and no graph route was selected. It still failed the requested answer
because the Chief subprocess ignored the human thread-root facts already
retained by the Slack adapter and then treated its own research recommendation
as authority to execute a fixture WorkItem. The shared child contract now
passes the typed Slack execution context into Chief, gives the human root
priority over prior bot prose, requires request or semantic-plan authority
before a recommended handoff can execute, and caps Slack SDK session history
behind the typed root-plus-recent context.

The sixth canary passed the complete boundary. Slack rendered exactly the three
requested bullets with no status title, metadata, explanation, clarification,
action button, or trailing note. Backend run 6363 selected `chief_of_staff`
with `route_request`, `requires_live_search=false`, an empty workflow, one
`gpt-5.4-mini` request, zero tool events, no provider activity, no new WorkItem
or LangGraph delegation, and a completed PublicResult; estimated model cost was
`$0.0436965`. The Slack connector and desktop UI independently showed the same
final message. This closes the exact stale-thread/negative-capability canary,
not the broader direct-specialist/provider/graph parity matrix.

The seventh canary deliberately used a new Slack root and a different
two-fact/two-bullet request. It again selected `chief_of_staff`, used two model
requests, admitted zero tools, called no provider, created no WorkItem or graph,
and produced the requested answer. Slack nevertheless titled the completed
answer `Business Agents Need Input`. Run
`sbar_63a5efc885484f289f2f14ea1b4aff45` and backend agent run 6364 prove that
the remaining failure was downstream of interpretation and tool admission:
advisory `recommended_route.workflow_type=clarification` metadata overrode a
complete answer even though `missing_information` was empty. The shared public
result contract now requires actual missing information, explicit
`needs_input`, or genuine clarification prose before assigning that status.
Chief also normalizes complete supplied-context answers to the direct
`project-context-review` route after synthesis. The repaired status boundary is
offline-proven.

The eighth canary used another new Slack root and the same answer-only task
shape with different wording. The false `Need Input` label did not recur.
Instead, the pre-Orchestrator Calendar detector treated “I have 30 seconds
before a meeting” as provider context and joined it across sentences to the
supplied architecture fact “negative constraints remove forbidden
capabilities.” It inferred a Calendar delete, used one Calendar interpretation
request, and returned `calendar_action_missing_required_field` for an event
name that the operator had never requested. Slack run
`sbar_4b85eb49e4e146ed9510bcf38c062180` created no WorkItem, provider/tool
event, or backend `agent_runs` row.

The repair replaces keyword authority with a shared two-signal admission
contract. Natural-language provider execution now requires both a shared
semantic plan with positive `business_system_write` intent and deterministic
evidence that an operation is bound to that provider in the same positive
clause. Either signal alone falls through to normal semantic execution and may
not generate a missing-provider-field blocker. Exact typed actions remain the
only planning bypass. Calendar action interpretation now runs after
Orchestrator preflight, and the generic business-system detector no longer
connects provider objects and verbs across punctuation boundaries. Failure
telemetry retains the safe block kind, child return code, selected agent, and
model-request count without exposing that metadata in the public Slack answer.

Offline proof passes the 80-case Calendar/admission slice, a 1,242-case
planner/Orchestrator/CLI/WorkItem/Slack-action matrix, the full 3,904-test KBA
suite with one skip, all 201 sibling Slack app-mention tests, and Ruff in both
repositories. The distinct post-fix canaries were completed in the final
checkpoint below.

### 2026-07-19 Final title, tool-admission, and direct-agent checkpoint

The ninth distinct-root CoS canary preserved `route_request`, used one
`gpt-5.4-mini` request, admitted zero tools, and returned the correct two-bullet
answer without provider, WorkItem, or graph activity. Slack nevertheless added
a decorative completion title. The shared PublicResult contract now carries an
explicit title-omission decision for completed exact-count responses; failure,
blocker, and clarification titles remain visible.

The tenth and final approved canary used a semantically equivalent supplied-only
ask. Slack rendered exactly the two requested bullets with no title, metadata,
action button, provider action, WorkItem, or graph. Backend run 6366 completed
with no tool event or provider receipt. The run still failed the request-scoped
capability boundary: the supplied architecture fact contained “Slack titles,”
and the Chief runner's pre-model keyword check attached all 47 tool schemas.
No tool was called, but input grew from 11,582 tokens in the ninth canary to
54,264 tokens in the tenth. The user-facing answer passed; tool admission and
cost precision did not.

The repair removes “provider keyword equals tool authority.” Provider names in
supplied facts, architecture commentary, examples, and negative constraints are
not positive tool evidence. A natural-language provider action now requires a
positive operation bound to that provider in the same clause, plus the semantic
plan when applicable. Complete supplied-context transformations retain the
explicit named agent but use a one-turn, zero-tool direct-response profile.
This also prevents Outreach approval prerequisites or Gmail/research domain
defaults from blocking a complete provider-free question merely because the
operator named that agent.

Offline parity now covers Chief of Staff, Business Research Analyst,
Opportunity Scout, Outreach Composer, and Gmail Triage with the same exact
two-bullet prompt. All five retain `route_request`, no live search, no approved
context requirement, no workflow, and the exact two-item constraint. The four
specialist direct-response builders expose zero tools; Chief uses its existing
compact zero-tool supplied-context profile. The full repository gate passes
3,930 tests with one skip and Ruff passes. No approved live call remains, so
the cross-agent comparison is offline-proven but not yet Slack/API-proven.

### 2026-07-20 Natural CoS multi-owner graph checkpoint

A realistic CoS request supplied one short company note and asked for one
decision brief containing what was supported, the strongest KNI advisory or
research fit, one first validation question, and a paste-ready internal Slack
note. The operator did not name Business Research, Opportunity Scout, Outreach
Composer, WorkItem, or LangGraph. The request explicitly prohibited search,
provider mutation, email drafting/sending, and external posting.

The first graph attempt reached the correct semantic workflow but was blocked
by request-cost admission. Slack inherited a raw `--live-search` flag from the
sibling bridge; KBA had already resolved `requires_live_search=false`, but the
estimator priced the raw flag and rejected the feasible workflow. Cost
admission now uses the resolved plan and effective retrieval state, not the raw
entrypoint flag.

The second attempt completed the intended
Business Research -> Opportunity Scout -> Outreach Composer graph, but returned
an external-email-shaped artifact. The planner had correctly recorded
`outreach_channel=slack_internal_note`, selected supplied context, and prohibited
email. The defect was downstream contract drift: source-detection, artifact
selection, and terminal rendering independently re-parsed request phrases
instead of consuming the typed plan. The repair makes the plan authoritative
for supplied evidence, internal Slack copy, ordered owner continuation, and
public result shape. Phrase detection remains a fallback for legacy callers.
The terminal renderer also removes duplicate next actions and suppresses
fixture-only source references unless the operator asks for sources.

The unchanged final canary passed. Slack run
`sbar_7f3c67b454234eec96dedb17a90dd578` produced WorkItem
`wi_d5075933327f476eb1e35d1e46c2cd43`, completed all three planned owners, and
returned one useful decision brief. The persisted research artifact is marked
`source_provided=true`; the terminal artifact is
`internal_slack_draft` with `external_write_performed=false`,
`gmail_draft_created=false`, and `send_enabled=false`. No tool event was
recorded during the run, so there was no search or business-provider call.
Slack connector output, persisted result text, and the expanded desktop Slack
thread show the same answer with no email fields, action button, route/tool
metadata, or generic command-completed text.

The saved live Opportunity artifact retained the supplied source refs but
defaulted its lineage boolean to false. A post-live offline repair now derives
`source_provided` from the typed metadata and the source refs themselves; the
exact graph regression requires that flag on both the research and Opportunity
artifacts. This additional metadata correction was not live-rerun.

The run used two `gpt-5.4-mini` requests: the manual planner and bounded
Outreach synthesis, with a maintained combined estimate of `$0.0392835`.
Focused offline verification passes 662 manual-plan/WorkItem/LangGraph tests
and 274 CLI tests; Ruff and diff checks pass. This closes the specific natural
multi-owner, supplied-evidence, review-only graph row. It does not close
provider-write graph parity or the full ANU-311 matrix. The current live
Slack/backend allowance is exhausted.

### 2026-07-20 Post-run content and concise-prompt audit

The graph canary above is now classified more precisely: backend execution
passed, but reader-facing content and rendering were partial. The WorkItem
completed all three owners and persisted the requested internal Slack copy, but
the public Slack response omitted that separate paste-ready section and exposed
a generic workflow-success title. A completed graph is not a complete
acceptance result when a named deliverable remains only in artifact metadata.

The shared output contract now treats two or more explicitly named
reader-facing components, such as a decision brief plus an internal Slack note,
as measurable response sections. This is output-shape authority only; it does
not select an owner, tool, provider, or graph. If a model proposes an unsupported
heading, the merge rejects that heading while retaining the components the
operator actually named. The internal Slack fallback renders those sections
separately, and the PublicResult omits the generic success title when the
canonical terminal artifact already owns the reader-facing internal Slack copy.

Two shorter, human-natural CoS prompts were added before any further live spend.
The direct note question remains a zero-workflow, no-search Chief request. The
stateful prompt—“Track this review ... assess what is supported, choose the
first validation gap, and give me a paste-ready internal Slack
recommendation”—preserves the same Business Research -> Opportunity Scout ->
Outreach Composer graph without naming any specialist. Its shorter verbs
exposed and repaired a general fallback-vocabulary gap: `supported`, `choose`,
and `paste-ready Slack recommendation` now express the same capabilities as
their longer equivalents without becoming deterministic routing authority.

Offline verification passes 692 affected planner/WorkItem/LangGraph/result
tests, 254 sibling Slack bridge/run-record tests with 49 subtests, Ruff, and
diff checks. A concise live Slack/backend rerun remains pending a fresh live-run
allowance; no additional model or provider call was spent on this repair.

### 2026-07-20 Concise direct and implicit multi-owner live checkpoint

Six short-prompt Slack runs exercised the shared CoS entrypoint without naming
specialists, tools, providers, WorkItem, or LangGraph. The direct prompt stated
one company fact and requested one supported fact plus one validation question.
The first attempt stopped after its planner request because the estimator did
not recognize the natural postposed phrase “from this note only.” The second
returned the right answer but still exposed the generic success title. The
unchanged third attempt passed in Slack run
`sbar_9feef0bc890b41d2a27a6f62d4b9d931`: one planner request plus one compact
Chief request, zero tools, no WorkItem, no provider event, no visible title,
and no route or workflow metadata. The Slack API and expanded desktop thread
show the same two-part answer.

The stateful prompt said only “Track this review,” supplied one fact and one
missing-evidence statement, then requested an assessment, the first validation
gap, and a paste-ready internal Slack recommendation. The first attempt
correctly stopped after planning because the cost estimator required magic
supplied-context wording and priced a generic Chief path at eight requests.
The next attempt completed the intended three-owner graph but failed
reader-facing acceptance: instruction sentences became evidence, an internal
`Answer / Detailed Summary` artifact wrapper leaked into the fit rationale, and
the live internal-Slack synthesis tripped an external-outreach claim guardrail,
so a noisy deterministic fallback was posted.

The shared repair now recognizes an implicit fact packet only when four
signals agree: the request contains an asserted fact, requests a synthesis,
explicitly forbids search, and contains no positive provider-action or handoff
request. A question without an asserted fact does not qualify. “Track this
review” is explicit durable-state authority, so a live
planner cannot collapse it into a one-off Chief response. Source-provided
research filters operator imperatives from evidence; Opportunity consumes a
reader-facing fit sentence rather than a specialist schema wrapper; and
internal decision artifacts keep PHI, security, and professional-advice
guardrails without applying outbound-claim heuristics. External outreach keeps
the original claim guardrail. If live synthesis still falls back, bounded
guardrail risk flags and reasons are retained in internal audit notes.

The unchanged final graph run passed in Slack run
`sbar_f533945cb7aa4ac4a6f16ca171ad8bcf` and WorkItem
`wi_9e32c27cd2c041f1a995ac0c14b38d02`. Business Research used the supplied
fact deterministically, Opportunity used the retained WorkItem evidence, and
Outreach produced the terminal internal Slack artifact with one live model
request. The persisted terminal artifact has
`sdk_synthesis_used=true`, `external_write_performed=false`, and
`send_enabled=false`; no tool event or provider action was recorded. Slack API,
run-record text, backend artifacts/events, and the desktop thread agree on the
assessment, first validation gap, and separate paste-ready recommendation,
with no generic title or workflow metadata.

The final graph run used two `gpt-5.4-mini` requests: planner cost
`$0.01451625` and Outreach cost `$0.02497950`, for a maintained combined
estimate of `$0.03949575`. Verification passes the 857-case shared
planner/Chief/CLI/LangGraph/result matrix, 419 workflow/graph tests, 104
Outreach/safety tests, the focused sibling Slack mention suite, and the full
repository gate (4,003 passed, 1 skipped). Ruff and diff checks also pass. One
lower-priority metadata defect remains: the persisted WorkItem
title can still inherit too much of the raw request even when its artifacts
resolve the correct company target. This does not affect the public answer,
route, tools, or safety outcome, but should be corrected before WorkItem titles
are treated as a primary operator-facing navigation surface.

## Priority Order

1. Finish ANU-309/209/206 as one shared contract slice: semantic interpretation
   determines owner and constraints; the resolved capability profile determines
   effective tools and cost; one structured result determines public rendering.
   Deterministic validation should remain authoritative only for safety,
   provider identity, exact mutation scope, receipts, and unambiguous measurable
   output constraints.
2. Continue ANU-205 incrementally so direct, Slack, continuation, WorkItem, and
   LangGraph paths consume those same typed contracts instead of independently
   re-parsing request prose.
3. Promote the passing supplied-evidence CoS graph canary into ANU-311 variation
   coverage, then add the next materially different graph row only after a new
   live allowance: provider read -> interpretation -> reversible write ->
   read-back, with exact owner/tool/result parity.
4. Keep ANU-322 test-state isolation and reproducible runtime fingerprints ahead
   of broad live matrices; validation must not contaminate operator state or
   make a passing checkout impossible to reproduce.
5. Extend current-opportunity and search/extraction validation only for new
   source-quality or freshness questions. Reuse the existing provider lifecycle
   proofs instead of spending live calls on equivalent wording.

Every proof record must include the raw request, route, backend, typed tools,
provider IDs or source URLs, structured result, reasoning assessment, safety
outcome, cleanup, request/usage/cost evidence when applicable, and the focused
fix or next blocker.
