# ANU-120 Target-Action Matrix

ANU-120 uses `src/keystone_agents/target_action_matrix.py` as the current
repo-local scorecard for natural asks that must resolve to the right owner,
target system, allowed tool tier, approval gate, and expected proof.

The matrix is deliberately data-first. It documents representative read,
research, compare, summarize, draft, label, save-plan, Airtable/CRM,
Workspace, Gmail, post, schedule, workflow, and artifact-backed asks without
adding phrase-specific planner branches for each prompt.

The same module also carries a thread-local scorecard for follow-ups whose
object identity is already present in Slack/WorkItem context. These cases cover
Gmail draft revision, Airtable record update, Google Doc append, Google Sheet
row update, Zotero article notes and PDF attachments, Slack message edits, and
prior-result link summaries. Each case records whether the owning typed tool
already supports the action or needs a separate reviewed tool contract. In
particular, the LLM can now route a Zotero note/PDF ask correctly while the
ordinary Zotero mutation remains explicitly unsupported; correct interpretation
does not imply invented provider capability.

## Thread-local tool status

| Follow-up action | Current execution status | Tool contract |
|---|---|---|
| Revise the selected Gmail draft | Supported | Exact draft/thread identity, draft-only gate, approval, provider read-back |
| Update the selected Airtable record | Supported | Schema validation, unique record identity, field-scoped write, approval, read-back |
| Append to a selected Google Doc | Supported | Explicit `content_mode=append`, exact document/folder scope, approval, content read-back |
| Update a selected Google Sheet row | Supported | Exact spreadsheet/tab and stable row key, approval, read-back |
| Add or append a Zotero article note | Not supported for ordinary items | Add a reviewed child-note tool with exact parent item, append/replace mode, version precondition, approval, and read-back |
| Attach a PDF to a Zotero article | Not supported | Add a reviewed attachment tool with local-file, MIME, size, checksum, parent-item, upload, approval, and attachment read-back checks |
| Edit a Slack message | Marked test messages only | Keep exact channel/timestamp/author marker; add a separate bot-authored production-edit contract if needed; never edit human posts |
| Summarize a link from prior results | Supported read-only | Preserve the retained source reference and visible attribution |
| Ask follow-up questions about the selected Zotero article | Supported read-only | Reuse the thread-scoped SDK session and selected item; retain authors, publication metadata, DOI/URL, and stored abstract; call bounded Zotero reads only when the follow-up needs metadata not already retained |

The Zotero additions should be child-item operations rather than generic item
patches. That keeps article metadata changes, note append/replace semantics, and
binary attachment upload rules independently reviewable and prevents a broad
write helper from silently expanding provider authority.

## Direct-call and graph alignment

Direct named-agent calls and graph-routed calls share the same typed provider
helpers, structured output schemas, approval gates, exact-target checks, and
read-back requirements. The difference is execution durability: direct calls
use Orchestrator preflight followed by the selected specialist and are intended
for one bounded operation or a thread-local follow-up; graph routes add durable
WorkItem state, checkpoints, manager coordination, and recovery for longer
multi-step work.

The route selects the owning specialist; the request shape selects that
specialist's runtime profile. A bounded read, exact write, thread follow-up,
and broad or multistage request may therefore use different instruction and
tool tiers without changing ownership. Direct calls load only the tools needed
for the current source and operation. When deterministic provider acquisition
has already supplied a complete evidence packet, the specialist performs one
tool-free synthesis turn.
Later thread follow-ups rebuild the same source specialist with its bounded read
tools, so questions about authors, publication venue, methods, or findings can
reuse the selected object and fetch only missing metadata. This preserves data
flexibility without attaching unrelated cross-system write tools to every turn.

The same compact direct profile now applies to provider-prepared Business
Research, Opportunity Scout, Gmail Triage, and Outreach Composer synthesis.
The current representative composed instructions measure approximately 38.3k,
33.5k, 48.0k, and 53.7k characters respectively, with zero attached tools after
bounded acquisition. Their full builders remain available for genuinely broad,
multistage, or tool-selecting runs and currently measure roughly 136.8k-147.0k
characters with 42-59 tools. These are request-shaped diagnostic snapshots, not
fixed protocol limits; regression tests enforce relative compaction and the
required contracts rather than freezing character counts.
This is a routing/runtime distinction, not a reduction in provider capability:
typed acquisition and write execution still use the owning tool, schema,
approval, exact-target, and read-back contracts before or after synthesis as
the operation requires.

Graph execution uses the same need-based segmentation per node rather than
replaying one graph-sized prompt everywhere. Planning and review nodes remain
tool-free, retrieval nodes receive only bounded read tools, mutation nodes
receive one source's write and read-back tools, and synthesis nodes consume
compact evidence packets without provider tools. WorkItems and LangGraph add
durable coordination, checkpoints, and repair; they do not require every node
to carry every instruction or integration.

The bounded live planner smoke covers all eight rows with `gpt-5.4-mini`, one
request per row, no model retries, no tool calls, and no provider writes. The
first Gmail run exposed a generic `continue_work_item` intent that could have
diverted execution even though the model selected Gmail; the shared planner
contract was corrected and the same case passed on rerun.

A real selected-message Slack context probe then exposed a second integration
gap: the CLI built the WorkItem transcript after Orchestrator preflight, so the
planner could not see the thread evidence. The context is now normalized and
bounded before preflight, including nested WorkItem Slack context. Explicit
`@KNI CoS` or Orchestrator follow-ups whose LLM plan selects a context owner now
enter that owner's dry/live specialist boundary instead of a generic Chief
summary. The final Zotero PDF probe returned `source=llm`,
`target_agent=zotero_context_agent`, `intent=business_system_write`, and the
specific missing PDF-attachment tool blocker with zero provider tools or writes.

The complete bounded batch used 11 OpenAI requests across eight distinct
follow-up variations, the corrected Gmail rerun, and two Slack-context
integration probes. It consumed 164,003 tokens with an estimated cost of
`$0.1202685`; no request retried, and none invoked provider tools or writes.

Focused coverage lives in `tests/test_target_action_matrix.py`. The tests
assert that the manual planner preserves the expected target action, keeps
approval-required actions draft/read-only or typed internal-write-plan only,
and blocks unsupported or underspecified post/schedule/write requests before
any live side effect. An authenticated exact internal write ask may proceed only
through the owning typed tool with provider flags, unique target identity,
approval reference, and read-back evidence; the matrix does not itself execute
provider mutations.
