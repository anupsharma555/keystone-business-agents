# Context Agent Contracts

This page captures the ANU-198 through ANU-202 contract slice for Airtable,
Google Workspace, Zotero, RSS, and Preprints context agents. The canonical
machine-readable source is `src/keystone_agents/context_agent_contracts.py`.

These agents are context and evidence nodes first. They may become graph stages
when WorkItem state needs resumable checkpoints, but they should not become
separate subgraphs until validation proves the extra state boundary is useful.

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

## Validation Requirements

Current zero-model SDK evidence: Airtable Context selects and consumes its
typed base-schema tool from a finance-tracker ask; Google Workspace Context
selects and consumes scoped Drive search from a file-finding ask; Zotero Context
selects and consumes typed API metadata from a literature ask. Their separate
fake-model runs also select guarded Airtable record, Workspace Sheet, and Zotero
test-note write previews using explicit markers and approval references without
provider mutation. Their separate live provider lifecycle harnesses prove
disposable writes and cleanup. The remaining boundary is live-model
interpretation that joins these layers in one natural-language run.

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
