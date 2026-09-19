# Keystone Skills Architecture

Keystone skills are repo-local capability contracts under
`src/keystone_agents/skills/<skill_id>/SKILL.md`. They guide agent reasoning and
structured output, but they do not attach tools, grant permissions, select
hidden routes, execute code, or replace deterministic backend gates.

## Layers

- Agents own role, workflow responsibility, output schema, tools, guardrails,
  handoff descriptions, and model/runtime configuration.
- Skills define reusable reasoning and output behavior with purpose,
  applicable agents, inputs, required behavior, flexible behavior, boundaries,
  output contract, failure modes, and eval criteria.
- Tools remain executable function/API/action wrappers under
  `src/keystone_agents/tools/` and must be attached explicitly in builders.
- Backend gates enforce approvals, no-send rules, source sufficiency, write
  scope, live flags, and schema validation.

## Runtime Selection

`AgentSpec.skills` and agent cards expose the full skill catalog an agent is
allowed to use. Runtime instructions use `select_agent_skill_names()` to load a
smaller deterministic subset:

- core safety and quality contracts are always visible;
- the agent's specialist contract is always visible;
- route-default shared contracts cover the agent's normal responsibilities;
- request text and context flags add optional contracts such as source
  attribution, artifact governance, duplicate checking, lifecycle tracking, and
  handoff packaging.

This mirrors the OpenAI Skills progressive-disclosure pattern without adopting
hosted shell skills for live Keystone business integrations. The selector only
chooses which prompt contracts are visible. It does not decide tool calls,
grant write permission, approve external use, or replace Python gates.

OpenAI's current Skills guidance treats skills as reusable workflow bundles with
a required `SKILL.md`, optional adjacent files, and progressive disclosure from
name/description/path into the full skill body. Keystone follows the same
packaging idea, but keeps live business actions in explicit SDK tools and
backend gates instead of executable skill scripts.

## Shared Skills

Shared skills can be selected before each specialist contract:

- `ask_to_target_resolution`
- `artifact_evidence_handling`
- `data_schema_mapping`
- `identity_and_record_resolution`
- `prior_work_and_duplicate_checking`
- `evidence_attribution_and_claim_mapping`
- `source_triage_decision`
- `context_permission_gating`
- `action_boundary_enforcement`
- `unsupported_claim_and_gap_handling`
- `tool_result_resilience`
- `structured_output_quality_review`
- `workspace_artifact_governance`
- `workflow_lifecycle_tracking`
- `handoff_contract_packaging`
- `writing_style_adaptation`
- `request_to_specialist_brief` (Orchestrator route-default skill)

## Specialist Contracts

Each registered agent also declares one specialist contract bundle:

- Gmail Triage: `gmail_triage_specialist_contracts`
- Business Research Analyst: `business_research_specialist_contracts`
- RAG Retrieval Specialist: `rag_retrieval_specialist_contracts`
- Opportunity Scout: `opportunity_scout_specialist_contracts`
- Outreach Composer: `outreach_composer_specialist_contracts`
- Airtable Context: `airtable_context_specialist_contracts`
- Google Workspace Context: `google_workspace_context_specialist_contracts`
- Zotero Context: `zotero_context_specialist_contracts`
- RSS Context: `rss_context_specialist_contracts`
- Preprints Context: `preprints_context_specialist_contracts`
- Orchestrator: `orchestrator_specialist_contracts`
- Chief of Staff: `chief_of_staff_specialist_contracts`

The canonical full catalog lives in `src/keystone_agents/skill_sets.py` and is
mirrored through `AgentSpec.skills` for agent cards and CLI inspection.
The current audited disposition for each Codex-facing and runtime skill lives in
`docs/SKILL_INVENTORY.md`.

## Design Rules

- Use `must` for safety, schema, source, permission, and action boundaries.
- Use `should` for preferred reasoning and output patterns.
- Use `may` for flexible strategies, source choices, wording, and fallback paths.
- Do not encode hard thresholds or fixed source recipes unless Python gates or
  schemas enforce them elsewhere.
- Do not let a skill name imply a tool call, write permission, approval, or live
  integration.

## Validation

Skill tests should prove:

- Every declared skill has metadata and a `SKILL.md`.
- Every registered agent declares the skills it composes.
- Skills include required contract sections.
- Skill metadata references evals or validation paths.
- Default agent instructions include the selected runtime skill subset and no
  longer depend on the monolithic compatibility `skills.md`.
