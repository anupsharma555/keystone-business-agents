# KBA Architecture Decision Log

This is a durable log of architecture decisions the project has already made.
It is not a runtime decision trace, routing log, or place where an agent records
per-run choices.

Runtime decisions belong in WorkItem state, `decision_trace`, audit events,
agent run metadata, and test artifacts. This log records the stable project
policy those runtime systems should follow until a decision is revisited.

Each entry stays short:

- decision
- why
- status and recorded timestamp
- proof required to revisit
- owning references

## 2026-07-06T15:37:35-0400 - LangGraph Backend Selection

Status: accepted

Agentic design need: choose the smallest execution surface that preserves the
state, checkpoints, and handoffs the workflow actually needs.

Decision: select LangGraph by backend policy for meaningful WorkItem workflow
boundaries, not because prompt text mentions graph concepts.

Why: this keeps simple specialist work cheap while preserving graph state,
checkpoints, and multi-step handoffs when they add operational value.

Proof required to revisit: backend-selection tests or run traces show missed
multi-step value, excessive graph selection, or repeated wrong simple-vs-graph
routing.

Owning references: `docs/LANGGRAPH_OPTION.md`; ANU-168; ANU-170.

## 2026-07-06T15:37:35-0400 - Graph Manager Reasoning

Status: accepted

Agentic design need: preserve model reasoning only where qualitative judgment
adds value and deterministic gates are still authoritative.

Decision: keep default graph completion review deterministic. Optional model
reasoning is limited to relevance/usefulness review, repair routing, source
sufficiency judgment, and final answer satisfaction, with `review_mode`,
`llm_review_used`, `cost_guard`, and deterministic gate authority recorded.

Why: some manager judgments need qualitative review, but default graph execution
must not add hidden API spend or weaken Python gates.

Proof required to revisit: offline/fake-review tests plus live-budgeted traces
show deterministic review misses useful repair/final-answer decisions, without
increasing side effects or default spend.

Owning references: `docs/LANGGRAPH_OPTION.md`;
`src/keystone_agents/langgraph_quality.py`; ANU-183.

## 2026-07-06T15:37:35-0400 - Initial Query Search

Status: accepted

Agentic design need: treat search as evidence acquisition, not hidden manager
reasoning.

Decision: treat initial query planning as specialist/tool behavior, not a
separate manager-review touchpoint. Search execution, provider caps, live flags,
and escalation stay under deterministic retrieval policy; weak initial search
feeds source-sufficiency or repair review.

Why: query planning can improve recall, but provider, budget, and source
controls need stable Python enforcement.

Proof required to revisit: search coverage evals show query planning
consistently misses source lanes, or traces show model-assisted planning
improves source quality within the approved live budget.

Owning references: `docs/SEARCH_COVERAGE_EVALS.md`;
`src/keystone_agents/retrieval_policy.py`; ANU-122; ANU-65.

## 2026-07-06T15:37:35-0400 - Search-Provider Ladder

Status: accepted

Agentic design need: keep retrieval quality, provider selection, and budget
policy outside prompt-specific branches.

Decision: use the shared retrieval/search-provider ladder with dry-run default,
explicit live flags, provider budgets, and deterministic quality gates before
deepening.

Why: search quality, source attribution, provider reachability, and cost
controls should not become prompt-specific branches.

Proof required to revisit: provider evals or run traces show provider order,
caps, extraction path, or fallback rules materially lower source quality or
waste budget.

Owning references: `docs/SEARCH_COVERAGE_EVALS.md`;
`src/keystone_agents/retrieval_policy.py`; ANU-122.

## 2026-07-06T15:37:35-0400 - Skill-Contract Boundaries

Status: accepted

Agentic design need: keep instruction surfaces, runtime skills, tools, and
deterministic gates separate so capability does not leak across layers.

Decision: keep repo-local Codex skills, runtime agent skills, prompts,
AgentSpec metadata, explicit tools, and deterministic gates as separate
contract layers.

Why: this prevents guidance bloat and avoids granting tools, write permission,
or hidden routes through the wrong instruction surface.

Proof required to revisit: repeated implementation mistakes trace to the current
boundary, or skill-selection tests show required agent behavior cannot be
expressed without changing the contract.

Owning references: `docs/SKILLS_ARCHITECTURE.md`; `codex-skills/`; ANU-171.

## 2026-07-06T15:37:35-0400 - Read/Write/Modify Capability Contracts

Status: accepted

Agentic design need: make every read, draft, write-plan, modify, and external
side-effect path explicit by owner, scope, and approval gate.

Decision: define read, write, and modify capability contracts per agent and
provider. Live writes remain explicitly approval-gated and owned by the correct
specialist or action handler.

Why: KBA can expand beyond read-only answers only when ownership, scope,
approval reference, and side-effect evidence are exact.

Proof required to revisit: a concrete workflow cannot be implemented safely
under the current ownership split, or approved live traces show the split blocks
high-value operator work.

Owning references: `docs/AGENT_CAPABILITY_BOUNDARIES.md`;
`docs/TARGET_ACTION_MATRIX.md`; ANU-193; ANU-194; ANU-124; ANU-196.

## 2026-07-06T16:52:00-0400 - Context-Agent Graph Promotion

Status: accepted

Agentic design need: preserve context evidence without turning read/context
agents into hidden workflow owners or write authorities.

Decision: keep Airtable, Google Workspace, Zotero, RSS, and Preprints context
agents as tool/context nodes by default. Promote them to graph stages only when
the backend sees a real WorkItem boundary that needs checkpointing, source/ref
preservation, approval review, retry behavior, or multi-context sequencing.
Do not promote them to standalone subgraphs until fixture-backed validation
shows a recurring stateful loop.

Why: context agents should pass bounded evidence into Business Research,
Opportunity Scout, Outreach Composer, or Chief of Staff without weakening
source attribution, exact identity, approval gates, or no-write boundaries.

Proof required to revisit: no-live fixtures or run traces show repeated loss of
context refs, repeated wrong routing, or repeated unsafe retry behavior that a
dedicated context subgraph would prevent.

Owning references: `docs/CONTEXT_AGENT_CONTRACTS.md`;
`src/keystone_agents/context_agent_contracts.py`; ANU-198; ANU-199; ANU-200;
ANU-201; ANU-202.

## 2026-07-06T15:37:35-0400 - Live-Test Budget Thresholds

Status: accepted

Agentic design need: separate architecture validation spend from normal agent
execution and internal SDK request telemetry.

Decision: treat live SDK/search validation budgets and internal SDK request
thresholds as separate controls. Static, fixture, mocked, dry-run, and
single-agent diagnostics come before live API validation.

Why: this prevents architecture validation from becoming hidden default spend or
broad live-eval drift.

Proof required to revisit: focused smoke tests show thresholds are too low to
validate real workflows, or cost telemetry shows thresholds are too loose for
routine use.

Owning references: `AGENTS.md`; `docs/LANGGRAPH_OPTION.md`; ANU-176.

## Update Rule

Add an entry only when an architecture choice is recurring across issues, tests,
docs, or agent behavior. Do not use this log for one-off implementation notes or
run-specific agent decisions. Revisit a decision only with the proof listed
above, not with preference or prompt wording alone.
