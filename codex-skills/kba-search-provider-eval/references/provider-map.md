# KBA Search Provider Map

Load this reference when changing search, retrieval, extraction, source
attribution, provider budgets, or provider evals in
`<repo>`.

## Provider Boundary Map

- Search provider wrapper: `src/keystone_agents/tools/search_provider.py`.
- Retrieval policy: `src/keystone_agents/retrieval_policy.py`.
- Live retrieval orchestration: `src/keystone_agents/live_retrieval.py`.
- Source lanes: `src/keystone_agents/source_registry.py`.
- Source quality: `src/keystone_agents/source_quality.py`.
- Visible citation rendering: `src/keystone_agents/visible_sources.py`.
- Page extraction: `src/keystone_agents/tools/website_extraction_tool.py`,
  `src/keystone_agents/source_enrichment.py`.
- Browser diagnostics: `src/keystone_agents/tools/browser_diagnostics_tool.py`,
  `src/keystone_agents/tools/playwright_tool.py`.
- Provider budget/cost surfaces: `src/keystone_agents/costing.py`,
  `src/keystone_agents/tavily_usage.py`, `src/keystone_agents/cost_tracking.py`.

## Current Runtime Rules

- Default dry-run provider is no-network.
- KBA live SearXNG uses `SEARXNG_BASE_URL=http://127.0.0.1:18080`.
- The sibling `keystone-slack` SearXNG instance on port `8080` is not this
  repo's runtime and should not be preflighted for KBA child runs.
- `agents-web-search` is a capped hosted-search lane beside default SearXNG
  when live research is enabled.
- Exa and Tavily are capped deepening lanes when configured by policy/flags.
- Firecrawl is explicit and credential-gated.
- Serper remains disabled unless credits are deliberately restored and
  `KEYSTONE_SERPER_ENABLED=true` is set.
- Trafilatura is the default selected-page extraction baseline.
- Search-heavy agents should combine raw request, provider recall, selected
  URLs, extracted page text, WorkItem/context-pack data, and approved memory
  before synthesis. Do not present weak generic search results as research
  leads.

## Dry-Run Evals

Search coverage:

```bash
.venv/bin/python scripts/run_search_coverage_eval.py \
  --provider searxng \
  --provider serper \
  --cases evals/provider/search_coverage_cases.jsonl \
  --output artifacts/search_coverage_evals \
  --json
```

Browser/page extraction:

```bash
.venv/bin/python scripts/run_browser_extraction_eval.py \
  --provider browserless \
  --cases evals/provider/browser_extraction_cases.jsonl \
  --output artifacts/browser_extraction_evals \
  --json
```

## Live Evals

Run live evals only with explicit user intent and configured providers:

```bash
.venv/bin/python scripts/run_search_coverage_eval.py \
  --provider searxng \
  --provider firecrawl \
  --provider tavily \
  --provider agents-web-search \
  --cases evals/provider/search_coverage_cases.jsonl \
  --output artifacts/search_coverage_evals/current-providers \
  --live --no-dry-run \
  --json
```

```bash
.venv/bin/python scripts/run_browser_extraction_eval.py \
  --provider firecrawl \
  --provider browserless \
  --cases evals/provider/browser_extraction_cases.jsonl \
  --output artifacts/browser_extraction_evals/current-providers \
  --live --no-dry-run \
  --json
```

## Promotion Checklist

Do not promote a provider or change default budgets unless evidence shows:

- Better expected source-lane or domain recall.
- No regression in primary-source count or visible attribution.
- Acceptable stale/noisy result rate.
- Bounded provider errors and latency.
- Explicit live flags and credential checks.
- Dry-run tests and fixture behavior remain clean.
- No new external side effects.

## Source Attribution Checks

- User-facing summaries with external factual claims include visible URLs in the
  first answer or explicitly say source verification is still needed.
- Selected URLs support the claims they are attached to; search snippets alone
  should not be treated as extracted evidence when page reads were required.
- Provider diagnostics distinguish candidate discovery results from retained
  source evidence and rejected/noisy sources.
- Company, opportunity, clinical, regulatory, funding, and policy claims state
  uncertainty when sources conflict or are stale.

## Runtime Checks

Use the repo-local SearXNG manager when needed:

```bash
./scripts/manage_searxng_headless.sh status
```

Do not substitute the `keystone-slack` runtime on port `8080` for this repo's
`18080` endpoint.

## Focused Tests

```bash
.venv/bin/python -m pytest tests/test_search_provider.py tests/test_retrieval_policy.py
.venv/bin/python -m pytest tests/test_search_coverage_eval.py tests/test_browser_extraction_eval.py
.venv/bin/python -m pytest tests/test_source_registry.py tests/test_source_quality.py tests/test_source_triage.py
```