- Request-specific builder tests prove relevant optional skills are visible
  when the request calls for them.

The focused local eval slice is `evals/local/skill_contracts.jsonl`. It covers
shared contracts and specialist bundles with cases for possible matches,
unsupported claims, pending-context outreach, tool failure fallback, lifecycle
state, handoff preservation, and flexible safe partial answers.

`evals/local/skill_task_matrix.jsonl` is the dry-run operator-surface matrix.
Run it with:

```bash
.venv/bin/python scripts/run_skill_task_matrix.py --json
.venv/bin/python scripts/run_skill_task_matrix.py --surface slack --json
.venv/bin/python scripts/run_skill_task_matrix.py --surface computer --json
```

Those cases test one agent at a time across Slack and Computer-style requests.
They prove that the expected common and specialist skills are selected and
visible in the composed instructions, while unrelated specialist skills remain
absent. The matrix is intentionally no-side-effect: it does not call Slack,
control the desktop, run live models, or write external artifacts.

The matrix is also included in the default local eval suite:

```bash
.venv/bin/python scripts/run_local_evals.py --dataset skill_task_matrix --json
```

Each result includes `selection_reasons`, so a failure can be traced to a core
skill, specialist contract, route default, request trigger, or explicit context
flag before changing prompt text.

Live and saved WorkItem runs also record a `skills_selected` audit event before
the specialist step executes. That event stores the selected skill IDs,
selection reasons, agent name, route, and selector input hash so Slack runs can
be audited without reconstructing the prompt after the fact.

After the routed specialist step, saved WorkItems also record
`skill_contract_gates_checked`. This event stores deterministic pass, limited,
or blocked checks for the route's hard skill-backed gate:

- Business Research: `business_research_claim_gate`
- Outreach Composer: `outreach_approval_claim_gate`
- Gmail Triage: `gmail_sensitive_message_gate`
- Chief of Staff: `chief_artifact_publish_gate`

These gates do not replace agent reasoning. They verify that high-risk skill
contracts degraded into blockers or limitations instead of crashes, fabricated
claims, sends, posts, or unsafe external writes.

Behavior-level skill coverage is layered onto ordinary local eval cases with
`validates_skills` and `surface`. These cases prove selected common and
specialist skills are not only visible but associated with passing fixture
behavior. Initial coverage is in:

- `evals/local/skill_gate_failures.jsonl` for deterministic gate degradation
  into `blocked` or `limited` outcomes instead of crashes or unsafe side
  effects.
- `evals/local/gmail_triage.jsonl` for suspicious-message blocking, legal-risk
  draft boundaries, and thread-state handling.
- `evals/local/outreach_copy_constraints.jsonl` for unsupported-claim removal,
  approval-gated follow-up lifecycle records, and Computer-style call-prep
  artifact behavior.
- `evals/local/source_attribution.jsonl` for Business Research Analyst and
  Opportunity Scout source-backed records, stale-source confidence handling,
  and duplicate company deconfliction.
- `evals/local/opportunity_scoring.jsonl` for Opportunity Scout fit, timing,
  weak-signal, lifecycle, and handoff decisions.
- `evals/local/orchestrator_routing.jsonl` for Orchestrator approval gates,
  workflow-state handling, and specialist handoff decisions.
- `evals/local/chief_of_staff.jsonl` for Chief of Staff Slack workflow review,
  Computer-style internal report planning, source use, and no-post/no-write
  boundaries.

## Backlog

Detailed follow-up work lives in `docs/SKILL_UPDATE_BACKLOG.md`.

- Add deeper cross-agent behavior graders that compare runs with different
  selected skill subsets once the live/fake SDK harness can safely vary runtime
  instructions without changing tools or backend gates.
- Add live optional smoke tests for Slack-triggered dry-run WorkItems after the
  Slack bridge exposes a safe test channel fixture. These should verify route,
  skill-selection trace, no-send state, and rendered Slack summary only.
- Add Computer-use artifact review cases after there is a scoped local workspace
  fixture and no desktop mutation requirement. These should verify draft
  artifacts land under `artifacts/`, preserve source/approval state, and never
  invoke user-screen browser actions.
- Track per-skill pass/fail history in the benchmark store alongside prompt and
  model metadata.
