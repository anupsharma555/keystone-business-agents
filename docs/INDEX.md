# Keystone Docs Index

Start here when changing or operating the repository.

## Run Locally

- `README.md`: setup, dry-run commands, package overview.
- `docs/RUNBOOK.md`: operator checklist for local and live-test workflows.
- `docs/DEPLOYMENT.md`: live integration setup, rollback, cost controls, and operations.
- `docs/OPENAI_AGENT_PLATFORM_SETUP.md`: OpenAI dashboard, tracing, and eval setup.
- `docs/SLACK_BUSINESS_AGENT_MODE.md`: `@KNI` Slack bridge flags, scopes, and
  approval boundaries.
- `docs/VISUAL_CONTEXT.md`: repo-local architecture visuals for operator and agent context.

## Natural-Language Workflows

- `keystone ask` without `--agent`: WorkItem-backed natural-language entrypoint
  in dry-run mode that saves artifacts, readiness gates, next actions, timeline
  events, and the `ManualRequestPlan`.
- `keystone ask --agent ...` or explicit `@KNI <agent>`: direct registered-agent
  path, including specialists, Orchestrator, and Chief of Staff. It stays
  dry-run by default, but live-test/full-live environments auto-enable live SDK
  model execution unless `--no-live-sdk` is passed. Live manual Business
  Research Analyst, Opportunity Scout, Gmail Triage, and Chief of Staff calls
  use script-backed execution paths where available.
- `@KNI keystone ask ...`: Slack-friendly alias for the same natural-language
  entrypoint.
- SDK sessions: local SQLite conversation continuity for live SDK follow-ups.
  Chief of Staff `ask` runs and live WorkItems enable scoped sessions by
  default; other specialists require inherited context or explicit opt-in.
  `--sdk-session-id`, `--sdk-session-db`, and `--no-sdk-session` control it.
- `--live-manual-plan`: optional LLM planning stage. Configure planner provider
  routing with `KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY`.
- `work-items continue/show/timeline/select/approve-context`: state management
  commands for existing WorkItems.
- `work-items advance --langgraph`: optional WorkItem graph wrapper for
  approval-checkpoint metadata and future resumable orchestration.

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
- `docs/EVALS.md`: deterministic and local eval guidance.
- `docs/SEARCH_COVERAGE_EVALS.md`: query-level search provider coverage evals.
- `docs/BROWSER_EXTRACTION_EVALS.md`: rendered page provider evals for Browserless,
  Firecrawl scrape, and future browser adapters.
- `docs/AGENT_IMPROVEMENT_TEST_PACK.md`: standing improvement backlog.
- `evals/` and `tests/evals/`: JSONL and fixture-backed eval datasets.

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
