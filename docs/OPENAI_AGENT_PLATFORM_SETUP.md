# OpenAI Agent Platform Setup

This runbook describes how to use OpenAI's developer platform around this repo
without changing production behavior. The code-first OpenAI Agents SDK
implementation in this repository remains the source of truth for Keystone
agents, prompts, tools, guardrails, structured outputs, and approval gates.

## Scope

Use the OpenAI platform for:

- Project and API key administration.
- Local live-model credentials when explicitly needed.
- Tracing and trace inspection for SDK runs.
- Trace grading, datasets, and evals after behavior is stable enough to score.
- Future Agent Builder exploration and prototyping.

Do not use the platform to bypass repo behavior:

- Do not replace `build_*_agent()` builders with dashboard-only workflows.
- Do not paste real secrets, account IDs, project IDs, user IDs, trace IDs, or
  screenshots containing private account data into docs.
- Do not add live outbound messaging behavior from the platform. This repo
  remains draft-only unless a future implementation explicitly adds tested
  approval and audit controls.

## Current Repo Contract

- `KEYSTONE_OPENAI_MODEL` defaults to `gpt-5.4-mini`; the Orchestrator, Account
  Researcher, and Opportunity Scout default to OpenAI `gpt-5.4-mini` unless
  their `KEYSTONE_*_MODEL` values are set.
- Gmail Triage and Outreach Composer default to `gemini-2.5-flash` through the
  Gemini provider path and require `GEMINI_API_KEY` for live SDK execution.
  Keystone uses Google's direct OpenAI-compatible Gemini endpoint by default and
  sets `use_responses=false` so the Agents SDK uses Chat Completions
  compatibility instead of requiring `/responses`.
- `MODEL_PROVIDER` defaults to `openai`.
- Runtime agents can override provider, model, and base URL with
  `KEYSTONE_*_MODEL_PROVIDER`, `KEYSTONE_*_MODEL`, and `KEYSTONE_*_BASE_URL`.
  Gmail Triage and Outreach Composer may override the default direct Gemini
  endpoint through an explicit OpenAI-compatible gateway such as LiteLLM. Account
  Researcher, Opportunity Scout, and Orchestrator should remain OpenAI-compatible
  unless a reviewed change says otherwise.
- Dry-run and fixture-mode development remain the default.
- Agent construction does not require `KEYSTONE_OPENAI_API_KEY`.
- Live model execution requires `KEYSTONE_OPENAI_API_KEY` and explicit live
  execution paths.
- Live Gemini execution requires `GEMINI_API_KEY`. The repo uses Gemini's
  direct OpenAI-compatible endpoint through the existing OpenAI Agents SDK
  provider path and does not add a separate Gemini SDK dependency.
- LiteLLM is supported as an external gateway through `LITELLM_BASE_URL` or an
  agent-specific `KEYSTONE_*_BASE_URL`. The Keystone runtime does not require or
  import the Python `litellm` package; keep the proxy in a separate environment
  unless its OpenAI Python dependency range matches the Agents SDK baseline.
- Local fake-model tests use `tracing_disabled=True` by design.

Keep these defaults unless a separate code change updates tests, docs, and the
model migration checklist below.

## Dashboard Setup

1. Sign in to the OpenAI API dashboard.
2. Create or select a project dedicated to Keystone local development or
   staging validation.
3. Keep production, staging, and local development projects separate when
   possible.
4. Review project members, service accounts, rate limits, and spend limits.
5. Use the project for API keys, trace logs, datasets, and eval runs related to
   this repo.

Do not document dashboard identifiers. Use names like `keystone-local-dev` in
docs and issue comments instead of real project IDs.

## Project And Key Handling

Recommended key practice:

- Use a project-scoped key for local live-model validation.
- Prefer separate keys for local development, CI experiments, and staging.
- Give keys descriptive non-secret names in the dashboard.
- Rotate keys after sharing mistakes, suspicious logs, or contractor turnover.
- Delete unused keys.
- Never commit `.env`, copied dashboard JSON, screenshots showing key fragments,
  or terminal output containing secrets.

Keystone reads `KEYSTONE_OPENAI_API_KEY` for OpenAI live model execution and
intentionally ignores generic `OPENAI_API_KEY`, `OPENAI_MODEL`, and
`OPENAI_BASE_URL` shell variables. This allows other projects to keep using
those generic variables without affecting Keystone. This repo reads
`KEYSTONE_OPENAI_MODEL`, `KEYSTONE_OPENAI_BASE_URL`, and `LITELLM_BASE_URL`
through the central model provider configuration. Gemini direct execution does
not require `LITELLM_BASE_URL`; set it only to point at a running external
LiteLLM proxy or compatible gateway. It is not a signal to install the `litellm`
Python package inside Keystone.

