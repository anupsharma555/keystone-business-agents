---
name: kba-search-provider-eval
description: Repo-local workflow for <repo>. Use only in this repo when changing or evaluating Keystone search providers, retrieval policy, SearXNG or Agents web-search behavior, Exa/Tavily/Firecrawl/Trafilatura lanes, browser extraction, source attribution, provider budgets, or search coverage evals.
---

# KBA Search Provider Eval

## Scope

Use this skill only when the current repository is
`<repo>`. If the working directory is
not this repo, stop and do not apply these instructions.

Keep discovery, extraction, synthesis, and attribution separate. Do not use the
`keystone-slack` repo's SearXNG runtime on port `8080`; this repo owns the
`kba-searxng` runtime and `SEARXNG_BASE_URL=http://127.0.0.1:18080`.

## First Reads

Read the smallest set that matches the task:

- Query-level discovery coverage: `docs/SEARCH_COVERAGE_EVALS.md`.
- Selected-page extraction quality: `docs/BROWSER_EXTRACTION_EVALS.md`.
- Provider policy and budgets: `src/keystone_agents/retrieval_policy.py`,
  `src/keystone_agents/live_retrieval.py`,
  `src/keystone_agents/tools/search_provider.py`.
- Source lanes and attribution: `src/keystone_agents/source_registry.py`,
  `src/keystone_agents/source_quality.py`,
  `src/keystone_agents/visible_sources.py`.
- Command map and promotion rules: `references/provider-map.md`.

## Request Shapes

Use this workflow for requests like:

- "Improve live search", "compare providers", "add Exa/Tavily/Firecrawl", or
  "change retrieval budgets".
- "Why are search results generic?", "fix source attribution", or "make the
  research answer more source-grounded".
- "Run search coverage evals", "evaluate browser extraction", or "decide
  whether a provider should be promoted".
- "Use SearXNG for KBA child runs" or "check why the local KBA search runtime is
  unavailable".

## Workflow

1. Classify the change as discovery, extraction, source triage, synthesis
   context, budget/cost tracking, or provider diagnostics.
2. Start dry-run. Validate provider boundaries, case shape, scoring, and
   artifact writing before any live provider call.
3. Use live mode only with explicit user intent, explicit live flags, configured
   credentials or local runtime checks, and no side-effect writes.
4. Preserve source-visible answers. External factual claims need URLs or an
   explicit source-verification-needed state in the first user-facing summary.
5. Keep provider-specific behavior behind shared retrieval/tool boundaries. Do
   not add provider-specific prompt branches just because a provider exists.
6. Promote or change defaults only when eval evidence improves at least one
   source lane without weakening attribution, safety gates, dry-run defaults, or
   budget controls.
7. When search-heavy scheduled automations delegate to this repo, preserve the
   same Orchestrator preflight, deterministic retrieval, specialist synthesis,
   review, and renderer-owned output path used by manual `@KNI` runs.

## Provider Boundaries

- Use search coverage evals to test whether discovery finds the right lanes,
  domains, and signals.
- Use browser extraction evals to test whether selected URLs can be rendered or
  extracted into useful text.
- Treat Trafilatura as the static extraction baseline.
- Treat Firecrawl as an optional configured extraction/search provider.
- Treat Browserless, Apify, Playwright, and Crawl4AI-style paths as eval,
  placeholder, diagnostic, or future boundaries unless the repo has a reviewed
  live adapter and tests.

## Verification

Use `references/provider-map.md` for exact commands. For provider or budget
changes, run the relevant focused tests plus the dry-run provider eval. Live eval
evidence should be saved under `artifacts/` only when explicitly requested.
