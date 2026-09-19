# Agent Semantic-Depth Audit

Audit date: 2026-08-03

Audited code snapshot: the current dirty integration checkout at
`<repo>`, based on
`c9cb5a4b48907f0d431169f614b6d34ff6b86a13` with 223 modified or untracked
paths at the revalidation checkpoint. The audit document itself is maintained in the
delegated writable worktree for integration. Conclusions do not automatically
apply to `origin/main`, a clean clone, or a deployed worker.

No live model, provider, Gmail, Calendar, Slack, Airtable, Workspace, Zotero,
RSS, or preprint call was made for this audit.

## Verdict

The current integration tree contains genuine model-owned specialist judgment,
not merely SDK objects wrapped around Python summaries. Six registered agents
qualify as `true specialist` across their ordinary live/default semantic routes:
Business Research Analyst, Opportunity Scout, Outreach Composer, RSS Context,
Preprints Context, and Orchestrator. Five are `bounded specialist`: Gmail Triage,
Airtable Context, Google Workspace Context, Zotero Context, and Chief of Staff.
Their bounded status reflects important route-parity, alternate-path, or
pre-admission limitations, not an absence of model-owned reasoning on their
strong paths.

Confidence is assessed over ordinary live semantic routes and important live
alternates. An explicitly labeled no-live fixture, deterministic health/status
read, or exact typed operation does not by itself downgrade a specialist. A live
semantic alternate that can bypass the stronger decision contract does.

The safe no-live defaults remain fixture previews, deterministic routing, or
typed provider-operation facades. They are useful and intentionally safe, but
they are not evidence of model-owned reasoning. `DirectAgentResponse` is an
explicit synthesis-only lane and is now admitted only for positively evidenced,
provider-free transformations; it no longer masks provider-dependent specialist
work.

| Registered agent | Ordinary live/default specialist route | Important alternate route | Overall confidence |
|---|---|---|---|
| Gmail Triage | model-owned query/read/compare/select loop in direct and live-SDK WorkItem execution | legacy provider-only/non-SDK acquisition remains deterministic; exact verified continuation is a bounded read-and-decide path | bounded specialist |
| Business Research Analyst | direct and WorkItem model-owned search/evidence loop | supplied verified bundle supports bounded tool-free judgment | true specialist |
| Opportunity Scout | direct and WorkItem model-owned search/score/compare loop | supplied verified candidates support bounded tool-free judgment | true specialist |
| Outreach Composer | verified-context, tool-free claim/CTA/draft judgment | positive-evidence provider-free transformation may use separate `DirectAgentResponse` | true specialist |
| Airtable Context | direct CLI and Chief child model-owned schema/read/compare loop | exact marked-test lifecycle is a deterministic operation; no first-class WorkflowRunner route | bounded specialist |
| Google Workspace Context | direct CLI and Chief child model-owned schema/read/compare loop | exact marked-test lifecycle is a deterministic operation; no first-class WorkflowRunner route | bounded specialist |
| Zotero Context | direct CLI and Chief child model-owned collection/search/read/compare loop | no first-class WorkflowRunner route | bounded specialist |
| RSS Context | signal WorkItem and every selected direct/Chief child history/compare/select loop | no-live preview remains fixture-only | true specialist |
| Preprints Context | signal WorkItem and every selected direct/Chief child history/compare/select loop | no-live preview remains fixture-only | true specialist |
| Orchestrator | raw-request preflight, route/workflow/context decision and repair | deterministic health/status routes and safety gates | true specialist |
| Chief of Staff | model-owned tool choice, synthesis, delegation, and validated child decisions | plan-based tool admission and several deterministic fast paths | bounded specialist |

## Objective detection standard

The unit of proof is an executed entry path, not an SDK `Agent`, prompt, registry
tool list, agent name, or polished output.

### 1. Model-called tool use

A path qualifies when observable records show all of the following:

1. The model received the raw current request and every material limitation.
2. The trace records the tools actually admitted to that model turn.
3. An SDK tool-call item and tool output occurred in the same logical run.
4. The provider result became visible to a later model turn before selection.
5. Selected identities bind to the raw provider result, and the bounded candidate
   universe was neither silently truncated nor reconstructed from the final answer.
6. The output records the decision, alternatives assessed or excluded, evidence
   used, limitations, validator result, and any repair action without recording
   private chain-of-thought.

An attached but uncalled tool fails this test. A Python-prefetched result does not
become model-called merely because the same function is also registered as a tool.

### 2. Workflow acquisition followed by model judgment

