---
name: kba-live-sdk-smoke-and-cost
description: Repo-local workflow for <repo>. Use only in this repo when running or debugging live SDK smoke tests, model/provider configuration, KEYSTONE_OPENAI_API_KEY handling, SDK sessions, trace metadata, request-cache/cost telemetry, rate limits, budget guards, or no-side-effect live validation.
---

# KBA Live SDK Smoke And Cost

## Scope

Use this skill only when the current repository is
`<repo>`. If the working directory is
not this repo, stop and do not apply these instructions.

Default to fixture/dry-run validation. Run live SDK calls only when the user
explicitly asks for live model execution or the task is specifically about live
SDK smoke/cost behavior.

## First Reads

Read the smallest set that matches the task:

- Runtime model/key policy: `AGENTS.md`, `README.md`,
  `src/keystone_agents/model_provider.py`, `src/keystone_agents/sdk.py`.
- Live SDK sessions and state distinction:
  `src/keystone_agents/sdk_sessions.py`, `src/keystone_agents/run.py`.
- Cost/cache/budget telemetry: `src/keystone_agents/costing.py`,
  `src/keystone_agents/cost_tracking.py`,
  `docs/AGENT_COST_CACHE_IMPLEMENTATION_QUEUE.md`.
- Trace summaries: `src/keystone_agents/trace_processor.py`,
  `tests/test_trace_processor.py`.
- Live smoke command map and failure anchors: `references/live-sdk-flow.md`.

## Request Shapes

Use this workflow for requests like:

- "Run a live SDK smoke test", "why is the live model call failing?", or "check
  the configured model/provider".
- "Use the repo OpenAI key", "map `KEYSTONE_OPENAI_API_KEY` for this raw SDK
  snippet", or "avoid the wrong OpenAI project".
- "Diagnose rate limits", "reduce prompt/cost for this smoke run", or "compare
  WorkItem cost".
- "Inspect usage, trace, request cache, budget guard, or `workflow_sdk_usage`
  metadata".

## Workflow

1. Verify the user requested live model behavior. If not, use fixture or dry-run
   tests instead.
2. Use `KEYSTONE_OPENAI_API_KEY` as this repo's key. If a raw SDK snippet only
   reads `OPENAI_API_KEY`, map the repo key to that variable only in the child
   process and never print the secret.
3. Keep live runs no-side-effect unless the user separately approves a scoped
   write path. Live model execution does not imply Slack posts, Gmail drafts,
   sends, CRM writes, schedules, or publication.
4. Prefer compact prompts, mini/nano approved models, and focused smoke inputs
   before retrying large eval or WorkItem runs.
5. Treat HTTP 429 as provider throttle. Respect retry-after/reset windows and
   avoid hot-loop retries.
6. Inspect usage, cost, request-cache, trace, and WorkItem `workflow_sdk_usage`
   events when claiming a live run was measured.

## Decision Rules

- Live model execution must fail before the model call when credentials or
  gateway configuration are missing.
- Do not rely on generic shell `OPENAI_API_KEY`, `OPENAI_MODEL`, or
  `OPENAI_BASE_URL` values from other projects.
- Do not use `result.to_input_list()`, server-managed continuation IDs, or
  `result.to_state()` as the default business-state mechanism unless the repo
  deliberately changes direction.
- Do not claim exact provider billing cost unless provider-reported or
  calculated from an explicitly maintained pricing table.

## Verification

Use focused validation from `references/live-sdk-flow.md`. For code changes to
live SDK/cost behavior, prefer:

```bash
.venv/bin/python -m pytest tests/test_model_provider.py tests/test_sdk_execution.py tests/test_costing.py
.venv/bin/python -m pytest tests/test_cost_tracking.py tests/test_sdk_sessions.py tests/test_trace_processor.py
```
