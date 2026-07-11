# Keystone Docs Index

Start here when changing or operating the repository.

## Run Locally

- `README.md`: setup, dry-run commands, package overview.
- `docs/RUNBOOK.md`: operator checklist for local and live-test workflows.
- `docs/DEPLOYMENT.md`: live integration setup, rollback, cost controls, and operations.
- `docs/OPENAI_AGENT_PLATFORM_SETUP.md`: OpenAI dashboard, tracing, and eval setup.
- `docs/SLACK_BUSINESS_AGENT_MODE.md`: `@KNI` Slack bridge flags, scopes,
  result rendering, and approval boundaries.
- `docs/AI_AGENTS_WORKFLOW_TEST_STATUS.md`: current `#ai-agents-workflow`
  named-agent testing status, gaps, and next low-cost probe plan.
- `docs/ANU60_LIVE_SLACK_PROOF_PLAN.md`: focused ANU-60 live Slack proof plan
  for answer-first rendering, metadata suppression, named-agent routing, and
  blocker wording after explicit live-posting approval.
- `docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md`: copyable evidence template for
  filling the ANU-60 live Slack probe results after approval.
- `docs/AGENT_CAPABILITY_BOUNDARIES.md`: ANU-193/194/124 Orchestrator and Chief
  read/write/modify boundaries, agents-as-tools, durable handoff, graph selector,
  unresolved decisions, and test acceptance criteria.
- `docs/BASIC_AGENT_EXECUTION_SMOKE_TASKS.md`: concise ANU-174 operator smoke
  queue with offline, live-read, and write-gated validation lanes.
- `docs/ANU174_LINEAR_TASK_EVIDENCE.md`: exact row-by-row evidence map for the
  distinct 20 tasks listed in Linear issue ANU-174.
- `docs/HUMAN_AGENT_EXECUTION_JOBS.md`: natural user jobs across every agent,
  with reasoning, execution, modification, verification, and cleanup proof.
- `docs/AGENT_OPERATIONAL_VALIDATION_STATUS.md`: evidence-backed status for
  every agent family across interpretation, tool/provider execution, reasoning,
  lifecycle, safety, continuation, and next proof.
- `docs/MANAGER_RWM_ACCEPTANCE.md`: low-friction manager review plus ANU-222
  advanced-scenario and ANU-223 delegation-readiness gates.
- `docs/ANU223_LIVE_DELEGATION_PLAN.md`: proposed serial request/cost ceilings,
  exact provider scopes, evidence requirements, and stop conditions for the
  three remaining manager-to-provider joins.
- `docs/INITIAL_OPERATIONAL_MODEL_VALIDATION.md`: plan-only first paid batch,
  fresh billing-baseline requirement, exact request estimate, and hard stops.
- `docs/AI_AGENTS_WORKFLOW_NO_LIVE_VALIDATION.md`: current pre-live proof gate
  for basic agent functions and backend-selected graph runs; the legacy
  Promptfoo suite is explicitly deferred for future migration.
- `docs/DIFFERENTIATION_MATRIX.md`: ANU-175 comparison and validation matrix
  defining how KBA should differ from ChatGPT and Codex.
- `docs/VISUAL_CONTEXT.md`: repo-local architecture visuals for operator and agent context.
- `docs/assets/kba-current-agent-architecture.svg`: generated current architecture visual.
- `scripts/render_agent_architecture_diagram.py`: regenerate the current architecture visual from
  the agent registry, workflow, trace, and eval structure.

## Natural-Language Workflows

- `keystone ask` without `--agent`: WorkItem-backed natural-language entrypoint
  in dry-run mode that saves artifacts, readiness gates, next actions, timeline
  events, and the `ManualRequestPlan`.