Zero model tool calls can still be valid when Python obtains bounded, verified
evidence and the model owns the semantic decision. The model input must include the
raw request, all bounded candidate identities and material evidence, receipts or
source references, and limitations. The model must compare or exclude alternatives
and emit the specialist decision. Python may normalize and validate; it must reject
or repair an invalid decision without substituting its preferred alternative.

Useful negative controls include reversing candidate order, adding a plausible
decoy, changing the objective, weakening one source, and removing a required fact.
The model decision or stated uncertainty should change for a domain-relevant reason.

### 3. Tool-free specialist reasoning

A tool-free stage is legitimate when no provider acquisition is required, or when a
repair replays already verified evidence. It must still expose a bounded specialist
choice: approved-claim selection, outreach strategy, signal relevance, route and
delegation, ambiguity resolution, or a domain recommendation. Specialist-specific
structured fields and validation must be required; fluent prose alone is synthesis.

### 4. Generic summarization

A path is synthesis-only when it receives a Python-chosen answer or final record and
only rewrites it, exposes no meaningful alternatives or exclusions, uses only a
generic answer schema, or can omit the semantic decision and still pass. The bounded
`DirectAgentResponse.answer` lane is useful, but it is not evidence for any registered
specialist's decision contract.

### 5. Deterministic semantic facade

A path is a deterministic facade when model request count is zero while Python
selects, ranks, routes, or composes the substantive answer. Fixture/no-live paths and
exact typed operation helpers must be labeled as such. A default decision field,
agent label, or tool catalog cannot convert them into agent judgment.

### 6. Repair and failure integrity

A recoverable semantic failure qualifies as agent repair only when the trace shows
the original invalid decision, validator feedback, a single bounded corrective turn,
the same verified evidence fingerprint, provider tools disabled where evidence has
already been acquired, and a validated replacement decision. Repair exhaustion must
fail closed. Python may not reread a stable provider merely to give the model the same
evidence again, and may not synthesize the missing decision itself.

## Evidence matrix

### Gmail Triage — bounded specialist

- **Paths:** direct named live execution and live-SDK WorkItem execution use the
  registered Gmail query/read/compare/select contract. Exact verified continuation
  skips mailbox search and reads only the already identified thread. A legacy
  provider-only/non-SDK path remains deterministic and is not counted as model-owned
  Gmail judgment.
- **Model-visible input:** raw request, query constraints, bounded message/thread
  candidates, exact thread reads, prior-thread context, Calendar context when supplied,
  and limitations. Verified continuation preserves the same evidence on repair.
- **Tools:** model-callable Gmail schema/query/read and allowed action-planning tools
  are request-scoped in direct and live-SDK WorkItem runs. Exact verified continuation
  context is explicitly preacquired rather than misreported as a new model tool call.
- **Model-owned decisions:** query refinement, current versus stale thread, category,
  priority, reply need, candidate selection/exclusion, risk, recommended action, and
  downstream specialist recommendation.
- **Python boundary:** call ceilings, account/thread identity, read receipts, exact
  continuation scope, permissions, draft/send gates, schema validation, and no-send.
- **Comparison/repair:** the validator uses the actual Gmail candidate universe and
  requires candidate coverage. One invalid decision receives a tool-free replay of the
  same evidence; repair cannot requery, and exhaustion does not trigger Python choice.
- **Delegation/output:** `EmailTriageResult` records triage, selected identity, risks,
  reply recommendation/draft, limitations, next agent, and the decision record.
  Delegation remains advisory until the orchestrator consumes it.
- **Trace evidence:** fake-model tests cover query/read order, full-candidate
  assessment, fabricated selection rejection, exact continuation, one no-requery
  repair, and exhausted repair. Production-path trace hydration is implemented, but
  no live Gmail trace was captured for this audit.
- **Why bounded:** direct, live-SDK WorkItem, and exact continuation paths preserve the
  specialist decision contract. The bounded rating remains because the legacy
  provider-only/non-SDK alternate still performs deterministic acquisition/selection.

### Business Research Analyst — true specialist

- **Paths:** direct live and live-SDK WorkItem research both run the registered agent
  over an agent-owned web search/evidence loop. A supplied verified source bundle is a
  legitimate tool-free comparison/synthesis alternate.
- **Model-visible input:** raw request, WorkItem context, raw `search_web` outputs,
  stable provider candidate IDs, separate citation identities, extracted evidence,
  source limitations, and the complete bounded candidate packet.
- **Tools:** the model calls admitted web search and may call bounded extraction,
  claim, ranking, or research helpers. Provider choice, budgets, URL safety, and
  normalization remain workflow-owned and are separately traced.