## Local `.env` Setup

Copy the template and keep real values local:

```bash
cp .env.example .env
```

Minimum OpenAI live-model settings:

```dotenv
KEYSTONE_LIVE_MODE=false
MODEL_PROVIDER=openai
KEYSTONE_OPENAI_MODEL=gpt-5.4-mini
KEYSTONE_AGENT_RUN_BUDGET_USD=0.25
KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER=gemini
KEYSTONE_GMAIL_TRIAGE_MODEL=gemini-2.5-flash
KEYSTONE_GMAIL_TRIAGE_BASE_URL=
KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL=gpt-5.4-mini
KEYSTONE_OPPORTUNITY_SCOUT_MODEL=gpt-5.4-mini
KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER=gemini
KEYSTONE_OUTREACH_COMPOSER_MODEL=gemini-2.5-flash
KEYSTONE_OPENAI_API_KEY=
KEYSTONE_OPENAI_BASE_URL=
LITELLM_BASE_URL=
GEMINI_API_KEY=

KEYSTONE_ORCHESTRATOR_MODEL_PROVIDER=openai
KEYSTONE_ORCHESTRATOR_MODEL=gpt-5.4-mini
KEYSTONE_AGENT_RUN_BUDGET_USD=0.25
KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER=
KEYSTONE_GMAIL_TRIAGE_MODEL=
KEYSTONE_GMAIL_TRIAGE_BASE_URL=
KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL_PROVIDER=openai
KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL=gpt-5.4-mini
KEYSTONE_OPPORTUNITY_SCOUT_MODEL_PROVIDER=openai
KEYSTONE_OPPORTUNITY_SCOUT_MODEL=gpt-5.4-mini
KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER=gemini
KEYSTONE_OUTREACH_COMPOSER_MODEL=gemini-2.5-flash
KEYSTONE_OUTREACH_COMPOSER_BASE_URL=
```

Set `KEYSTONE_OPENAI_API_KEY` only in local `.env` or your shell environment.
Leave it blank in examples, fixtures, tests, and committed docs.

Sanity check the effective model configuration without making a network call:

```bash
.venv/bin/python - <<'PY'
from keystone_agents.model_provider import get_model_config

print(get_model_config().as_log_dict())
PY
```

Expected properties:

- The global OpenAI model is `gpt-5.4-mini` unless overridden. The Orchestrator,
  Business Research Analyst, and Opportunity Scout use `gpt-5.4-mini` unless
  overridden. Gmail Triage and Outreach Composer use `gemini-2.5-flash` through
  Google's direct OpenAI-compatible endpoint unless overridden.
- `api_key` is `[masked]` when present.
- No raw API key is printed.

## Live SDK Runs

Run tests and dry-run scripts first. Only use live model execution for a narrow
validation target after prompts, tools, schemas, and guardrails pass locally.

Before a live SDK run:

- Confirm `KEYSTONE_OPENAI_MODEL=gpt-5.4-mini`,
  `KEYSTONE_ORCHESTRATOR_MODEL=gpt-5.4-mini`, and confirm any Account
  Researcher or Opportunity Scout override only when intentionally testing a
  different model.
- Confirm any per-agent provider override is intentional. For Gmail Triage and
  Outreach Composer Gemini runs, confirm `GEMINI_API_KEY` is set.
  `KEYSTONE_GMAIL_TRIAGE_BASE_URL`, `KEYSTONE_OUTREACH_COMPOSER_BASE_URL`, or
  `LITELLM_BASE_URL` is optional and should be set only when intentionally
  routing through a reviewed gateway.
- Confirm the input does not contain PHI or patient-specific information.
- Confirm any outbound copy remains draft-only and requires human approval.
- Confirm live integrations use their explicit flags.
- Keep sample inputs small and non-sensitive.

Do not add dashboard-created workflow IDs to code paths unless a future feature
explicitly introduces Agent Builder or ChatKit integration.

## Tracing Dashboard

Agents SDK tracing is intended for live SDK observability. Use it to inspect
model calls, tool calls, handoffs, guardrails, and workflow spans.

Process:

1. Run one narrow live SDK workflow with tracing enabled through the normal SDK
   path.
2. Open the OpenAI dashboard and navigate to Logs > Traces.
3. Inspect only non-sensitive traces.
4. If a trace shows unexpected behavior, identify whether the root cause is a
   prompt, schema, tool, guardrail, routing, or input issue.
