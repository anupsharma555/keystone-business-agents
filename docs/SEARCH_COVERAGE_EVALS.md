# Search Coverage Evals

Keystone uses search coverage evals to answer a different question from browser
extraction evals:

- search coverage evals: did discovery find the right source lanes and domains?
- browser extraction evals: can selected URLs be read into useful text?

The eval is query-level and provider-neutral. Current production search providers are
SearXNG, `agents-web-search`, Exa, Firecrawl, and Tavily. Tavily uses its REST
search API and should default to `basic` search depth for the free plan. Tavily
search credits are tracked in live provider/eval runs: `basic`, `fast`, and
`ultra-fast` cost 1 credit/request, while `advanced` costs 2. The local budget
guard defaults to warning near the configured monthly soft limit rather than
blocking retrieval.
`agents-web-search` wraps the OpenAI Agents SDK hosted web-search tool and is used
as the capped parallel lane beside default SearXNG live discovery. Serper is
disabled until credits are deliberately restored. Browserless, Apify, and
Crawl4AI are eval/future boundaries, not live production search adapters.

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
- first primary-source rank and first expected-domain rank
- top-ranked domains for quick source-ranking review
- useful unique domains
- expected-signal claim yield from titles/snippets/content
- whether selected URLs still need extraction because the provider returned
  snippets only
- suggested missing-lane follow-up queries for broad searches that find useful
  results but miss official source lanes or expected domains
- stale or noisy result count
- latency p50/p95
- provider errors
- estimated metered provider calls
- provider credit usage where exposed or locally estimated

Reports also include provider diagnostic specs: intended role, budget class,
promotion status, readiness, default use, live requirement, benchmark focus,
next validation, and promotion rule. Use these specs to interpret SearXNG as the
local/free broad-recall baseline, Exa/Tavily as capped deepening lanes,
Firecrawl as an explicit extraction/search lane, Serper as disabled until
credits are restored, and `agents-web-search` as OpenAI-metered corroboration
that should not be expanded by default without eval evidence and budget
approval.

The shared provider-sequence policy enforces the same boundary before live
providers are built: configured Serper is ignored unless
`KEYSTONE_SERPER_ENABLED=true`, explicit `requested_provider="serper"` remains
available for deliberate comparison/debug runs, and rendered-page providers such
as Browserless, Playwright, Apify, Crawl4AI, and Trafilatura are not accepted as
search-provider defaults or fallbacks.

The tested provider-use ladder is a policy hypothesis, not a permanent claim that
these defaults are globally best. It should be judged by coverage reports,
source-ranking metrics, extraction quality, provider cost/credit telemetry, and
operator-visible diagnostics. Orchestrator may pass `retrieval_hint` constraints
such as precision search, structured enrichment, or search review; the shared
retrieval policy consumes those hints plus query text, missing source lanes, and
extraction quality to decide which conditional lanes are eligible for the run.
Provider selection stays in Python policy rather than prompt-specific branches.

The current ladder is:

- Always/default live discovery: SearXNG.
  The repo container mounts `infra/searxng/core-config/settings.yml` as a
  read-only file so JSON API output remains enabled. Restart the dedicated
  `kba-searxng` profile after changing this mount contract.
- Default capped corroboration: `agents-web-search`, bounded by hosted-search
  caps and not expanded by default.
- Conditional semantic deepening: Exa when broad recall misses lanes or the ask
  needs related-source/landscape discovery, including Orchestrator structured
  enrichment hints. Exa receives a bounded related-source query expanded with
  up to three missing source-lane terms, including when baseline search returns
  no results.
- Conditional research deepening: Tavily for grants, trials, RFPs, literature,
  conference/CFP, and formal opportunity gaps with credit accounting, especially
  when Orchestrator marks the ask as precision-sensitive. Tavily receives a
  bounded official-primary-source query expanded with missing lane terms and is
  capped by `KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN`.
  When both lanes are eligible, Exa runs first because its current allowance is
  materially larger; Tavily runs only if the evidence gap remains.
- Selected-page extraction baseline: Trafilatura after URLs are selected and
  claim-supporting text is needed.
- Selected-page extraction fallback: Firecrawl only when the selected page is
  weak, blocked, or unreadable through the baseline.
- Rendered diagnostics: Playwright for read-only rendered-page failure
  diagnosis, not routine extraction.
- Disabled unless explicitly restored: Serper.
- Eval/future search boundaries: Browserless, Apify, and Crawl4AI are not search
  indexes. Crawl4AI may be evaluated separately as a selected-page extractor.

## Provider Readiness Matrix

Use this as the current ANU-122 milestone view. The matrix is intentionally
conservative: it records what is ready now, what evidence is still missing, and
which benchmark should be run next before promoting a provider or changing
defaults.

| Provider | Current fit | Readiness | Gap | Useful benchmark | Next small validation |
| --- | --- | --- | --- | --- | --- |
| SearXNG | Local/free broad discovery baseline | Ready when `SEARXNG_BASE_URL=http://127.0.0.1:18080` is reachable | Snippets usually still need selected-page extraction | Lane/domain recall, first primary-source rank, extraction-needed followups | Rerun targeted official-lane probes before changing broad-search defaults |
| `agents-web-search` | Capped hosted corroboration beside SearXNG | Policy-defined, but excluded from no-OpenAI diagnostics | OpenAI-metered; not expanded without budget approval | Corroboration value per hosted-search call | Defer until an operator-approved OpenAI live-test budget exists |
| Exa | Semantic/landscape deepening | Candidate, not default fanout | Needs credential/free-tier proof and noise review | Semantic/landscape recall and related-source quality over SearXNG | Run a small no-OpenAI Exa-vs-SearXNG probe only with explicit credentials and free-tier budget |
| Tavily | Research/opportunity deepening | Candidate with credit guard | Needs credit/rate and depth-value evidence | Formal grant, trial, RFP, literature, and conference lane recall | Compare basic-depth Tavily against targeted SearXNG official-domain followups |
| Firecrawl search | Explicit search/extraction lane | Explicit fallback only | Search role is less proven than extraction role | Source recall plus extraction content yield | Prefer selected-page extraction comparison before promoting Firecrawl search |
| Serper | Google-style fallback | Disabled | Credits unavailable/not deliberately restored | None while disabled | Restore only after credit availability and policy review |
| Browserless/Apify/Crawl4AI | Not search providers today | Outside discovery policy | These tools render or extract selected pages rather than query a search index | Not applicable as discovery providers | Keep out of `SearchProvider` defaults unless backed by a real search API |

Live retrieval metadata also exposes compact Slack/CLI-safe diagnostics:
`provider_policy` summarizes the providers actually used by role and budget
class, `provider_use_ladder` summarizes the tested ladder for the current
request/evidence gaps and includes reason codes for conditional provider
decisions, and `source_limits` summarizes missing lanes, missing expected
domains, official-source presence, and whether source verification or search
review is still needed. Search-review hints do not automatically promote
rendered-browser providers; Playwright remains a diagnostics-only lane unless
rendered-page evidence or extraction failure makes it relevant.

Use this eval before adding a new search provider or changing query budgets. A provider
should be promoted only when it improves coverage for one or more source lanes without
weakening source attribution, safety gates, or dry-run defaults.
