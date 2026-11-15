# Graph variation review before live testing

Astra reviewed the active-channel graph pilot at medium reasoning on 2026-11-14 Eastern, using current code and provider-free reproductions. The review found two actionable defects. It did not run models, query providers, restart services, change configuration, or alter production data. The parent implementation and independent acceptance work are recorded separately below.

## Technical index

- [Evidence scope across normalization and tool admission](#evidence-scope-across-normalization-and-tool-admission)
- [Reader availability](#reader-availability)
- [Production-shaped recovery tests](#production-shaped-recovery-tests)
- [Verification boundaries](#verification-boundaries)
- [Final cached-resume review](#final-cached-resume-review-addendum)

## Evidence scope across normalization and tool admission

**P1, confirmed:** an explicit source override narrowed `WorkItem.sources`, but the supplied-source normalizer appended references from previously selected artifacts. A synthetic selected-only source set regained an unrelated historical source. A stale excerpt using the same source identity instead caused a contract failure.

A second reproduction showed a deeper disagreement: the citation catalog contained only the selected source, while the recursive SDK reader extractor admitted a historical artifact's web snapshot. Merely filtering the citation list would therefore leave the model-visible evidence and the actual reader scope inconsistent.

**Implemented repair:** an explicit supplied-source stage now builds its source normalization, context pack, and orchestration context from one projection containing only the chosen sources and no prior artifact payloads. Canonical saved artifacts remain untouched. Reader admission also rejects available web handles outside an explicit source catalog. Legacy inputs without a catalog retain their existing behavior; ordinary source-provided research can still discover deliberately selected artifact evidence.

The independent public-path regression identified one additional projection of the same stale evidence: a reusable Slack task brief had been prepared before source narrowing and retained a historical artifact summary. Explicit-source synthesis now omits that redundant derived brief; the current request, typed plan and scoped context pack remain intact. The saved brief and canonical artifact history are not modified.

**Required proof:** historical unrelated sources, stale same-ID excerpts, and historical reader handles cannot enter the exact-source stage's model input, citation universe, or reader scope. The original saved artifacts must remain in storage. An independently constructed current source override cannot authorize an unrelated historical reader.

## Reader availability

**P2, confirmed:** `retained_source_read_enabled` checked only retained evidence pages, although the workflow and SDK also support exact web snapshot handles. A web-only source input consequently advertised no retained reader while attaching one. This is a contract inconsistency; the audit did not establish that it caused the earlier live failure.

**Implemented repair:** the model-visible flag now includes available web snapshot handles as well as retained pages. Unavailable web handles are excluded from actual reader admission. The flag describes an admitted capability, not proof that a snapshot file was successfully read. A missing snapshot can still yield an honest unavailable result.

**Required proof:** preview-only and unavailable-web cases advertise no readable continuation; available-web and retained-page cases advertise the corresponding bounded reader.

## Production-shaped recovery tests

The preceding acceptance fixture populated a URL/read plan that the live model had omitted. The revised public-path scenario retains the actual variation: a heuristic target of unknown type, no provider operations, unspecified strictness, empty optional selected-source field, enabled search capability, a saved canonical target URL, and both prior route failure markers. It checks native query/read, exact saved-source binding, local snapshot reading, original WorkItem completion, and absence of discovery or repeated extraction.

A broader check also caught an applicability regression before another live run: validating every plan through the new source-resume adapter broke existing internal Chief handoff plans. The adapter now proves exact current continuation identity and model-selected ownership before validating the targeted public plan. Internal handoff intent values were preserved; the public schema was not broadened.

Conflict controls cover a changed current source, ambiguous saved target, write or broad-search requests, historical quoted URLs, unrelated owners, and exhausted request capacity. These checks distinguish trusted saved identity from current permission and avoid substituting a source or route for the model.

## Verification boundaries

The combined affected backend gate passed **1,149 tests**. After the final cached-brief correction, the independent public-path suite passed **40 tests**, including both historical-artifact variants, and the affected supplied-source/diagnostic controls passed **29 tests**. A separate reader compatibility gate passed **106 tests**. Astra's read-only repair review found no remaining defect within its two reported findings. Scripted native SDK tests establish wiring, scope enforcement, saved-state behavior and failure boundaries. They do not prove natural model choice, coverage honesty in a live answer, current external page access, or Slack delivery.

The bounded Astra review found no further issue with enough evidence to justify another pre-live repair. Missing-cache and broader plan-omission variations remain coverage questions rather than claimed production defects. No dependency, service, storage migration, or permission change was needed.

Refs: [source normalization and input](../src/keystone_agents/business_research_analyst/supplied_context.py), [reader admission](../src/keystone_agents/agents/business_research_analyst.py), [source continuation binding](../src/keystone_agents/planning/selected_source_resume.py), [workflow handoff](../src/keystone_agents/workflow_runner.py), [scope controls](../tests/test_supplied_source_scope.py), [public-path acceptance](../tests/test_slack_cos_graph_backend.py).

## Final cached-resume review addendum

A follow-up offline review on 2026-11-15 checked the latest early source binding and completed-WorkItem reuse repair. The source-binding and public CLI suites passed **45 tests**. A temporary public-path variant additionally asserted that the saved URL was actually visible to the scripted Orchestrator before it selected that URL; all **5 variants passed**. This verifies available input and wiring, not natural model selection. The earlier artifact-scope and reader-availability findings remain repaired; the later cached-resume host admission failure is a separate fix.

**P1, newly confirmed at review time: historical approved facts bypass the exact-source projection.** The public native graph reproduction added one approved research fact backed by an unrelated synthetic source to the saved WorkItem. Although the current request permitted only the selected cached article, the actual specialist model input contained that historical fact. The regression failed on an explicit model-input sentinel assertion, with HTTP, socket, provider, and subprocess boundaries denied. This is model-visible evidence contamination; the reproduction does not claim that a live model used the fact or delivered an incorrect answer.

The explicit evidence projection in `_supplied_business_research_sdk_result` replaces sources and removes prior artifacts but retains `WorkItem.facts`. `supplied_research_input` builds a research context pack whose `approved_facts` includes all facts accepted by `_approved_facts`, including their unrelated source references. Repair should scope these facts at the same explicit evidence boundary while retaining canonical saved state and keeping operator preferences separate from source evidence. Verification must include unrelated facts, stale same-source facts, and preservation of saved facts. This issue remained open when this addendum was written; a subsequent repair needs its own result recorded.

The review did not classify rejected external-use approvals as a read-only defect: research readiness and external-use approval have different purposes. Session-history contamination was not reproduced in this pilot; eligible compact continuity explicitly disables raw SDK history. No provider calls, service changes, or production state mutations were performed.

Refs: [explicit evidence projection](../src/keystone_agents/workflow_runner.py), [research input](../src/keystone_agents/business_research_analyst/supplied_context.py), [context-pack approved facts](../src/keystone_agents/work_items.py), [cached-resume public regression](../tests/test_compact_continuity_source_resume.py).

A second strict-offline public-path variant confirmed another copy of the same defect: `target.metadata.project_context.source_refs` retained an unrelated historical excerpt in the actual specialist model input. The research pack includes both the target metadata and the normalized project context. The fix therefore needs to scope substantive source evidence across these projections while preserving project identity, governance constraints, and operator preferences. An unrelated available reader handle is independently rejected by the explicit source catalog, but that check does not remove historical fact/excerpt text without a handle. Both reproductions remain pending parent repair verification.

**Repair independently verified:** the new `project_explicit_research_evidence` helper creates a stage-local copy with facts and artifacts omitted and project source references removed, preserving project identity, goals, governance, and the canonical WorkItem. The workflow applies it before orchestration context and research context-pack construction. Both original public-path reproductions were rerun unchanged against the repair and **passed (2 tests)**: neither historical approved-fact text nor unrelated project-source excerpt reached the specialist model input, while the exact cached-source read and same-WorkItem completion succeeded. This closes the two reproduced paths of the P1 finding; it does not establish live model behavior or exhaustive coverage of all metadata fields.
