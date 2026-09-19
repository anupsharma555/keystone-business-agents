# Context Agent Contracts

This page captures the ANU-198 through ANU-202 contract slice for Airtable,
Google Workspace, Zotero, RSS, and Preprints context agents. The canonical
machine-readable source is `src/keystone_agents/context_agent_contracts.py`.

These agents are context and evidence nodes first. They may become graph stages
when WorkItem state needs resumable checkpoints, but they should not become
separate subgraphs until validation proves the extra state boundary is useful.

Current execution is not one uniform context-agent lane. Airtable, Workspace,
and Zotero have direct live CLI paths outside `WorkflowRunner`; RSS and
Preprints use the signal runtime; Chief calls nested context specialists through
validated child wrappers over agent tools. The nested wrapper binds selections
to actual provider candidates, permits one evidence-only repair without rereads,
fails closed without execution context, returns blocked envelopes rather than
unvalidated prose, and rejects mutation tools. It remains a distinct call shape
from the shared direct wrapper. Its validated child decision record is retained
through SDK tool custom data and is not exposed in the model-visible or public
result envelope. The unified trace ingests that custom data; a live
parent-supplied `run_config` is labeled `live_sdk`; and nested live reads enforce
`live=true` on isolated tool copies while mutation tools remain absent.

The main integration tree now gives RSS/Preprints one evidence-preserving,
tool-free semantic repair over the first candidate universe without repeating
the history provider read. Airtable/Workspace/Zotero now have
provider-authoritative candidate binding, one semantic repair, cumulative
telemetry, mutation-safe evidence handling, and one bounded missing-required-tool
correction before semantic validation/repair. Completed read evidence may be
replayed with completed tools disabled; mutations are never repeated, and
unavailable replay or a failed corrected postcondition fails closed. They
remain outside first-class `WorkflowRunner` specialist dispatch.

## Boundary Summary

| Issue | Agent | Current boundary | Graph candidate edge | Evidence passed downstream |
| --- | --- | --- | --- | --- |
| ANU-198 | `airtable_context_agent` | Reads configured bases/tables/records; approved create/update only with exact record identity, field mapping, approval reference, and live flag. Cleanup deletion is limited to an exact provider record whose fields contain `KBA_TEST_RECORD`, with a separate approval/live gate and absence verification. No ordinary deletes, schema changes, silent bulk overwrites, or hidden nested writes. | Opportunity Scout or Business Research to Airtable Context to Chief review. Subgraph later only for approval-gated record create/update/test-cleanup loops. | Base/table/field refs, candidate record ids, record summaries, missing fields, dedupe/status context, write-plan recommendations, identity blockers. |
| ANU-199 | `google_workspace_context_agent` | Reads scoped Drive/Docs/Sheets context; approved internal artifact writes only with exact destination and approval. Disposable Sheet lifecycles require marked titles/rows, stable row keys, read-backs, a test-only live gate, and verified trash cleanup. No broad delete/share or sensitive-data publication. | Business Research or Opportunity Scout to Workspace Context to Chief review. Subgraph later for approved artifact update loops. | Folder/file/doc/sheet/tab refs, existing document/table facts, stale-fact notes, artifact gaps, source-backed update plans. |
| ANU-200 | `zotero_context_agent` | Reads local/API Zotero and approved article evidence. General Zotero mutation stays behind the guarded importer path. The native exception is one versioned disposable note containing `KBA_TEST_NOTE`, with direct-agent approval, live gate, read-back verification, and cleanup. Workspace artifacts remain approval-gated. | RSS/Preprints to Zotero Context to Business Research, or Zotero to Opportunity Scout. Subgraph later only for digest/importer/test-note checkpoints. | Collection/item/source ids, article metadata, abstract/full-text basis, evidence packets, source maturity, opportunity implications, unsupported-claim caveats. |
| ANU-201 | `rss_context_agent` | Strict read-only historical RSS/#announcements context. It prefers KBA local history and may use the structured Slack `#announcements` fallback only with `live=true` plus the dedicated process gate. It may create internal insight packets, but must not mutate feeds or post/modify Slack messages. | RSS Context to Business Research, Opportunity Scout, or RSS to Preprints/Zotero staging. | Feed item ids, titles, direct URLs, dates, source notes, Slack-derived stable ids, theme clusters, stale/current caveats, opportunity hooks, monitoring queries. |
| ANU-202 | `preprints_context_agent` | Strict read-only preprint/#knowledge-hub context. It prefers KBA history and may read the allowlisted linked discovery store in SQLite read-only mode when local rows are empty. It may create internal article/evidence packets, but must not mutate source records or overstate preliminary findings. | Preprints Context to Business Research, Opportunity Scout, or Zotero staging. | Article titles, source URLs/DOIs, dates, discovery-candidate identity, evidence status, abstract/full-text basis, preliminary-claim caveats, opportunity implications. |