- **Model-owned decisions:** query strategy, evidence sufficiency, source relevance
  and quality, contradictions, selected and excluded sources, company findings,
  uncertainty, KNI relevance, and next investigation.
- **Python boundary:** stable identity construction, provider policy, budget, receipt
  verification, URL safety, extraction normalization, candidate-bound validation,
  schema checks, and publication gates.
- **Comparison/repair:** WorkItems validate the selection and assessment of the full
  bounded raw candidate universe. Missing IDs, collisions, unavailable identities,
  omitted alternatives, and truncation fail closed. One tool-free repair replays the
  identical candidate packet and never rereads the provider.
- **Delegation/output:** `ResearchBrief` and focused/comparison variants separate
  facts, inferences, unknowns, sources used/excluded, limitations, and next steps.
  Recommendations do not themselves execute a downstream agent.
- **Trace evidence:** fake-model tests prove real SDK tool items, post-tool model
  visibility, stable IDs, full coverage, decoy/fabrication rejection, truncation gate,
  same-packet repair, no second repair, and no provider reread.
- **Specialist basis:** the model—not Python—chooses search actions and adjudicates
  source relevance, contradictions, evidence sufficiency, and final research judgment.

### Opportunity Scout — true specialist

- **Paths:** direct live and live-SDK WorkItem discovery both use the registered
  agent-owned search/score/compare loop. Supplied verified candidates support a bounded
  tool-free assessment alternate.
- **Model-visible input:** raw objective and constraints, raw web candidate packet,
  stable provider candidate IDs, distinct citation and opportunity-entity IDs,
  deterministic score inputs/results, evidence, freshness, and limitations.
- **Tools:** the model calls admitted search and scoring tools and may request bounded
  extraction. Python owns provider policy and exact arithmetic, not opportunity choice.
- **Model-owned decisions:** query strategy, eligibility, source/evidence quality,
  current versus stale status, fit, why-now interpretation, selected/excluded
  alternatives, rank order, uncertainty, and need for Research follow-up.
- **Python boundary:** numeric calculations, identity separation, deduplication,
  provider/budget bounds, lifecycle gates, receipt verification, approvals, and schema.
- **Comparison/repair:** the validator requires full bounded-candidate assessment and
  checks that each selected opportunity entity is grounded in the cited candidate.
  Fabricated mappings, missing alternatives, collision, missing IDs, and truncation
  block. Repair replays the same evidence with provider tools forbidden.
- **Delegation/output:** `OpportunityScoutResult` carries candidates, filters, ranks,
  evidence, quality notes, limitations, and an agent-owned
  `handoff_to_business_research_analyst` decision. The validated handoff controls the
  next WorkItem agent rather than serving as decorative prose.
- **Trace evidence:** fake-model WorkItem tests prove tool sequence, stable identity
  mapping, comparison, formal gates, one no-reread repair, and model-owned handoff.
- **Specialist basis:** the model owns opportunity relevance, ranking, exclusions, and
  delegation over a verifier-bound universe; Python supplies arithmetic and gates.

### Outreach Composer — true specialist

- **Paths:** canonical WorkItem and direct specialist paths supply approved context to
  a tool-free model decision. Variant/revision paths retain the same approval and claim
  constraints. Positively evidenced provider-free transformations may instead use the
  separate synthesis-only `DirectAgentResponse` lane.
- **Model-visible input:** raw request, objective, recipient/thread context, approved
  and blocked claim IDs, source basis, style constraints, channel, CTA constraints,
  prior draft when revising, limitations, and the no-send boundary.
- **Tools:** canonical drafting needs no model-called provider tool. Context acquisition,
  claim approval, recipient verification, and any persistence are workflow-called and
  receipt-bound before the model turn.
- **Model-owned decisions:** which approved claims to use or omit, narrative order,
  tone, personalization boundary, CTA, response strategy, wording, and when evidence is
  insufficient to draft.
- **Python boundary:** recipient readiness, approved-claim membership, external-use
  approval, unsupported-claim rejection, PHI/no-send rules, length, output schema, and
  exact write scope.
- **Comparison/repair:** validators require selected claim/source IDs to be approved
  and grounded; invalid claim use or contract failure may receive one evidence-preserving
  repair. Python never swaps in a preferred claim or silently cleans a send-unsafe draft.
- **Delegation/output:** `OutreachDraft` contains subject/body or channel copy,
  claims/sources used, rationale, review flags, limitations, CTA, and approval state.
  It is specialist work because the model makes a bounded persuasion and evidence-use
  decision, not because it produces polished prose.