5. Add or update a local regression test before relying on the live path again.

Do not paste trace screenshots into this repo if they show private account
metadata, customer data, prompts containing sensitive context, API keys, or
dashboard identifiers.

## Trace Grading

Use trace grading after representative traces exist and the behavior you want
to score is clear.

Good first graders for this repo:

- Correct specialist or handoff chosen.
- No PHI or patient-specific content processed.
- Draft-only behavior preserved.
- Sources included for company or opportunity research.
- No unsupported Keystone capability or outcome claims.
- Tool calls stayed inside allowed live/dry-run boundaries.

Suggested workflow:

1. Select a small set of representative traces.
2. Define one grader per safety or quality dimension.
3. Run graders against the traces.
4. Treat failures as prompts for code, prompt, schema, or test changes.
5. Port durable checks back into pytest when practical.

Trace grading complements this repo's tests. It does not replace tests or the
code-first SDK implementation.

## Datasets And Evals

Use datasets once examples are stable enough to repeat. Start with local,
non-sensitive examples that mirror existing fixtures.

Candidate dataset columns:

- `input_kind`: email, company, opportunity, outreach, orchestrator.
- `input_text`: sanitized request or fixture-derived text.
- `expected_route`: intended specialist or workflow step.
- `expected_safety_flags`: expected safety labels.
- `expected_draft_only`: true or false.
- `expected_source_required`: true or false.

Recommended progression:

1. Convert a small subset of fixture-style examples into a dashboard dataset.
2. Add graders that map to existing safety and architecture rules.
3. Compare model or prompt variants only after the local test suite passes.
4. Export or mirror durable cases back into repo fixtures when they catch a real
   regression.
5. Use Evals for larger or API-driven evaluation runs when dashboard datasets
   are too limited.

Do not upload proprietary, patient-specific, credential-bearing, or
account-specific data to eval datasets.

## Future Agent Builder Exploration

Agent Builder can be useful for visual prototyping, debugging workflow shape,
and comparing possible orchestration designs. Treat it as an exploration
surface, not the production source of truth.

Allowed exploration:

- Recreate a simplified workflow using sanitized fixture inputs.
- Compare routing or handoff concepts.
- Inspect trace behavior from visual prototypes.
- Export SDK code for review.

Guardrails:

- Do not replace repo agent builders with unpublished dashboard workflows.
- Do not commit workflow IDs, account IDs, screenshots with private metadata, or
  generated code without review.
- Do not add ChatKit or Agent Builder deployment paths without a separate
  implementation plan, tests, and approval-gate review.
- Any useful prototype behavior must be translated back into repo code, prompt
  files, schemas, tools, and tests.

## Model Migration Checklist

Use this checklist before moving from `gpt-5.4-mini` to a different OpenAI model.

- Confirm the target model is available in the intended OpenAI project.
- Review current OpenAI model docs, pricing, rate limits, context limits, tool
  behavior, structured output support, and tracing behavior.
- Update only `KEYSTONE_OPENAI_MODEL` locally first.
- Run `pytest` and `ruff check .`.
- Run deterministic dry-run scripts for each specialist and the orchestrator.
- Run a small live SDK validation set with non-sensitive examples.
- Inspect traces for tool selection, handoffs, guardrails, and output schema
  adherence.
- Run trace graders or dataset evals against the before/after examples.
- Check safety invariants: PHI rejection, draft-only outbound copy, source
  attribution, and unsupported-claim handling.
- Update `.env.example`, README, and this runbook only after the migration is
  accepted.
- Keep the rollback path simple: set `KEYSTONE_OPENAI_MODEL=gpt-5.4-mini`.

## Verification

Docs-only changes should still pass the normal repo gates:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

If future docs tests or markdown linting are added, run those as part of this
runbook update path.

## Official References

- OpenAI developer quickstart: https://developers.openai.com/api/docs/quickstart
- OpenAI Agents SDK overview: https://developers.openai.com/api/docs/guides/agents
- Agents SDK integrations and observability: https://developers.openai.com/api/docs/guides/agents/integrations-observability
- Agent workflow evaluation: https://developers.openai.com/api/docs/guides/agent-evals
- Trace grading: https://developers.openai.com/api/docs/guides/trace-grading
- Getting started with datasets: https://developers.openai.com/api/docs/guides/evaluation-getting-started
- Agent Builder: https://developers.openai.com/api/docs/guides/agent-builder
- Project API keys: https://developers.openai.com/api/reference/resources/organization/subresources/projects/subresources/api_keys
