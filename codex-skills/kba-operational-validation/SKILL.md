---
name: kba-operational-validation
description: Repo-local validation coordinator for <repo>. Use only in this repo when validating realistic KBA jobs through bounded direct-agent routes, Orchestrator-first WorkItems, optional LangGraph workflows, typed provider evidence, approval gates, OpenAI request and cost caps, backend receipts, and Slack content and visual acceptance.
---

# KBA Operational Validation

Use this skill only for `<repo>`. If the working directory is
not this repo, stop and switch to the correct checkout before continuing.

## First Reads

1. Read the repository `AGENTS.md` and current dirty-worktree state.
2. Read `docs/AGENT_OPERATIONAL_VALIDATION_STATUS.md`, `docs/HUMAN_AGENT_EXECUTION_JOBS.md`, and `docs/OPERATIONAL_VALIDATION_RESUME.md`.
3. Read `references/validation-workflow.md` for the evidence ladder, live-call rules, and checkpoint format.
4. Read the matching companion skill before changing a narrower contract.

Treat the current checkout, provider receipts, and Linear state as authoritative. Keep legacy Promptfoo deferred until current agent and graph paths are operationally validated.

## Request Shapes

- Operational-readiness review, realistic agent smoke test, or Slack acceptance check.
- Direct-versus-WorkItem/LangGraph route diagnosis.
- Sequential live API test planning with request and cost caps.
- Cross-agent provider lifecycle, continuation, mutation, and cleanup validation.
- Operational status or checkpoint documentation after evidence changes.

## Workflow

### Select Companion Skills

- Existing agent, prompt, schema, tool, context-pack, or registry change: `$kba-agent-contract-change`.
- New agent or agent-family design: `$kba-new-agent`, paired with `$kba-agent-contract-change` for implementation.
- `@KNI`, Orchestrator, WorkItem, approval, continuation, or LangGraph issue: `$kba-workitem-orchestrator-ops`.
- Retrieval, extraction, citations, or search-provider issue: `$kba-search-provider-eval`.
- Live model, request budget, usage, rate-limit, or cost issue: `$kba-live-sdk-smoke-and-cost` plus `$codex-general-api-cost-aware-agent-testing`.
- Promptfoo, eval dashboard, trace compaction, or strict `#evals` readiness issue: `$kba-eval-readiness-triage`.

Use this skill as the cross-cutting acceptance coordinator; do not duplicate the companion skills' detailed implementation instructions here.

### Execute One Capability At A Time

1. Convert the human job into explicit acceptance evidence: interpretation, route, typed tool, provider identity, useful output, safety, continuation, and cleanup.
2. Run the smallest relevant offline or provider-only proof first.
3. Fix the first shared defect with a general contract; do not add phrase-specific shortcuts.
4. Add a focused regression and rerun only the affected gate unless a shared routing, schema, graph, Slack bridge, or SDK change justifies the broader no-live gate.
5. Run live-model validation only after offline/provider evidence is green and the user-approved request ceiling permits it.

### Select The Execution Path By Job Shape

- Use a bounded direct route when one specialist owns the job and deterministic preflight, retrieval or typed context, one specialist synthesis, validation, and rendering are sufficient.
- Use the canonical Orchestrator/WorkItem route when the request is ambiguous, stateful, cross-agent, approval-dependent, resumable, or requires review and repair.
- Use LangGraph only as an optional WorkItem backend when the validated plan contains multiple stages, handoffs, checkpoints, or repair loops. Do not select it merely because the user says `graph` or names a control-plane agent.
- Treat explicit `@KNI` agent shorthand as routing advice, not permission to bypass Orchestrator preflight, specialist ownership, Python gates, review, or side-effect controls.
- Expect Chief of Staff jobs to be multistage when they coordinate systems or specialists, but keep one exact supported action, status check, or bounded read lightweight when no durable handoff or checkpoint is needed.
- Validate direct and graph paths separately. A direct specialist pass does not prove WorkItem handoffs, and a graph receipt does not prove useful specialist output.

### Validate The Slack Acceptance Surface

Use backend receipts as primary execution evidence, then validate Slack as the final human-entrypoint acceptance surface for every live Slack test:

1. Read the resulting thread with the Slack connector and verify delivery, correct thread placement, requested content, source URLs, uncertainty, safety language, duplication, truncation, and absence of raw debug metadata.
2. Use Slack desktop/Computer Use for renderer changes, new routes, failures, and sampled live runs to verify visible hierarchy, readability, links, blocks, and error presentation. Do not require brittle GUI inspection for every unchanged routine run.
3. Record separate execution, content, and rendering outcomes. Report the overall result as `PASS`, `PARTIAL`, or `FAIL`; a successful backend process with poor or missing Slack output is not a pass.
4. Preserve the Slack permalink or channel/thread timestamp with the backend run ID when available.

When reporting request consumption, distinguish the configured ceiling, conservative preflight estimate, and actual requests made. A command blocked before execution may have a high estimate while making zero OpenAI requests.

### Preserve Boundaries

- Gmail and Slack sends/posts remain disabled without explicit scoped approval.
- Provider reads and reversible marked test lifecycles are allowed when the user has approved them.
- Keep raw Gmail bodies transient when building style profiles; persist only redacted aggregate profiles.
- Keep live search off unless separately approved.
- Use `KEYSTONE_OPENAI_API_KEY`, never an unrelated generic project key.
- Stop on the first retry, shared failure, missing receipt, unexpected side effect, or approved request/cost ceiling breach. Do not describe an estimator maximum as actual consumption.
- Do not claim a pass when routing, provider execution, reasoning quality, lifecycle evidence, or Slack acceptance is partial.

## Verification

### Record Results

Update local status documents after material evidence changes. Update Linear only for a major pass milestone or blocker; prefer updating an existing checkpoint over adding routine comments. Report capability, selected execution path, backend evidence, Slack acceptance, safety, request/usage/cost evidence, remaining boundary, and the next bounded proof.

Do not mark the operational-readiness goal complete until every agent family has current direct evidence across the required dimensions.