- **Trace evidence:** fake-model and WorkflowRunner tests cover raw input, approved
  claim selection, unsupported-claim rejection, revision constraints, and fail-closed
  validation. No live outbound draft or send was attempted.
- **Specialist basis:** verified context is preacquired, but the substantive evidence,
  audience, CTA, and wording decisions remain model-owned.

### Airtable Context — bounded specialist

- **Paths:** direct CLI and Chief-nested live reads use the registered context agent.
  Exact marked-test record lifecycle operations are deterministic typed operations.
  Airtable is not yet a first-class WorkflowRunner route.
- **Model-visible input:** raw request, schema/context, actual provider candidate records
  with public stable identities, relevant fields, limitations, and completed-read
  evidence replayed during correction or repair.
- **Tools:** schema/list/search/read and narrowly gated write tools are model-callable
  only when admitted. Direct-path read results come back through the same SDK loop.
  Completed reads and all mutations are disabled for evidence-preserving repair.
- **Model-owned decisions:** schema-to-intent mapping, required read/tool choice,
  record relevance, comparison, ambiguity, selected/excluded records, and specialist
  conclusion. Exact write authorization and identity remain deterministic.
- **Python boundary:** schema normalization, permissions, live flags, approval,
  candidate identity, exact fields/records, read-back, marked-test restrictions, and
  decision validation.
- **Comparison/repair:** a missing required tool call gets one bounded corrective turn.
  A semantically invalid decision gets one separate tool-free repair over the same
  verified evidence. Failure after either opportunity blocks rather than substituting.
- **Delegation/output:** `AirtableContextResult` carries selected records, evidence,
  gaps, limitations, recommended action, and decision data. Chief consumes a validated
  private child envelope rather than trusting child prose.
- **Trace evidence:** direct fake-model tests cover missing-tool correction, mutation
  disabling, full candidate validation, semantic repair, and truthful exhaustion.
  Chief tests cover hard live-read enforcement and private child trace persistence.
- **Why bounded:** strong direct and nested decisions exist, but WorkflowRunner does
  not yet expose a first-class Airtable specialist route and its production wrapper
  mapping does not prove equivalent execution.

### Google Workspace Context — bounded specialist

- **Paths:** direct CLI and Chief-nested live reads use the registered agent. Exact
  marked-test document lifecycle operations are deterministic. Workspace is not a
  first-class WorkflowRunner route.
- **Model-visible input:** raw request, Drive/Docs/Sheets schema and bounded search/read
  candidates, stable public identities, normalized content, limitations, and replayed
  completed-read evidence.
- **Tools:** model-callable schema/search/read and narrowly gated write tools are
  request-scoped. Provider reads occur inside the agent loop on the strong path; repair
  disables completed reads and mutation tools.
- **Model-owned decisions:** artifact type and search/read choice, relevance, document
  comparison, requested transformation, ambiguity, exclusions, and conclusion.
- **Python boundary:** provider identity, schema normalization, permissions, approvals,
  live flags, exact operation scope, read-back, and output validation.
- **Comparison/repair:** one missing-tool corrective turn is distinct from one semantic
  evidence-replay repair. The candidate validator rejects unsupported selections and
  blocks after exhausted repair.
- **Delegation/output:** `GoogleWorkspaceContextResult` preserves selected artifacts,
  evidence, limitations, recommended actions, and decision data. Validated Chief child
  results are persisted privately into unified traces.
- **Trace evidence:** fake-model direct and Chief-nested tests cover correction,
  repair, hard live reads, no mutation in repair, and private trace hydration.
- **Why bounded:** model ownership is proved for direct/nested execution, but the lack
  of a first-class WorkflowRunner route leaves an architectural parity gap.

### Zotero Context — bounded specialist

- **Paths:** direct CLI and Chief-nested live reads use the registered Zotero agent.
  Zotero is not a first-class WorkflowRunner route.
- **Model-visible input:** raw request, collection/search/read candidates, bibliographic
  metadata and bounded notes/text, stable public identities, provenance, limitations,
  and replayed verified evidence.
- **Tools:** collection listing, search, item read, and bounded related-item operations
  are model-callable when admitted. Direct reads return within the model loop; repair
  forbids completed reads and mutations.
- **Model-owned decisions:** collection/search strategy, source relevance and quality,
  selected/excluded items, ambiguity, synthesis basis, and whether evidence is enough.
- **Python boundary:** authentication/live gates, identity, result limits, content
  normalization, permissions, receipt checks, and schema/decision validation.
