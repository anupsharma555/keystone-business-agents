# Search Coverage Evals

Keystone uses search coverage evals to answer a different question from browser
extraction evals:

- search coverage evals: did discovery find the right source lanes and domains?
- browser extraction evals: can selected URLs be read into useful text?

The eval is query-level and provider-neutral. Current production search providers are
SearXNG, `agents-web-search`, Serper, Firecrawl, and Tavily. Tavily uses its REST
search API and should default to `basic` search depth for the free plan. Tavily
search credits are tracked in live provider/eval runs: `basic`, `fast`, and
`ultra-fast` cost 1 credit/request, while `advanced` costs 2. The local budget
guard defaults to warning near the configured monthly soft limit rather than
blocking retrieval.
`agents-web-search` wraps the OpenAI Agents SDK hosted web-search tool and is used
as the capped parallel lane beside default SearXNG live discovery. Exa, Brave,
and Browserless are accepted as future eval boundaries but are not live
production adapters in this repo.

## Dataset

Seed cases live in:

```bash
evals/provider/search_coverage_cases.jsonl
```

Rows include:

```text
id
mode: wide | focused | specific
query
category
expected_source_lanes
expected_domains
forbidden_domains
expected_signals
requires_extraction
max_results
time_range
source
notes
```

Source lanes come from `keystone_agents.source_registry`:

```text
company_site
careers_jobs
clinical_trials
grants_funding
literature
procurement_rfp
conference_events
press_news
people_institutions
regulatory
```

## Running

Dry-run mode validates cases, provider boundaries, scoring, and artifact writing without
network execution:

```bash
.venv/bin/python scripts/run_search_coverage_eval.py \
  --provider searxng \
  --provider serper \
  --cases evals/provider/search_coverage_cases.jsonl \
  --output artifacts/search_coverage_evals \
  --json
```

Live mode requires explicit live confirmation and provider configuration:

```bash
.venv/bin/python scripts/run_search_coverage_eval.py \
  --provider searxng \
  --provider serper \
  --provider firecrawl \
  --provider tavily \
  --provider agents-web-search \
  --cases evals/provider/search_coverage_cases.jsonl \
  --output artifacts/search_coverage_evals/current-providers \
  --live --no-dry-run \
  --json
```

## Scoring

The eval scores agent-useful discovery outcomes:

- expected source-lane recall
- expected domain recall
- forbidden/noisy domain hits
- primary-source count
- useful unique domains
- expected-signal claim yield from titles/snippets/content
- stale or noisy result count
- latency p50/p95
- provider errors
- estimated metered provider calls
- provider credit usage where exposed or locally estimated

Use this eval before adding a new search provider or changing query budgets. A provider
should be promoted only when it improves coverage for one or more source lanes without
weakening source attribution, safety gates, or dry-run defaults.
