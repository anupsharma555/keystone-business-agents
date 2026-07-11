# KBA Skill Inventory

Review date: 2026-07-06. Issue: ANU-171.

This inventory covers the repo-local Codex skills under `codex-skills/`, the
runtime agent skills under `src/keystone_agents/skills/`, and the skill metadata
surface exposed through `src/keystone_agents/skill_sets.py` and
`src/keystone_agents/agent_registry.py`.

## Review Basis

Current KBA execution is Orchestrator-first for natural-language work:
Orchestrator preflight preserves raw operator wording, Python applies
deterministic gates, specialists receive typed context packs, WorkItems remain
the canonical business state, optional LangGraph orchestration wraps WorkItem
advancement, and renderers own Slack/CLI/report presentation.

Skills remain prompt-visible reasoning and output contracts only. They do not
grant tools, approve writes, select hidden routes, create live credentials, or
bypass source, approval, cost, dry-run, no-send, or no-write gates.

## Disposition Legend

- Keep: current contract is aligned and should remain as-is.
- Update now: stale wording was patched in this review.
- Split candidate: keep now, but split later only if evals show repeated
  reusable behavior that is too large for one skill.
- Merge candidate: keep now, but merge later only if two skills repeatedly
  select and validate as the same behavior.
- Deprecate candidate: keep now only if compatibility requires it; remove after
  registry, selector, and eval coverage no longer reference it.

## Repo-Local Codex Skills

| Skill | Disposition | Notes |
| --- | --- | --- |
| `kba-agent-contract-change` | Update now | Kept as the contract-change entrypoint; refreshed reference map for LangGraph/WorkItem ownership and route-boundary checks. |
| `kba-eval-readiness-triage` | Keep | Still owns Promptfoo, Slack eval readiness, trace summaries, route compaction, and no-live SDK eval safety. |
| `kba-live-sdk-smoke-and-cost` | Keep | Still owns `KEYSTONE_OPENAI_API_KEY`, live SDK smoke, rate-limit, trace, request-cache, and cost telemetry guidance. |
| `kba-new-agent` | Update now | Kept as the novel-agent workflow; refreshed to include optional LangGraph backend policy while preserving Orchestrator-first execution. |
| `kba-search-provider-eval` | Keep | Still owns live-search, provider budgets, KBA SearXNG `18080`, extraction, attribution, and no-side-effect live eval rules. |
| `kba-workitem-orchestrator-ops` | Update now | Kept as the WorkItem/Orchestrator operations workflow; refreshed to name LangGraph as optional orchestration around WorkItems, not canonical business state. |

## Runtime Agent Skills

| Skill | Disposition | Notes |
| --- | --- | --- |
| `action_boundary_enforcement` | Keep | Current no-send/no-write boundary contract remains aligned with approval gates and live tool policy. |
| `airtable_context_specialist_contracts` | Keep | Context-agent write language is bounded by direct selection, exact identity, approval references, and live-write gates; nested Chief calls remain advisory. |
| `artifact_evidence_handling` | Keep | Correctly keeps local files and artifacts as bounded evidence, not permission to send, publish, upload, or share. |
| `ask_to_target_resolution` | Keep | Current target-resolution contract is useful for diverse asks and avoids brittle one-off routes. |
| `business_research_specialist_contracts` | Keep | Current source strategy, identity validation, evidence synthesis, and fit-assessment contract remains aligned. |
| `chief_of_staff_specialist_contracts` | Update now | Refreshed to mention WorkItem/LangGraph runtime diagnostics and the internal-only/no-post boundary. |
| `context_permission_gating` | Keep | Current context eligibility and audience/scope checks remain aligned. |
| `data_schema_mapping` | Keep | Current schema-read, model-map, helper-validate, bounded-write contract remains aligned. |
| `evidence_attribution_and_claim_mapping` | Keep | Current claim/source mapping contract preserves source attribution and uncertainty. |
| `gmail_triage_specialist_contracts` | Keep | Current Gmail triage contract remains draft-only and approval-gated. |
| `google_workspace_context_specialist_contracts` | Keep | Workspace writes are bounded by direct selection, exact identity, approval/live gates, and verified marked-Sheet lifecycle cleanup; nested Chief calls remain advisory. |
| `handoff_contract_packaging` | Keep | Current compact handoff contract preserves source, approval, and blocker state. |
| `identity_and_record_resolution` | Keep | Current entity/record matching contract prevents silent merges. |
| `opportunity_scout_specialist_contracts` | Keep | Current opportunity discovery contract avoids outreach generation and requires validation. |
| `orchestrator_specialist_contracts` | Update now | Refreshed to name WorkItems/context packs and optional LangGraph manager flow without letting route advice bypass Python gates. |
| `outreach_composer_specialist_contracts` | Keep | Current outreach drafting contract remains draft-only, source-backed, and approval-gated. |
| `preprints_context_specialist_contracts` | Keep | Current preprint context contract supports the allowlisted linked discovery-store read in SQLite read-only mode and blocks unsupported claims. |
| `prior_work_and_duplicate_checking` | Keep | Current prior-work contract avoids brittle duplicate blockers. |
| `request_to_specialist_brief` | Keep | Current brief-expansion contract preserves raw request, route authority, and permission gates. |
| `rss_context_specialist_contracts` | Keep | Current RSS context contract supports the explicitly live-gated structured Slack history read while blocking posting, modification, and browser-scraping overreach. |
| `source_triage_decision` | Keep | Current source-selection contract aligns with live-search, extraction, source gaps, and cost-aware deepen/broaden behavior. |
| `structured_output_quality_review` | Keep | Current final-output review contract preserves schema, source, approval, and action-state checks. |
| `tool_result_resilience` | Keep | Current provider/tool failure contract preserves diagnostics without unsafe inference. |
| `unsupported_claim_and_gap_handling` | Keep | Current gap-handling contract prevents fabricated claims. |
| `workflow_lifecycle_tracking` | Update now | Refreshed applicable-agent text to match metadata and current WorkItem lifecycle scope. |
| `workspace_artifact_governance` | Keep | Current internal artifact governance preserves approval, source, and no external-write boundaries. |
| `writing_style_adaptation` | Keep | Current writing-style contract stays draft-only and does not add unsupported facts. |
| `zotero_context_specialist_contracts` | Keep | Zotero context/importer contract blocks ordinary native mutation, permits only versioned `KBA_TEST_NOTE` lifecycles, and keeps nested Chief calls advisory. |

## Agent Coverage Check

The reviewed specialist/context agents are represented in `AGENT_SKILL_NAMES`
and `AgentSpec.skills`: Chief of Staff, Business Research Analyst, Opportunity
Scout, Outreach Composer, Gmail Triage, Airtable Context, Google Workspace
Context, Zotero Context, RSS Context, Preprints Context, and Orchestrator.

AgentSpec skill metadata remains the registry source for agent cards and CLI
inspection. If a future skill contract changes selection behavior, update
`src/keystone_agents/skill_sets.py`, `src/keystone_agents/agent_registry.py`,
`evals/local/skill_task_matrix.jsonl`, and focused prompt/registry tests in the
same change.

## Live-Smoke Implications

This review did not require broad live SDK or live-provider tests. Future live
smoke work should stay serial and bounded, use `KEYSTONE_OPENAI_API_KEY` for
KBA SDK calls, preserve dry-run fixture coverage first, and record rate-limit,
trace, request-cache, and cost telemetry when claiming live behavior was
measured.