## Context-Pack Contracts

All five agents must preserve source refs and uncertainty through
`ResearchContextPack`. Airtable, Workspace, RSS, Preprints, and Zotero can also
feed `OpportunityContextPack` when evidence informs opportunity scoring,
dedupe, target selection, or tracking gaps. Zotero and Preprints may feed
`OutreachContextPack` only as approved, caveated evidence for draft rationale;
they do not grant permission to use preliminary or unsupported claims.

Required source fields and uncertainty fields are defined per agent in the
typed catalog. Downstream Business Research and Opportunity Scout should receive
bounded evidence packets, not loose prose.

## Model-Visible Selection And Cross-Agent Handoffs

Context acquisition and semantic selection are separate responsibilities. A
workflow may perform a bounded provider read before a downstream model turn, or
the context agent may call the read tool itself. In either case, the candidate
identities, bounded evidence, source limitations, and provider receipt needed
for the decision must be serialized into the deciding agent's input or returned
through a tool in the same model loop. Python validates identity, permission,
ceilings, and receipt truth; it does not choose the substantive winner for the
agent.

Cross-provider joins use the same rule. For example, a Calendar-to-Gmail reply
workflow requires a manager-owned Calendar selection bound to the exact events
returned by the Calendar read, an explicit typed handoff to Gmail Triage, and a
bounded read-only context packet. Gmail remains the owner of message/thread
selection and reply judgment. Calendar context is evidence only and cannot
grant Gmail mutation or send authority.

## Validation Requirements

The current offline contract matrix covers tool selection/consumption,
agent-owned candidate decisions, identity-preserving continuation, and guarded
write previews without provider mutation. Separate provider lifecycle harnesses
cover marked disposable writes and cleanup. These are distinct proof layers:
fixture or fake-model coverage does not by itself establish live model,
provider, Slack, or cross-agent acceptance.

Local SDK session regressions also prove identity-preserving follow-ups: the
Airtable agent reuses the existing record ID, Workspace reuses the selected
Sheet ID and stable row key, and Zotero reuses the existing note key. Each
second turn selects an update preview and explicitly avoids duplicate creation.

The next implementation milestone is fixture-backed no-live coverage for:

- Airtable base-specific reads, approved create/update plans, ambiguous record
  blockers, and delete/schema-change blockers.
- Workspace document synthesis, opportunity/contact artifact plans, sheet-row
  updates, identity blockers, and broad delete/share blockers.
- Zotero key-article reads, collection comparison, importer plans, ambiguous
  collection/item mutation blockers, and provenance preservation.
- RSS detailed insight, theme clustering, opportunity signal extraction,
  stale/current caveats, and blocked feed mutation.
- Preprints article summaries, paper-set synthesis, opportunity implications,
  preliminary-claim caveats, and blocked source mutation.

Focused local gate:

```bash
.venv/bin/python -m pytest tests/test_context_agent_contracts.py
```
