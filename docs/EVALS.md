# Local Evals

Keystone evals are offline and deterministic. They do not call OpenAI model APIs,
OpenAI Evals API, Gmail, Slack, Serper, SearXNG, Apify, Browserless, or any other live
service.

Current verification from this implementation pass:

- `.venv/bin/python -m pytest`: 685 passed.
- `.venv/bin/python -m ruff check .`: all checks passed.
- `.venv/bin/python scripts/run_evals.py --agent all --json`: 22 passed.
- `.venv/bin/python scripts/run_local_evals.py --json`: 39 passed.

## Eval Surfaces

Static specialist evals:

```bash
.venv/bin/python scripts/run_evals.py --agent all --markdown
.venv/bin/python scripts/run_evals.py --agent gmail --json
.venv/bin/python scripts/run_evals.py --agent company --markdown
.venv/bin/python scripts/run_evals.py --agent scout --markdown
.venv/bin/python scripts/run_evals.py --agent outreach --markdown
```

These use JSON datasets in `tests/evals/`:

- `gmail_triage_cases.json`
- `business_research_analyst_cases.json`
- `opportunity_scout_cases.json`
- `outreach_composer_cases.json`

JSONL local evals:

```bash
.venv/bin/python scripts/run_local_evals.py
.venv/bin/python scripts/run_local_evals.py --dataset gmail_triage
.venv/bin/python scripts/run_local_evals.py --json
```

These use seed datasets in `evals/` and include prompt version traceability.

Both commands exit with code `1` when any case fails.

## What They Check

Current deterministic graders cover:

- Gmail category, reply need, labels, risk flags, draft quality, and no-auto-send gates.
- Company profile source attribution, fit scoring, hallucination checks, risks, and missing
  information.
- Opportunity ranking, component scoring, source preservation, why-now signals, and handoff
  thresholds.
- Outreach copy length, tone, no em dashes, no unsupported claims, approval state, and send
  disabled outputs.
- Orchestrator routing and safety refusals.
- Prompt metadata and prompt version references.

## Dataset Shape

Static `tests/evals/*.json` rows use this shape:

```json
{
  "id": "stable_case_id",
  "agent": "gmail",
  "input": {},
  "expected": {}
}
```

JSONL `evals/*.jsonl` rows use this shape:

```json
{
  "id": "stable_case_id",
  "task": "gmail_triage",
  "validates_prompts": ["keystone_profile@2026-04-21.1", "gmail_triage@2026-04-21.1"],
  "input": {},
  "expected": {}
}
```

Prompt versions come from metadata blocks at the top of
`src/keystone_agents/prompts/*.md`. Eval JSON output includes `prompt_metadata` and
`prompt_versions` so a result can be traced from agent to prompt version to dataset row.

## Local Graders

The eval harnesses call fixture-mode agent functions and schemas instead of duplicating live
agent behavior:

- Gmail rows use `triage_email_fixture`.
- Company rows use `research_company_fixture`.
- Scout rows use `scout_opportunities_fixture` and `score_opportunity_impl`.
- Outreach rows use `compose_outreach_draft_fixture` and `check_unsupported_claims`.
- Orchestrator rows use `route_request`.
- Safety rows use deterministic guardrail checks.

This keeps CI offline while still checking user-facing contracts: no auto-send behavior,
approval gates, source attribution, safety refusals, scoring quality, and copy constraints.

## Improvement Test Pack

Use `docs/AGENT_IMPROVEMENT_TEST_PACK.md` as the iteration backlog for agent quality
gaps that are broader than the current automated eval suite. The pack organizes future
Opportunity Scout, Researcher/company research, Gmail Triage, Outreach Composer, and cross-agent
collaboration cases.

When converting a pack item into executable coverage:

1. Add a deterministic fixture or mocked tool path first.
2. Add the smallest useful pytest, static eval, or local JSONL eval.
3. Mark the case complete only after it has passing automated coverage or a documented
   manual validation path.
4. Keep live-provider checks separate from offline evals.

## Prompt Traceability

Use these commands to inspect prompt coverage:

```bash
.venv/bin/python scripts/run_evals.py --agent all --json
.venv/bin/python scripts/run_local_evals.py --json
```

When a prompt changes:

1. Increment its prompt version metadata.
2. Update its eval dataset references if coverage changes.
3. Update affected JSONL `validates_prompts` values.
4. Run pytest, ruff, and the relevant eval command.

## Future Hosted Evals

OpenAI-hosted evals are not implemented. If added later, they must be explicit live
operations with reviewed datasets, no PHI, no secrets, and no private customer data.

Keep local evals as the canonical seed set. Hosted evals should preserve row IDs and
`input` / `expected` fields, and safety-critical checks should remain available locally.
