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
| ANU-198 | `airtable_context_agent` | Reads configured bases/tables/records; approved create/update only with exact record identity, field mapping, approval reference, and live flag. No deletes, schema changes, silent bulk overwrites, or hidden nested writes. | Opportunity Scout or Business Research to Airtable Context to Chief review. Subgraph later only for approval-gated record create/update loops. | Base/table/field refs, candidate record ids, record summaries, missing fields, dedupe/status context, write-plan recommendations, identity blockers. |
| ANU-199 | `google_workspace_context_agent` | Reads scoped Drive/Docs/Sheets context; approved internal artifact writes only with exact destination and approval. No broad delete/share or sensitive-data publication. | Business Research or Opportunity Scout to Workspace Context to Chief review. Subgraph later for approved artifact update loops. | Folder/file/doc/sheet/tab refs, existing document/table facts, stale-fact notes, artifact gaps, source-backed update plans. |
| ANU-200 | `zotero_context_agent` | Reads local/API Zotero and approved article evidence. Zotero mutation stays behind the guarded importer path with dry-run proof; Workspace artifacts remain approval-gated. | RSS/Preprints to Zotero Context to Business Research, or Zotero to Opportunity Scout. Subgraph later only for digest/importer planning checkpoints. | Collection/item/source ids, article metadata, abstract/full-text basis, evidence packets, source maturity, opportunity implications, unsupported-claim caveats. |
| ANU-201 | `rss_context_agent` | Strict read-only historical RSS/#announcements context. It may create internal insight packets, but must not mutate feeds or post digests. | RSS Context to Business Research, Opportunity Scout, or RSS to Preprints/Zotero staging. | Feed item ids, titles, URLs, dates, source notes, theme clusters, stale/current caveats, opportunity hooks, monitoring queries. |
| ANU-202 | `preprints_context_agent` | Strict read-only preprint/#knowledge-hub context. It may create internal article/evidence packets, but must not mutate source records or overstate preliminary findings. | Preprints Context to Business Research, Opportunity Scout, or Zotero staging. | Article titles, source URLs/DOIs, dates, evidence status, abstract/full-text basis, preliminary-claim caveats, opportunity implications. |

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
