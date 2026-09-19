# Keystone Business Agents

`keystone-business-agents` is the agent layer for Keystone business development
and operating workflows. The repo is meant to turn natural-language requests
from CLI, Slack, scheduled jobs, and local operator workflows into structured,
reviewable work across research, opportunity qualification, email triage,
internal context lookup, and approval-gated outreach drafting.

The project is built around a simple boundary: agents may research, reason,
prepare artifacts, create drafts, and stage internal plans, but they do not send
external messages or perform unapproved business-system writes.

For a first local run, use [Setup](#setup) and [Common Commands](#common-commands).
For operator modes, runtime versions, live flags, and troubleshooting, use
`docs/RUNBOOK.md`.

## What This Repo Aims To Do

- Coordinate Keystone business workflows through typed agents instead of
  one-off scripts.
- Preserve source-backed research, opportunity reasoning, approval state, and
  audit trails as local WorkItems.
- Let specialists use structured context from Gmail, Airtable, Google
  Workspace, Zotero, Slack, search providers, local files, and prior artifacts
  without blurring safety boundaries.
- Support practical operator workflows: triage inbound email, research a
  company or topic, assess whether an opportunity is worth pursuing, draft
  outreach from approved context, and summarize operational state.
- Keep side effects explicit: live reads, draft creation, internal writes, and
  future external actions each require their own reviewed flags, adapters, and
  approval references.

## System Shape

The runtime has five main layers:

1. **Orchestrator and Chief of Staff**
   The Orchestrator is the first control plane for natural-language `@KNI`,
   Slack, WorkItem, scheduled automation, and named-agent requests. It reads the
   raw request and compact context, owns the semantic route/workflow decision,
   records blockers and retrieval needs, and passes the validated internal
   decision plus the original request to the selected specialist. Deterministic
   code may reject an invalid decision, but it must not silently invent a
   different business decision.
   Chief of Staff handles broad operational synthesis, context-agent
   coordination, and structured handoffs when a downstream specialist should own
   the next WorkItem step.

2. **Specialist SDK Agents**
   Each specialist is an OpenAI Agents SDK `Agent` with markdown prompts,
   Pydantic structured output, explicit tool wrappers, and registry metadata.
   The stable specialist routes are Gmail Triage, Business Research Analyst,
   Opportunity Scout, and Outreach Composer. A request-scoped capability layer
   admits the safe toolbox for the run. Depending on the execution path,
   evidence may come from model-called tools, workflow-called tools/helpers, or
   verified context acquired before the model turn; runtime telemetry keeps
   those origins distinct.

3. **WorkItems, Context Packs, And Gates**
   WorkItems in local SQLite are the canonical workflow state. Context packs
   carry approved facts, selected artifacts, source refs, blockers, recipient or
   thread metadata, and next-action state into specialists. Python gates remain
   authoritative for source sufficiency, recipient readiness, Gmail thread
   readiness, approval state, and no-send boundaries.

4. **Optional LangGraph Execution**
   LangGraph is an optional graph runtime at the WorkItem orchestration
   boundary. Backend selection can use graph nodes for multi-step WorkItem runs,
   resumes, approval checkpoints, context-agent handoffs,
   Gmail-to-research/reply paths, and Chief-of-Staff coordination. It does not
   replace SDK agents, WorkItems, context packs, or Python safety gates.
   Details live in `docs/LANGGRAPH_OPTION.md`.

5. **Decision Validation, Recovery, And Observability**
   Where the shared decision wrapper is wired, agent decisions use typed records
   checked against the candidate identities and provider evidence visible to
   that agent. Required tool-use postconditions and bounded correction/repair
   complement provider receipts and safe retry points; completed writes must
   never be repeated merely to repair model output. Execution summaries distinguish
   attached tools from actual model calls, workflow calls, pre-acquired context,
   deterministic helpers, provider attempts/successes, retained receipts, and
   `performed` / `not_performed` / `unknown` external-write state.

These are shared architecture primitives, not one uniform call path. In the
current snapshot, Airtable, Google Workspace, and Zotero have direct live CLI
paths that `WorkflowRunner` does not dispatch; RSS and Preprints use the signal
runtime; Chief-of-Staff context specialists use validated child wrappers over
their agent tools. The shared direct corrective turn is therefore not evidence
of universal coverage. Read exact per-agent execution and recovery behavior
from current call sites and traces.

Direct Airtable, Workspace, and Zotero now bind decisions to provider-authoritative
candidates, retain cumulative telemetry, and preserve mutation-safe evidence
across one semantic repair. Their direct CLI path first permits one bounded
missing-required-tool correction, then applies semantic decision
validation/repair. Completed read evidence is replayed with completed tools
disabled; mutations are never repeated, and unavailable replay or a failed
corrected postcondition fails closed. These routes remain outside first-class
`WorkflowRunner` specialist dispatch.

Live Business Research and Opportunity WorkItems now keep retrieval, selection,
ranking, and handoff judgment inside the owning SDK specialist. The model and
validator share stable provider candidate IDs; one semantic repair replays the
same bounded universe without another provider read. Python validates identity,
bounds, and formal gates but does not choose a replacement candidate or handoff.

Chief nested context children enforce live reads on isolated tool copies, keep
mutation tools absent, and persist validated child decisions through SDK custom
data into the unified privacy-safe trace. A live parent-supplied `run_config` is
labeled `live_sdk`; private child identities stay out of model-visible and
public envelopes.

A model request is not itself a model-called tool. That label is reserved for
an observed SDK tool invocation and its returned output in the same run.
Likewise, the generic provider-free `DirectAgentResponse` lane is restricted to
positively evidenced supplied-text or attachment synthesis. It cannot intercept
provider actions, candidate selection, continuations, delegation, or specialist
schemas, and must not be cited as proof that a named specialist executed its
decision contract.

When a model-request limit is configured, one request-local hard ledger covers
the parent run, nested agent calls, reserved child-process work, and sandbox SDK
execution. Each actual model turn is admitted and counted before provider
dispatch; the next turn after the configured limit is rejected before the model
call. Request estimates expose semantic stage rows for the initial loop and any
conditional tool correction or decision repair. Transport and structured-output
retries are not duplicated as semantic stages, but every model request they
actually start remains ledger-bounded and observable.

Generated architecture visual:
`docs/assets/kba-current-agent-architecture.svg`

Integrated architecture and ordered execution visual:
`docs/assets/kba-integrated-agent-architecture.svg`

Regenerate them after agent, workflow, trace, or eval-structure changes:

```bash
.venv/bin/python scripts/render_agent_architecture_diagram.py
.venv/bin/python scripts/render_agent_execution_diagrams.py
```

## Agent Set

Workflow specialists:

- **Gmail Inbound Triage Agent** classifies inbound email, recommends labels,
  flags safety issues, and prepares draft-only reply recommendations.
- **Business Research Analyst** builds source-attributed briefs for companies,
  institutes, conferences, labs, topics, Zotero collections, and article sets.
- **Opportunity Scout Agent** finds, evaluates, and scores potential Keystone
  opportunities while preserving source and approval requirements.
- **Outreach Composer Agent** drafts email or LinkedIn copy only from approved
  context and keeps output pending human approval.

Read-only or write-planning context agents:

- **Airtable Context Agent** resolves configured bases, tables, fields, and
  bounded records for context and approved write planning.
- **Google Workspace Context Agent** reads scoped Drive, Docs, Sheets, and file
  metadata for artifact-placement and context decisions.
- **Zotero Context Agent** reads library, collection, item, importer, and
  evidence context; mutation remains limited to guarded importer paths.
- **RSS Context Agent** evaluates bounded announcement history and remains
  read-only.
- **Preprints Context Agent** evaluates bounded preprint evidence and remains
  read-only.

The Calendar Action Interpreter is a supporting typed SDK agent rather than a
registry route. It interprets one bounded Calendar action; deterministic code
still owns identity resolution, approval/live gates, provider execution, and
read-back verification.

The canonical registry is `src/keystone_agents/agent_registry.py`. It is the
source of truth for agent builders, schemas, prompt files, tools, live flags,
validation paths, handoff descriptions, and safety notes.

## Key Capabilities

- Natural-language `@KNI` and CLI routing through Orchestrator-first WorkItems.
- Source-backed company, topic, collection, and opportunity research.
- Shared search and extraction policy for web research, website content,
  provider diagnostics, and source-visible synthesis.
- Gmail read, label, and draft-only workflows behind live flags.
- Slack bridge contracts for thread-aware replies, approval cards, background
  WorkItem actions, and human-summary-first rendering.
- Local SQLite audit storage for WorkItems, events, approvals, drafts, sources,
  tool calls, and benchmark/eval records.
- Typed agent-decision validation, bounded recovery, and path-specific
  tool/provider/receipt telemetry. SDK summaries carry the strongest current
  evidence. The production compiler now hydrates nested attempts, linked
  manager/specialist run IDs, actual tool/context/provider origins, and CLI
  multi-run linkage while preserving privacy-safe unknown evidence.
- Repo-local `SKILL.md` bundles that encode reusable reasoning contracts such as
  source triage, evidence handling, tool-result resilience, handoff packaging,
  action boundaries, and output review.

## Setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel -c constraints/ci.txt
.venv/bin/python -m pip install --no-build-isolation -e ".[dev]" -c constraints/ci.txt
```

The V2 dependency baseline is `openai-agents>=0.22.2,<0.23` with
`openai>=3.12,<4`. Use the tested [dependency constraints](constraints/README.md)
for reproducible development and CI installs. Confirm the installed version with:

```bash
.venv/bin/python -c 'from importlib.metadata import version; print(version("openai-agents"))'
```

Optional LangGraph runtime:

```bash
.venv/bin/python -m pip install --no-build-isolation -e ".[orchestration]" -c constraints/ci.txt
```

The orchestration extra includes LangGraph 1.2.11 or newer and the SQLite
checkpointer. Saved graph runs use native checkpoints; direct execution retains
the shared local operation journal. See the [V2 implementation report](docs/KBA_V2_IMPLEMENTATION.md)
for inspection/resume commands and proof boundaries, and the
[V2 experiment guide](docs/KBA_V2_EXPERIMENTS.md) for controlled comparisons.

Initialize local SQLite state:

```bash
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/health_check.py
```

Optional live configuration can be copied from `.env.example`:

```bash
cp .env.example .env
```

This repo uses `KEYSTONE_OPENAI_API_KEY` for live OpenAI SDK work. It does not
assume that a generic `OPENAI_API_KEY` belongs to this project.

## Common Commands

With the normal local defaults, these commands remain fixture-backed and
dry-run-safe. In `live-test` or `full-live` mode, explicit named-agent mentions
may enable live SDK execution; pass `--no-live-sdk` when a run must remain
offline. No agent command grants permission to send, publish, or perform an
unapproved external write.

List registered agents:

```bash
.venv/bin/python -m keystone_agents.cli agents list
```

Route or run a natural-language request:

```bash
.venv/bin/python scripts/ask_agent.py @KNI orchestrator agent "find 5 behavioral health AI companies"
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health"
```

Advance a stateful WorkItem:

```bash
.venv/bin/python -m keystone_agents.cli work-items advance \
  --input "research NeuroFlow" \
  --database-url sqlite:////tmp/keystone_agents.db

.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "continue" \
  --database-url sqlite:////tmp/keystone_agents.db
```

Run a graph-backed WorkItem diagnostic:

```bash
.venv/bin/python -m keystone_agents.cli work-items advance <work_item_id> \
  --input "continue" \
  --database-url sqlite:////tmp/keystone_agents.db \
  --langgraph \
  --json
```

## Safety And Side Effects

The repo is designed for reviewable business operations, not autonomous external
action.

- No external email is sent automatically.
- Outreach remains draft-only until a human approval path explicitly permits
  the next action.
- Live integrations require credentials plus explicit live flags.
- Gmail live mode is limited to scoped reads, labels, and draft creation.
- Slack posting, Gmail drafts, Airtable writes, Google Workspace writes, Zotero
  importer writes, and future CRM actions are separate approval-gated surfaces.
- PHI and patient-specific information are blocked.
- Medical, legal, tax, security, contractual, and regulatory content should be
  flagged for human review.
- Logs, traces, tests, fixtures, and docs must not expose secrets or private
  customer identifiers.

See `AGENTS.md` for the full repo-local agent contract and development
guardrails.

## Validation

Run the standard local gate:

```bash
.venv/bin/python scripts/scan_repo_secrets.py
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

For the broader quality gate:

```bash
.venv/bin/python scripts/check_quality.py
```

Before publishing local changes to GitHub, use `docs/GITHUB_UPDATE_RUNBOOK.md`.

## Project Map

- `src/keystone_agents/`: agent builders, prompts, schemas, tools, storage,
  WorkItems, orchestration, and CLI runtime.
- Canonical implementation subpackages under `src/keystone_agents/` coexist
  with supported top-level compatibility facades; see
  [Architecture internals](docs/ARCHITECTURE_REORGANIZATION.md) for ownership
  and migration status.
- `src/keystone_agents/prompts/`: markdown prompt surfaces loaded by agent
  builders.
- `src/keystone_agents/skills/`: repo-local runtime skills used inside agent
  instructions.
- `codex-skills/`: repo-local Codex-facing skills for recurring implementation,
  eval, search-provider, WorkItem, and live-SDK workflows.
- `contracts/`: Slack/business-agent contract files and rendering examples.
- `docs/`: architecture, runbooks, integration notes, eval docs, and operator
  references.
- `evals/` and `promptfoo/`: static/local/provider eval cases and promptfoo
  coverage.
- `scripts/`: operator scripts, diagnostics, validation helpers, and focused
  agent entrypoints.
- `tests/`: fixture-heavy unit and integration coverage for agents, tools,
  WorkItems, safety gates, Slack contracts, and LangGraph paths.

## Documentation

Start here for deeper details:

- `docs/INDEX.md`: documentation map.
- `docs/RUNBOOK.md`: local operator checklist.
- `docs/DEPLOYMENT.md`: setup, health checks, live integration flags, rollback,
  audit review, and cost controls.
- `docs/LANGGRAPH_OPTION.md`: graph runtime position, node contract, backend
  selection, diagnostics, and validation.
- `docs/ARCHITECTURE_REORGANIZATION.md`: canonical implementation ownership,
  compatibility facades, migration status, and architecture validation.
- `docs/SLACK_BUSINESS_AGENT_MODE.md`: Slack bridge configuration, scopes, and
  approval boundaries.
- `docs/AGENTS_SDK_CONFORMANCE.md`: how the local folder layout maps to Agents
  SDK concepts.
- `docs/OPENAI_AGENT_PLATFORM_SETUP.md`: OpenAI project setup, tracing,
  datasets, evals, and model migration notes.
- `docs/GITHUB_UPDATE_RUNBOOK.md`: repeatable local-to-GitHub publish flow.

Reference repositories used for architecture review are listed in the docs; no
reference code is copied into this repository.