- **Comparison/repair:** missing required acquisition triggers one bounded correction;
  a bad selection triggers one tool-free repair over the same verified candidate
  universe. Python does not replace the selected source.
- **Delegation/output:** `ZoteroContextResult` records relevant items, evidence,
  provenance, gaps, limitations, and decision data. Chief runs it as a validated child
  rather than accepting an unvalidated nested summary.
- **Trace evidence:** direct matrix and resilience tests cover model calls, tool
  correction, stable references, candidate validation, repair, and exhaustion; nested
  tests cover private decision persistence and redacted public output.
- **Why bounded:** the live direct/nested paths are specialist, but no first-class
  WorkflowRunner adapter currently carries the same decision contract.

### RSS Context — true specialist

- **Paths:** direct signal execution, signal WorkItem runtime, and Chief-nested child
  execution use the registered RSS agent. The no-live preview is fixture-only.
- **Model-visible input:** raw signal request, bounded history items with stable
  identities, timestamps/source metadata, prior state, inclusion limits, and relevance
  constraints.
- **Tools:** the first model turn must call the bounded history read tool. Python
  verifies the returned history universe and provider receipt.
- **Model-owned decisions:** relevance, novelty, continuity, selected/excluded items,
  grouping, limitations, and recommended follow-up.
- **Python boundary:** history read limits, normalization, item identity, recency,
  deduplication, permissions, schema, and candidate-universe validation.
- **Comparison/repair:** all selected signals must belong to the verified history
  result. One invalid decision is repaired by a tool-free agent over replayed evidence;
  the history provider is not read again. Exhaustion blocks.
- **Delegation/output:** `RssContextResult` carries selected signals, evidence,
  exclusions/limitations, and decision data. Chief receives a validated child envelope.
- **Trace evidence:** fake-model tests prove required first history call, no-repeat
  repair, evidence fingerprint continuity, zero provider calls during repair, nested
  validation, and unified trace hydration.
- **Specialist basis:** once the canonical entry path selects RSS Context, route
  authority—not request vocabulary—requires the history-bound runtime, candidate
  validator, and no-reread repair contract. The no-live preview remains explicitly
  fixture-only and is not evidence of model reasoning.

### Preprints Context — true specialist

- **Paths:** direct signal execution, signal WorkItem runtime, and Chief-nested child
  execution use the registered Preprints agent. The no-live preview is fixture-only.
- **Model-visible input:** raw topic/objective, bounded historical preprints, stable
  identities, titles/abstract evidence, source/date metadata, prior state, and limits.
- **Tools:** the first model turn must call the bounded preprint-history tool; its
  provider result is visible before the model selects or ranks papers.
- **Model-owned decisions:** topic relevance, novelty, evidence quality, selected and
  excluded preprints, ambiguity, limitations, and research follow-up.
- **Python boundary:** provider/date/result bounds, normalization, identity,
  deduplication, receipt checks, and candidate/schema validation.
- **Comparison/repair:** selections must be contained in the verified history packet.
  One invalid result gets a tool-free same-evidence repair; no second provider read and
  no second repair are allowed.
- **Delegation/output:** `PreprintsContextResult` records relevant preprints,
  provenance, selection basis, gaps, limitations, and decision data. Chief child
  execution is validated and privately traced.
- **Trace evidence:** signal-runtime and nested-agent fake-model tests cover required
  tool use, same-evidence repair, zero repair reread, exhaustion, and trace merging.
- **Specialist basis:** once the canonical entry path selects Preprints Context, route
  authority—not request vocabulary—requires the history-bound runtime, candidate
  validator, and same-evidence repair contract. The no-live preview remains explicitly
  fixture-only and is not evidence of model reasoning.

### Orchestrator — true specialist

- **Paths:** natural-language front doors call Orchestrator preflight before
  deterministic routing and specialist execution. Output review is a second bounded
  manager stage where wired. Narrow health/status and safety commands may remain
  deterministic.
- **Model-visible input:** unmodified request, compact Slack/thread context, WorkItem
  state, prior run summaries, decision trace/audit notes, context packs, memory, and
  deterministic route advice presented as advice rather than authority.
- **Tools:** ordinary preflight may be a legitimate tool-free manager decision.
  Request-scoped inspection/context tools and registered specialist tools are callable
  only when admitted; deterministic dispatch is traced separately from SDK handoff.
- **Model-owned decisions:** ambiguity resolution, route/owner, workflow order,
  clarification need, missing context, retrieval need, delegation recommendation,
  assumptions, safety notes, and output-review judgment.
