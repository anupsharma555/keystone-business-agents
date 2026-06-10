# Skill Update Backlog

This backlog tracks follow-up work for Keystone `SKILL.md` bundles after the
reasoning/rubric pass. Treat skills as prompt-visible capability contracts:
focused, reusable, progressively disclosed, and backed by evals or deterministic
gates where behavior matters.

## P0

- Extend the first route-level gate events beyond Business Research, Outreach,
  Gmail, and Chief of Staff to cover Orchestrator brief expansion and
  Opportunity Scout formal-opportunity gates in the same
  `skill_contract_gates_checked` schema.
- Add per-skill fixture evals that validate the new `Reasoning Questions`,
  `Decision Rubric`, and `Tie-Breakers` against concrete agent outputs.
- Add a selector regression suite for short natural-language requests so the
  Orchestrator expansion skill proves it can enrich terse asks without changing
  the operator's constraints.
- Add formal-opportunity eval cases for grants, RFPs, pilots, and CFPs that
  verify Opportunity Scout does not collapse multi-lane requests into RFP-only
  retrieval.

## P1

- Split large specialist contracts into smaller focused skills only where evals
  show reusable behavior across agents or repeated failure modes.
- Add OpenAI-style trigger phrases to each skill description so request-time
  selection can be tested from the same language operators use in Slack.
- Add negative-selection tests showing unrelated specialist skills remain absent
  from compact runs.
- Add source-provider evals that compare SearXNG, hosted web search, Tavily
  deepening, and Crawl4AI extraction for the same formal-opportunity prompt.
- Add a benchmark view that groups failures by skill ID, agent, provider, prompt
  version, and model.
- Add sibling `keystone-slack` bridge parsing for the exported
  `operator_failure` contract so failed child runs render kind, reason, next
  step, and retryability without exposing raw traceback text.

## P2

- Add optional `references/` files for skills that need compact rubrics or
  examples but should not inflate the main `SKILL.md`.
- Add deterministic helper scripts only for repeatable validation work, such as
  schema checks or source-coverage summaries; keep reasoning in prose contracts.
- Add human-review examples for borderline source sufficiency, duplicate
  records, unsupported claims, and approval-state ambiguity.
- Review whether any local skills should become sandbox-only executable skills
  after the OpenAI Skills API/runtime surface stabilizes for Keystone's safety
  model.

## OpenAI Alignment Notes

- Keep each skill scoped to one repeatable job with clear inputs and outputs.
- Use `SKILL.md` as the manifest and put optional scripts, references, or assets
  beside it only when they improve reliability.
- Keep agent identity, tools, guardrails, schemas, and handoffs at the agent
  layer; skills should not grant permissions or attach tools.
- Preserve progressive disclosure by loading only the selected skill subset per
  run, then tracing the selected IDs for auditability.