- `keystone ask --agent ...` or explicit `@KNI <agent>`: registered-agent path
  where the explicit mention is route advice. In live planning/model paths, the
  raw request still goes through Orchestrator-first interpretation before the
  selected specialist runs. It stays dry-run by default, but live-test/full-live
  environments auto-enable live SDK model execution unless `--no-live-sdk` is
  passed. Live manual Business Research Analyst, Opportunity Scout, Gmail
  Triage, and Chief of Staff calls use script-backed execution paths where
  available.
- `@KNI keystone ask ...`: Slack-friendly alias for the same natural-language
  entrypoint.
- SDK sessions: local SQLite conversation continuity for live SDK follow-ups.
  Chief of Staff `ask` runs and live WorkItems enable scoped sessions by
  default; other specialists require inherited context or explicit opt-in.
  `--sdk-session-id`, `--sdk-session-db`, and `--no-sdk-session` control it.
- `--live-manual-plan`: optional LLM planning stage. The planner stores compact
  route guidance while Orchestrator preflight reads the raw request and context
  before specialists execute. Configure planner provider routing with
  `KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY`.
- `work-items continue/show/timeline/select/approve-context`: state management
  commands for existing WorkItems.
- `work-items advance --langgraph`: optional graph-native WorkItem execution
  with approval-checkpoint metadata and future resumable orchestration.
- `docs/MULTI_AGENT_WORKFLOW_TEMPLATES.md`: backend-selected templates for
  independent, combined, scheduled, and modification-loop agent workflows.
- `docs/CONTEXT_AGENT_CONTRACTS.md`: ANU-198 through ANU-202 context-agent
  read/write/modify boundaries, graph-candidate edges, and evidence handoffs.

## Add Or Change Agents

- `docs/ADD_AGENT.md`: checklist for adding a new SDK agent safely.
- `docs/AGENTS_SDK_CONFORMANCE.md`: project mapping to OpenAI Agents SDK concepts.
- `docs/AGENTS_SDK_REVIEW.md`: current SDK/public-implementation review,
  context-source map, and MCP decision rules.
- `src/keystone_agents/agent_registry.py`: canonical `AgentSpec` registry and agent cards.
- `docs/AGENT_LEARNING_LOOP.md`: feedback to eval to prompt-version promotion path.

## Add Or Change Tools

- `AGENTS.md`: tool boundary, safety, dry-run, and live flag rules.
- `src/keystone_agents/tools/`: explicit tool wrappers and integration boundaries.
- `docs/corpus/`: approved FileSearch corpus manifest, local resource notes, and
  selected vendored official docs/specs.
- `scripts/ingest_file_search_corpus.py`: dry-run-first upload helper for putting
  the approved corpus into an OpenAI vector store.
- `tests/test_tool_impls.py` and integration-specific tests: tool behavior and safety coverage.

## Change Prompts Or Evals

- `src/keystone_agents/prompts/`: markdown prompts with metadata headers.
- `docs/EVALS.md`: benchmark strategy, deterministic evals, local evals, and future trace/rubric eval plan.
- `docs/SEARCH_COVERAGE_EVALS.md`: query-level search provider coverage evals.
- `docs/BROWSER_EXTRACTION_EVALS.md`: rendered page provider evals for Browserless,
  Firecrawl scrape, and future browser adapters.
- `docs/AGENT_IMPROVEMENT_TEST_PACK.md`: standing improvement backlog.
- `evals/`: static, local, and provider-specific eval datasets.

## Review Safety

- `docs/SAFETY.md`: safety model and approval boundaries.
- `docs/HUMAN_INTERFACE.md`: human review and operator interaction model.
- `docs/SANDBOX_AGENTS.md`: optional staged-workspace review model.
- `src/keystone_agents/storage/sqlite_store.py`: local audit storage and redaction behavior.

## Runtime State

Local state defaults remain backward compatible. Prefer setting `KEYSTONE_HOME` for new local
workspaces:

```bash
export KEYSTONE_HOME="$PWD/.keystone"
keystone init-db
```

With no `DATABASE_URL`, this stores SQLite state under
`$KEYSTONE_HOME/state/keystone_agents.db`. `DATABASE_URL` still wins when set.
