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

## 2026-07-11 Stage A capped rerun 1

The first four-request rerun correctly honored the Airtable owner override and
stopped at the declared ceiling. It used four `gpt-5.4-mini` requests, 137,671
input tokens including 65,536 cached tokens, 1,501 output tokens, and a
maintained estimate of `$0.06577095`. No Airtable record was created, no other
provider mutation ran, and no cleanup artifact was needed.

The run did not pass. After reading the live schema, the model attempted an
invalid `Categories=Other` value and exhausted the remaining turns before a
valid create. The fix is structural rather than prompt-only: Airtable Context
now exposes one marker-restricted `airtable_test_record_lifecycle` tool that
uses only the schema-valid `Item` and `Description` fields, owns provider
identity and both read-backs in Python, updates the same record, and always
attempts marker-gated deletion in `finally`. Direct exact lifecycle asks receive
a request-hashed process-local operator approval reference, which is removed
after execution so the operator is not asked to approve the same scope twice.
Focused agent, tool, receipt, cleanup-failure, registry, CLI, and manager gates
pass offline.

## 2026-07-11 Stage A capped rerun 2 — PASS

The same natural manager ask selected the new bounded lifecycle tool and passed
in two `gpt-5.4-mini` requests. Usage was 61,132 input tokens including 30,208
cached tokens and 796 output tokens; the maintained estimate was `$0.0290406`.
One marked Business Expenses record was created and read back, its Description
was updated on the same internal identity and read back, then marker-gated
cleanup deleted only that record and confirmed absence. No blocker, retry,
duplicate, orphan, Slack/Gmail/Calendar action, attachment, schema change, or
unrelated provider write occurred.

The original debug payload retained the provider ID in structured internal
fields. Deterministic public-payload sanitization now removes that identity from
the public output and tool receipt while retaining the private execution record.
Offline re-rendering of saved run `2869` proves the internal ID is absent from
the public receipt and human summary. Stage A is now a joined manager/provider
PASS and should not be rerun.

## 2026-07-11 Stage C capped run 1

The first Zotero manager-join attempt correctly exposed two offline defects
before any provider mutation. The natural standalone-note ask initially stayed
with Chief because the generic internal-write classifier did not include
`note`; adding that object class now routes complete Zotero note lifecycles to
`zotero_context_agent`. The exact six-request preflight then passed.

The live run used four `gpt-5.4-mini` requests, 127,918 input tokens including
63,488 cached tokens, 988 output tokens, and an estimated `$0.0575301`. The
agent called create twice with `live=false`, so both were previews; no provider
note/key existed, no delete ran, and no orphan or unrelated write occurred.

The structural repair mirrors the successful Airtable pattern: one bounded
`zotero_test_note_lifecycle` tool now owns marked standalone-note create/read-
back, same-note version-aware update/read-back, marker-gated deletion, and
absence verification with finally-based cleanup. The direct execution context
explicitly requires `live=true`, reuses a request-scoped operator approval
reference, and deterministic public-payload sanitization keeps the provider key
internal. Focused routing, approval, lifecycle, fake-model tool-selection,
receipt, cleanup, registry, and sanitizer tests pass. Stage C remains partial
until a later capped live run selects the new tool and passes.

## 2026-07-11 Stage C capped run 2 — PASS

The same natural standalone-note ask selected the bounded lifecycle tool and
passed in two `gpt-5.4-mini` requests. Usage was 64,300 input tokens including
31,744 cached tokens and 886 output tokens; the maintained estimate was
`$0.0307848`. One marked note was created and read back, revised on the same
versioned internal identity and read back, then marker-gated deletion confirmed
absence. No retry, blocker, duplicate, orphan, Workspace write, import, Slack/
Gmail/Calendar action, or unrelated provider mutation occurred.

The provider key remains internal. Public output clears item-key fields,
replaces any nested identity mention with `the marked test note`, and omits the
key from the tool receipt. Zotero is now a joined manager/provider PASS and
should not be rerun. Stage B Workspace remains optional for broader coverage,
not required for the current one-additional-provider acceptance row.