- **Python boundary:** permissions, side-effect gates, exact route availability,
  WorkItem state transitions, approval, deterministic arithmetic/identity, and schema.
- **Comparison/repair:** the contract exposes route candidates and rejects unsupported
  owners, contradictions, and inconsistent `needs_more_context`. One bounded repair
  receives validator feedback; deterministic code does not silently relabel the route.
- **Delegation/output:** `OrchestratorResult` carries selected route, workflow,
  blockers, planner rationale, retrieval hints, safety notes, and decision data. The
  workflow runner must record how that decision was consumed.
- **Trace evidence:** fake-model tests cover raw-request visibility, ambiguous routing,
  unsupported-owner repair, workflow ordering, route/delegation ownership, and output
  review. Linked-run trace hydration is covered offline.
- **Specialist basis:** manager-specialist judgment is route and workflow choice over
  visible context; it need not call a provider tool to be substantive.

### Chief of Staff — bounded specialist

- **Paths:** live named and WorkItem Chief execution can select tools, synthesize
  verified context, and call registered child specialists. Some finance/status and
  direct Calendar/Gmail fast paths remain deterministic or use the owning specialist
  without a separate Chief decision.
- **Model-visible input:** raw request, Orchestrator memo, plan and admitted capability
  scope, WorkItem/context-pack data, verified provider/local-document evidence, prior
  runs, limitations, and validated child envelopes.
- **Tools:** request-scoped context/read tools and specialist agents are model-callable
  after deterministic admission. Python may preacquire verified context. A Chief child
  request cannot downgrade a required live read to fixture mode.
- **Model-owned decisions:** tool/delegation choice within admitted scope, evidence
  relevance, cross-source synthesis, role distinctions in local documents, conflicts,
  recommendation, limitations, and whether more specialist work is needed.
- **Python boundary:** tool admission, live/read enforcement, privacy, source identity,
  permissions, approval, exact operations, validation, and output rendering.
- **Comparison/repair:** Airtable/Workspace/Zotero child results are candidate-bound
  and receive one tool-free evidence repair; RSS/Preprints use the no-repeat signal
  repair. Invalid child prose cannot reach the Chief as a successful tool result.
- **Delegation/output:** nested agents execute validated child decisions, not raw
  `Agent.as_tool` summaries. Private SDK custom data preserves attempts, validator
  feedback, and child evidence lineage for unified traces while public output is
  redacted. `ChiefOfStaffResult` records conclusions, actions, owners, evidence,
  blockers, limitations, and specialist outcomes.
- **Trace evidence:** fake-model tests cover all five context children, fabricated or
  omitted child decisions, one repair, exhaustion, hard live-read enforcement, private
  custom-data persistence, linked-run hydration, and privacy redaction.
- **Why bounded:** a deterministic plan still pre-admits Chief tools, and several
  Chief-labeled or Chief-adjacent fast paths do not execute the same manager decision.
  The strong live/nested path itself is specialist.

## Cross-cutting corrections confirmed in this snapshot

- Calendar, although not one of the 11 registered agents, now performs one
  evidence-preserving, tool-free lookup-decision repair and persists privacy-safe CLI
  telemetry. It does not reread the provider or use Python to select after exhaustion.
- Gmail uses the same one-turn, no-requery repair principle for both ordinary mailbox
  selection and exact continuation. Live-SDK WorkItems now use the registered
  query/read/candidate-decision contract and block without a Python-selected substitute.
- RSS and Preprints replay the first verified history packet to a tool-free repair
  agent; the obsolete repeated-history-read gap is closed. Every selected direct signal
  route now enters this runtime without a lexical admission predicate.
- Direct Airtable, Workspace, and Zotero paths distinguish a missing-required-tool
  corrective turn from a later semantic repair and disable completed reads/mutations.
- Chief context-agent tools now validate child decisions, enforce live reads, repair
  once without rereading, and persist private child decision data into unified traces.
  Canonical plan context reaches nested builders, and Chief no longer defaults to the
  first planned workflow route when no validated model-authored handoff exists.
- Research and Opportunity WorkItems now use the registered agent-owned tool loops,
  complete bounded candidate validation, stable provider identities, separate
  citation/entity identities, formal fail-closed gates, and no-reread repair.
- Production-path trace compilation now hydrates linked runs, provider/tool origins,
  decision attempts, repairs, nested decisions, handoffs, receipts, and failure
  evidence when recorded. Missing evidence is reported as unavailable rather than
  invented.
