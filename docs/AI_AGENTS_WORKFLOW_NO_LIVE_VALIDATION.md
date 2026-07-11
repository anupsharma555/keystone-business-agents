# AI Agents Workflow No-Live Validation

Updated: 2026-07-10

## Current Boundary

This is the current pre-live validation surface for KBA basic agent functions
and backend-selected WorkItem/LangGraph execution used by
`ai-agents-workflow`.

The legacy 102-case Promptfoo suite was introduced on 2026-06-13 before the
current graph execution model. It is intentionally excluded from this gate.
That suite is reserved for future migration and reactivation after the updated
agents and graph paths are validated. Its current failures must not drive
prompt-specific fixes or block basic-function validation.

Run the current gate with:

```bash
.venv/bin/python scripts/run_ai_agents_workflow_no_live_gate.py
```

The gate runs focused agent/WorkItem/graph tests, the seven-scenario offline
LangGraph quality comparison, the 36-case no-live Slack expansion gate, and
ANU-60's no-live preflight. It never enables live SDK, search, connectors,
Slack posting, Gmail drafts, sends, or external writes.

The legacy `#evals` dashboard/runtime check is intentionally optional. Add
`--include-evals-readiness` only when reactivating that evaluation surface;
dashboard availability does not gate current agent and graph validation.

## ANU-174 Recorded Offline Evidence

| Smoke | Current no-live proof | Route/output proof | Side-effect proof | Status |
|---|---|---|---|---|
| SMK-01 | `test_backend_selected_manager_loop_uses_graph_for_research_opportunity_outreach_checkpoint` and seven-scenario comparison | Research-first graph reaches Outreach checkpoint | No send/post/write; approval checkpoint retained | Pass |
| SMK-02 | Chief selected-thread planner/orchestrator tests plus 36-case expansion gate | Chief remains manager for selected-thread synthesis | No Slack post outside the response | Pass |
| SMK-04 | `test_source_provided_comparison_uses_supplied_evidence_before_generic_comparison` plus multi-target tests | Business Research preserves supplied evidence and requested comparison fields | Read-only fixture path | Pass |
| SMK-08 | `test_basic_smoke_outreach_revision_uses_approved_inline_facts_without_side_effects` | Outreach draft artifact records approved context, source IDs, and revision request | No Gmail draft, send, post, or external write | Pass |
| SMK-11 | `test_basic_smoke_gmail_to_calendar_without_context_returns_exact_safe_blocker` | Gmail Triage returns `gmail_context_required` without selected email text | No draft, schedule, calendar change, or send | Pass at blocker boundary |
| SMK-15 | Google Workspace artifact-plan graph tests and context-agent write-boundary tests | Pasted-note artifact planning stays Workspace-owned and approval-gated | No Drive/Docs write | Pass at artifact-plan boundary |
| SMK-17 | `test_langgraph_stages_zotero_context_before_business_research` | Zotero context is staged before Business Research | Read-only context; no Zotero mutation | Pass |
| SMK-20 | Chief/local-KNI evidence tests | Returns evidence paths when local evidence resolves, otherwise an exact local-source blocker | Local-only; no hosted or external provider | Pass at local evidence/blocker boundary |

Stable proof IDs are pytest node IDs or the named gate command. Runtime
WorkItem IDs remain per-run evidence and should be captured only when a manual
or live run occurs.

## Current Acceptance Rules

- The seven graph scenarios must report `Ready: yes` with no regressions.
- The no-live Slack expansion gate must pass all 36 cases and retain route
  coverage across operating and context agents.
- ANU-60 preflight must retain its KBA and sibling fixture evidence.
- Strict `#evals` readiness warnings remain blockers only when that deferred
  evaluation surface is reactivated.
- No API budget is needed for this phase.

## ANU-64 Current-Workflow Acceptance Scorecard

This scorecard covers the current operating routes. It is not a `#evals`
scorecard and does not use model grading.

| Route | No-live status | Failure category proved | Tool tier | Side-effect boundary | Next live proof |
|---|---|---|---|---|---|
| Orchestrator | Pass | wrong-lane/meta-routing regression | planning and typed handoff | cannot bypass Python gates | One bounded model-only cross-agent run after approval |
| Chief of Staff | Pass | manager ownership/advisory-routing regression | read/plan advisory plus durable handoff | no post, schedule, send, or provider write | Read-only selected Slack thread |
| Gmail Triage | Pass live connector probe | missing thread identity and calendar context | Gmail read plus approved provider draft | one marked synthetic draft passed create/read, same-ID update/read, exact-ID delete, and absence verification; no send and zero OpenAI requests | Natural-language draft create/read/update/delete cycle through Orchestrator and Gmail ownership |
| Business Research | Pass | source-provided evidence diverted to generic comparison/multi-target | fixture/source read; live search off | read-only; visible source basis | One current source-backed company comparison |
| Opportunity Scout | Pass | weak/no-source opportunity handling | fixture/source ranking | no CRM or Airtable write | One source-provided ranking, then optional live search |
| Outreach Composer | Pass | unapproved prompt echo and revision-context handling | approved context plus local draft artifact | no Gmail draft, send, post, or external save | One model-only approved-context revision |
| Airtable Context | Pass live read; reversible write blocked | identity and write-scope blocker | live schema/read context | no test record created because KBA has no delete/cleanup tool | Add an approval-gated test-record cleanup contract before create/remove proof |
| Google Workspace Context | Pass live reversible marked-Sheet lifecycle | context ownership, exact row identity, and approval gates | Drive metadata plus Sheet create/read/append/update/delete-row/trash read-backs | provider lifecycle proven; natural-language LLM tool selection pending | Run the approved model-selection proof with linked Workspace config and cost monitoring |
| Zotero Context | Pass live read and reversible marked-note lifecycle | context handoff, item identity, and natural note-operation routing | live API metadata plus create/read/update/read/delete/absence verification | provider lifecycle proven; natural-language LLM tool selection pending | Run the approved model-selection proof with request/cost monitoring |
| RSS Context | Pass | route/render/no-refresh contract | local history/read context | no feed refresh or Slack post | Defer until live Slack rendering proof |
| Preprints Context | Pass | preliminary-evidence and graph staging | local history/read context | no refresh, publication, or validated-clinical claim | One current read-only preprint probe after Slack rendering |

