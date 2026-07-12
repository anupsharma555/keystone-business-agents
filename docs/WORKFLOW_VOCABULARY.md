# KBA Workflow Vocabulary (ANU-216)

Executable contract: `src/keystone_agents/workflow_vocabulary.py`

This is a small interoperability vocabulary, not an ontology. It gives tools,
WorkItems, artifacts, memory, and context packs canonical labels that survive
Gmail, Slack, Workspace, Airtable, Zotero, local files, and SQLite handoffs.

Every canonical label has the shape `kba:<dimension>:<value>` and belongs to one
of five dimensions:

- `object`: work item, source, artifact, message, record, document,
  presentation slide, or memory;
- `workflow`: discovery, research, triage, planning, drafting, approval review,
  execution, verification, cleanup, or monitoring;
- `safety`: read-only, draft-only, internal write, external side effect,
  approval required, sensitive, no-PHI, no-send, or no-post;
- `evidence`: user-provided, provider-verified, first-party, extracted,
  synthesized, inferred, stale, missing, or contradicted;
- `storage`: transient, WorkItem, SQLite, provider, artifact file, or hosted
  store.

Deterministic compatibility gates reject external-side-effect tags without an
approval tag, sensitive context tagged for hosted storage, read-only objects
combined with writes, and presentation-slide evidence without file or SQLite
provenance. A short alias map accepts common existing labels such as `no-send`,
`review-only`, and `provider-readback`; unknown labels fail closed.

Use `attach_controlled_tags()` to add the versioned `controlled_tags` field to
metadata. Do not use free-form tags to grant permissions, bypass approvals, or
replace typed source, identity, or handoff fields.