- The 11-agent production-wrapper matrix resolves real callable wrappers for every
  registered agent. Read-only Orchestrator receipt reconciliation explicitly records
  deterministic output authority and whether it superseded the model result.

## Remaining confirmed gaps and priorities

1. **First-class WorkflowRunner parity for Airtable, Workspace, and Zotero.** Their
   direct and Chief-nested paths are now strong, but the canonical workflow route
   table still lacks equivalent adapters. The production wrapper matrix should point
   to the real direct/nested wrappers rather than a generic prepared-WorkItem symbol.
2. **Gmail legacy alternate-path parity.** Direct and live-SDK WorkItem execution now
   share the registered query/read/candidate-decision contract. Provider-only/non-SDK
   acquisition still performs deterministic selection and must remain explicitly
   labeled or be consolidated onto the specialist contract before it can count as
   model-owned Gmail judgment.
3. **Chief fast-path authority.** Canonical plan forwarding and model-authored durable
   handoff validation are fixed. Continue distinguishing a genuine Chief manager run
   from a Calendar/Gmail owner fast path, deterministic finance/status answer, or safe
   fixture response in output metadata and traces. Plan-based tool admission must not
   predetermine the semantic answer.
4. **Orchestrator alternate-path observability.** Read-only WorkItem inspection is now
   labeled as deterministic receipt reconciliation and records when it supersedes the
   model result. Direct named-Orchestrator SDK runs should still participate in the
   same durable linked-run trace-summary hydration as WorkItem runs. Runtime SDK
   handoffs remain disabled; Python dispatch consumption must remain observable if that
   architecture is retained.
5. **Complete entry-path coverage as an enforced registry contract.** The current
   11-agent wrapper matrix resolves real callable wrappers and records schemas, decision
   stages, expected read tools, and continuation policy. Extend it to name whether model
   execution is required, the candidate source, validator callable, repair policy, and
   trace origin. Symbol existence alone remains insufficient.
6. **Live proof remains pending.** The current audit establishes offline structural,
   fake-model, deterministic-validator, and trace-hydration proof. A later bounded,
   explicitly authorized checkpoint should inspect sanitized real SDK tool sequences
   and hydrated production traces without exposing provider IDs or message contents.

## Automated assertions to retain

For every registered agent and supported entry path, tests should assert:

- raw-request fingerprint appears in the model-visible input;
- exact admitted tool schemas are recorded, and attached-but-uncalled tools are not
  counted as model use;
- SDK tool call, provider result, and post-tool model decision occur in order when the
  model owns acquisition;
- workflow-prefetched evidence is explicitly labeled and completely serialized before
  a tool-free specialist decision;
- selected IDs are a subset of the raw bounded provider universe, all required
  alternatives are assessed, and separate provider/citation/entity identities do not
  collapse into one another;
- candidate truncation, collision, missing identities, fabricated mappings, or
  ungrounded entities fail before a semantic repair can make the output appear valid;
- a repair receives validator feedback plus the identical evidence fingerprint, has
  provider/mutation tools forbidden when appropriate, performs zero provider rereads,
  and cannot repeat indefinitely;
- repair exhaustion returns a blocker and never a Python-selected substitute;
- delegation is counted only when an observable handoff/tool call or downstream route
  consumes the validated model decision;
- deterministic no-live and exact-operation lanes record zero model requests and do
  not claim specialist reasoning;
- synthesis-only `DirectAgentResponse` admission proves positive provider-free context
  and rejects search, provider selection, durable writes, or missing substantive
  evidence;
- unified traces distinguish model-called, workflow-preacquired, tool-free,
  deterministic, child-agent, repair, and failed attempts while redacting private
  provider data;
- order reversal, objective change, weakened evidence, and plausible-decoy tests
  produce a reasoned decision change or explicit uncertainty.

## Natural-language backend scenarios

These scenarios test semantic depth, not agent-name keywords or formatting. They are
designed for fake providers/models first; live execution requires separate approval.

1. **Gmail:** “Find the current thread requiring my reply about the revised meeting,
   ignore the older cancelled thread, and explain why the other plausible thread is
   not current.” Return two close candidates, then make the first decision select an
   unread candidate and verify one no-requery repair.
2. **Research:** “Which of these two behavioral-health vendors has stronger evidence
   for health-system deployment, and what would change your view?” Include a glossy
   weak source, a primary source contradicting it, and an irrelevant high-ranked decoy.
3. **Opportunity:** “Rank the open opportunities for a small neuroinformatics firm;
   exclude expired or eligibility-mismatched items and delegate research only for the
   best unresolved company.” Test separate source and opportunity IDs plus truncation.
