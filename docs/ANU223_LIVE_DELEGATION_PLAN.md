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
| A | Create one marked Airtable expense in the configured test-safe table, read it, update the same record from a natural follow-up, verify it, and remove only that marked record. | 4 | $0.10 | One marked record; no schema change; Airtable owner override fixed at four turns |
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

## 2026-07-11 Stage A checkpoint

The natural Chief request reached `airtable_context_agent` through Orchestrator
preflight and completed one marked `Business Expenses` lifecycle: schema check,
create/read-back, same-record update/read-back, marker-gated delete, and confirmed
absence. The authenticated operator scope was reused without a second approval.
No Slack, Gmail, Calendar, attachment, schema, or unrelated provider write ran.

This is partial evidence rather than a Stage A pass because two control-plane
acceptance checks failed:

- The declared four-request ceiling was calculated from the named Chief route,
  while execution delegated to Airtable's six-turn default. The run used six
  requests at an estimated $0.0492. The CLI estimator now resolves deterministic
  delegation before enforcing the ceiling, so this mismatch blocks before any
  future model call.
- The first rendered receipt exposed provider record/base IDs. Airtable IDs now
  remain in internal structured state while the human summary shows only the
  table, marked lifecycle result, verification, and cleanup evidence.

The run also contained one safely blocked pre-write record read; it was not a
mutation and no longer appears as a blocked-write side effect. The rerun command
must set `KEYSTONE_AIRTABLE_CONTEXT_AGENT_SDK_MAX_TURNS=4`; both preflight
estimation and delegated execution resolve that same owner-specific limit. If
the lifecycle cannot complete in four requests, stop and improve the owner path
offline rather than silently increasing the stage budget. Do not rerun Stage A
until a new four-request budget is approved. Stages B and C remain unrun.
