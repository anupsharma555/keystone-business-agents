---
name: kba-eval-readiness-triage
description: Repo-local workflow for <repo>. Use only in this repo when diagnosing or changing Promptfoo evals, Slack eval readiness, eval dashboard/database behavior, strict live-readiness preflights, eval trace summaries, route compaction, or no-live SDK eval safety.
---

# KBA Eval Readiness Triage

## Scope

Use this skill only when the current repository is
`<repo>`. If the working directory is
not this repo, stop and do not apply these instructions.

Default to no live agent API calls and no Slack posting. Live Slack probes are
read-only and require explicit user intent or an existing command contract that
states read-only probing.

This skill validates the eval harness and readiness surfaces. It does not
replace `$kba-operational-validation` for realistic direct-agent, WorkItem,
provider, or final Slack-output acceptance. Keep Promptfoo deferred unless the
request is specifically about Promptfoo or eval readiness.

## First Reads

Read the smallest set that matches the failure:

- Promptfoo harness or assertions: `docs/PROMPTFOO_EVALS.md`,
  `promptfoo/providers/keystone_agent_provider.py`,
  `promptfoo/assertions/kba_slack_invariants.py`.
- Strict readiness or live `#evals` setup:
  `scripts/check_eval_slack_readiness.py`,
  `scripts/run_eval_slack_test_server.py`.
- Dashboard or review persistence: `promptfoo/eval_database.py`,
  `promptfoo/eval_dashboard.py`, `promptfoo/eval_dashboard_server.py`.
- Trace summary behavior: `src/keystone_agents/trace_processor.py`,
  `src/keystone_agents/run.py`.
- Command map and known failure signatures: `references/eval-flow.md`.

## Request Shapes

Use this workflow for requests like:

- "Double check the eval workflow", "fix Promptfoo failures", or "why did this
  Slack eval case fail?"
- "Start the eval dashboard", "fix eval dashboard labels/links", or "import the
  latest Promptfoo run".
- "Run strict readiness", "prepare live #evals testing", or "check the Slack
  eval bridge without posting".
- "Fix trace summaries", "add run-level trace metadata", or "diagnose missing
  `keystone.sdk_run_summary.v1` events".

## Workflow

1. Classify the failure surface before editing: Promptfoo config, provider
   compaction, assertion logic, case data, dashboard persistence, readiness
   contract, sibling Slack bridge assumption, or SDK trace metadata.
2. Reproduce with the narrowest offline command. Do not jump to live Slack or
   live SDK calls for a dry-run harness failure.
3. Preserve route-result detail. When the outer route is `orchestrator`, prefer
   inner `route_result` diagnostics for user-facing eval compaction.
4. Keep Slack context sanitized, bounded, deduped, and read-only.
5. Treat strict readiness warnings as blockers for live `#evals` setup, but
   verify whether the assumption is stale before changing runtime behavior.
6. Fix only the layer that is actually failing, then run the full local
   Promptfoo Slack suite or strict readiness gate that caught the issue.
7. Use the full local Promptfoo Slack audit as the real regression stop rule
   after provider/assertion changes; focused tests can miss route and safety
   regressions.

## Safety Rules

- Keep `--no-live-sdk` and dry-run defaults unless the user explicitly asks for
  live model execution.
- Do not post to Slack, create Gmail drafts, send messages, write CRM records,
  or run external side effects while triaging eval readiness.
- Prefer read-only Slack Web API probes only through the documented strict
  live-readiness command.
- Keep trace summaries redaction-first. Store join keys and metadata, not raw
  private messages or secrets.

## Verification

Use `references/eval-flow.md` for the exact command sequence. The usual offline
stop rule after harness changes is:

```bash
npm run eval:promptfoo:validate
npm run eval:promptfoo:json
```

Use strict readiness as the final pre-live `#evals` gate:

```bash
npm run eval:slack:strict-readiness
```