4. **Outreach:** “Draft a concise reply using only the two approved claims, omit the
   tempting unapproved outcome claim, and choose a low-friction CTA appropriate to a
   first conversation.”
5. **Airtable:** “Find the expense record that matches this date, vendor, amount, and
   attachment; compare the duplicate-looking row and do not prepare an update unless
   the identity is unique.” First omit the required read call, then return two records.
6. **Workspace:** “Find the current operating-plan document, distinguish it from the
   prior-year copy, and extract the accountable owner and unresolved milestone.” Test
   schema choice, version evidence, ambiguity, and one tool-free repair.
7. **Zotero:** “Choose the two strongest sources for the claim, exclude the topical but
   methodologically weak paper, and state the evidence limitation.” Include near-title
   duplicates and conflicting designs.
8. **RSS:** “From the last feed window, identify genuinely new clinical-AI signals,
   exclude syndications of an older item, and explain the follow-up value.” Force an
   invalid first selection and assert no second history read.
9. **Preprints:** “Select preprints that materially change the evidence picture, not
   merely match the keywords; separate replication from speculative novelty.” Include
   a keyword-heavy off-target decoy and one ambiguous item.
10. **Orchestrator:** “I need to understand this company and decide whether to contact
    them, but the evidence is incomplete.” Verify a research-before-outreach workflow,
    explicit missing context, and repair of an unsupported initial owner.
11. **Chief:** “Use the internal plan and literature context to identify the accountable
    owner, distinguish organizer from signer, and recommend the next safe action.” Have
    one child omit its decision, confirm one private child repair, and verify that the
    Chief sees only validated child evidence.

## Offline verification boundary

The focused current-tree suite completed with `261 passed` and no live calls. It
covered decision ownership, Research/Opportunity WorkItem tool loops, Calendar CLI
telemetry, Chief nested specialists, direct context-agent correction and repair,
RSS/Preprints no-repeat repair, Gmail decision contracts, `DirectAgentResponse`
admission, plannerless matrices, and trace compilation/hydration.

This proves code-level routing, fake-model behavior, deterministic validation,
repair invariants, privacy boundaries, and trace construction for the current dirty
snapshot. It does not prove provider availability, model quality on natural language,
credential/deployment alignment, or that a deployed Slack/CLI worker has emitted an
equivalent real production trace. Those require a later, bounded live checkpoint.

## Evidence index

The conclusions above were cross-checked against the canonical registry and policy
cards (`agent_registry.py`, `agent_decision_policy.py`), all 11 agent builders and
their markdown prompts, actual CLI/WorkflowRunner/Chief call sites, output schemas,
tool wrappers, validators, repair runtimes, request-cache/trace processors, and
representative fake-model fixtures. In particular:

- `entrypoints/cli_impl.py`, `workflow_runner.py`, `specialist_agent_tools.py`, and
  `runtime/signal_context.py` define the entry-path, correction, repair, and nested
  execution behavior.
- `agent_decision_contracts.py`, `decision_validation.py`, and the Gmail-specific
  decision-ownership module define model/Python authority and fail-closed checks.
- `tools/search_provider.py` and the WorkItem candidate-universe helpers define stable
  provider, citation, and entity identity boundaries.
- `run.py`, `trace_processor.py`, `runtime/execution_attempt.py`, and
  `runtime/decision_trace_harness.py` define observable tool origin, linked runs,
  repairs, child decisions, receipts, privacy filtering, and missing-evidence status.
- Focused proof came from `test_agent_owned_decisions.py`,
  `test_agent_owned_decisions_opportunity_outreach.py`,
  `test_agent_owned_retrieval_loops.py`,
  `test_work_item_agent_owned_research_opportunity.py`,
  `test_context_agent_fake_model_matrix.py`,
  `test_context_agent_resilience_boundaries.py`,
  `test_direct_signal_decision_runtime.py`,
  `test_decision_repair_evidence_replay.py`,
  `test_chief_nested_specialist_decisions.py`,
  `test_gmail_explicit_decision_contract.py`, `test_direct_response.py`,
  `test_calendar_cli_decision_telemetry.py`, `test_plannerless_agent_matrix.py`,
  `test_trace_processor.py`, and `test_backend_decision_trace_harness.py`.

Registry attachment and prompt assertions were treated only as supporting intent.
They were never counted as execution proof unless a callable entry path, model-visible
input, tool/receipt record, validator outcome, and trace/test assertion corroborated
the claimed behavior.
