# ANU-223 Live Manager Delegation Plan

Run this plan only after the no-live delegation readiness gate passes and the
operator approves the exact request and cost ceiling. Execute one provider
family at a time. Stop after the first retry, missing receipt, unexpected write,
or shared routing/identity defect.

## Reused evidence

- Calendar and Gmail already have joined manager-entry/provider proof. Do not
  rerun them.
- Airtable, Workspace, and Zotero provider mechanics, read-back, identity reuse,
  and cleanup already pass. The live tests below validate only the missing
  manager-to-owner join.
- Airtable structural base/table creation is out of scope. Do not broaden the
  finance token.

## Proposed serial budget

| Stage | Natural manager ask | Maximum OpenAI requests | Maximum estimated cost | Provider scope |
|---|---|---:|---:|---|
| A | Create one marked Airtable expense in the configured test-safe table, read it, update the same record from a natural follow-up, verify it, and remove only that marked record. | 4 | $0.10 | One marked record; no schema change |
| B | Create one marked Sheet in the approved KNIOps location, add and revise one marked row without an ID in the follow-up, verify it, and trash the test Sheet. | 8 | $0.12 | One marked Sheet; no share |
| C | Create one marked Zotero note for the selected test item, revise the same note without its key, verify it, and remove the note. | 6 | $0.10 | One marked note; no library-wide mutation |

Combined hard ceiling: 18 OpenAI requests and $0.32 estimated cost. Approval may
cover one stage or the full sequence. A stage uses fewer requests when the
deterministic manager/owner path can safely skip model planning.

## Evidence required per stage

1. Raw natural-request hash and selected manager/owner route.
2. Exact scoped approval reference with no duplicate approval loop.
3. Provider-safe identity retained internally across follow-ups.
4. Create/read-back, same-object modify/read-back, cleanup, and absence/trash
   verification.
5. One sanitized user-facing receipt with no secret, raw private content, or
   unnecessary provider metadata.
6. Request count, token/cost evidence, retry count, trace/run IDs, and explicit
   no-unintended-write result.

## Stop conditions

- Stop before provider mutation on zero or ambiguous target matches.
- Stop after the first unexpected retry, duplicate object, repeated approval,
  missing read-back, orphan, or unrelated side effect.
- Fix the shared defect offline and rerun the focused regression before asking
  to spend on the same stage again.
- Do not advance to the next provider merely because cleanup succeeded; inspect
  answer usefulness, manager friction, identity continuity, and receipt quality.