The connector probes above were explicitly approved and used no model API.
Provider content and account identifiers are intentionally omitted from this
committed scorecard. Model-backed runs remain a separate budgeted phase.

## Initial Operational Connector Evidence

- Gmail: authenticated account matched the configured agent mailbox; one
  bounded message summary was read; one marked synthetic provider draft passed
  create/read, same-ID update/read, exact-ID delete, and verified absence with
  `sent=false`.
- Airtable: the configured finance/tax base returned five allowed tables and
  one bounded record; one marked test row passed create/read/update/read/delete/
  absence verification.
- Zotero: the configured library returned one bounded item; one marked
  standalone note passed versioned create/read/update/read/delete/absence
  verification.
- OpenAI/model requests: zero.

## Operational Agent Acceptance Contract

A connector primitive is necessary but does not prove that an agent is
operational. Every live scenario must record two separate results:

1. **Connector lifecycle:** bounded read, clearly labeled disposable create,
   read-back, modification, modification verification, cleanup or explicit
   manual cleanup, and confirmation of no unintended send/post/publish.
2. **Natural-language agent lifecycle:** a normal operator ask enters through
   Orchestrator, the owning agent interprets target/action/scope, ambiguity and
   approval are validated, the typed tool is selected, and post-execution
   review confirms that the result satisfied the original ask.

Direct adapter calls are diagnostics only. They cannot satisfy the second
result. Airtable, Google Workspace, and Zotero agents may execute their own
explicitly allowed, approval-gated test writes when they are the selected
agent; their live natural-language proof must show exact target resolution,
typed tool selection, read-back verification, and cleanup. RSS and Preprints
remain read-only and must change only a downstream owner-created artifact.

| Agent/route | Read proof | Create/modify proof | Natural-language/LLM proof |
|---|---|---|---|
| Orchestrator | raw ask plus bounded context | create/update WorkItem plan and decision trace | route, scope, approval, and completion review match the operator ask |
| Chief of Staff | multi-source advisory context | create/revise internal action plan or owned artifact | manages specialists without impersonating their write ownership |
| Gmail Triage | selected thread/message | create, read, and update one draft; never send | interprets recipient, subject, body change, and approval from normal asks |
| Business Research | supplied or retrieved sources | create/revise a sourced brief through the artifact owner | preserves requested format, caveats, and evidence under follow-up revision |
| Opportunity Scout | source-backed candidates | create/update one disposable opportunity record through Airtable owner | preserves hard filters and validates exact record identity |
| Outreach Composer | approved context plus existing draft | create/revise draft-only copy through Gmail or local artifact owner | preserves approved facts and applies requested revision without sending |
| Airtable Context | schema plus bounded records | selected agent creates/updates one marked disposable record, then verifies cleanup | schema/identity evidence changes the write plan and exact approved tool arguments |
| Google Workspace Context | bounded Drive/Doc/Sheet context | selected agent creates/edits one marked disposable Doc or Sheet artifact | exact file/folder scope and requested edit are interpreted and verified |
| Zotero Context | selected collection/item/note | selected agent updates/removes one marked disposable note through guarded tools | item identity and version are validated before mutation |
| RSS/Preprints Context | bounded current entries | downstream owner creates/revises a sourced digest | source status and preliminary-evidence caveats survive synthesis and revision |

## Framework Execution Tiers

Run the current framework-only tier gate with:

```bash
npm run test:agent-execution-tiers
```

- **Simple:** one owner, bounded context, typed output, and deterministic safety
  behavior. Includes natural-prompt routing and all specialist SDK agents with
  injected fake models.
- **Intermediate:** Orchestrator/agent interpretation plus one typed tool or
  approved artifact revision. Includes fixture tool selection, approval
  rejection, Outreach revision, and Chief advisory ownership.
- **Advanced:** backend-selected LangGraph execution, multiple typed handoffs,
  checkpoint state, and manager review. Includes research-to-opportunity-to-
  outreach, preprints/Zotero-to-research, and approval resume.

The gate uses no live connector, OpenAI, search, Slack, or Promptfoo execution.
Passing it proves framework mechanics and bounded interpretation with
fixture/fake model boundaries. It does not replace the later natural-language
live-model and provider lifecycle proof.

## Future Promptfoo Reactivation

Before running the 102-case suite again:

1. Inventory each case as current, update-required, or retired.
2. Rewrite pre-graph route/status expectations around current Orchestrator,
   WorkItem, backend graph selection, advisory-tool, and renderer contracts.
3. Validate the migrated cases no-live against `ai-agents-workflow` behavior.
4. Only then reconnect the suite to `#evals` and human scoring.
5. Keep API-backed model grading and live connectors behind a separate budget
   and approval gate.
