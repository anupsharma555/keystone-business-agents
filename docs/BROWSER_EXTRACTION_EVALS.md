# Browser Extraction Evals

Keystone treats headless browsers as rendered page extraction providers, not primary
websearch providers. SearXNG, hosted web search, explicit Serper, and Firecrawl search remain responsible for
discovery and ranking. Browser providers are evaluated on selected URLs after
discovery.

Use `docs/SEARCH_COVERAGE_EVALS.md` for query-level provider coverage tests. Use this
document when the question is whether a selected URL can be rendered or extracted well.

![Keystone Web Search & Extraction Architecture](assets/web-search-extraction-architecture.png)

## Provider Contract

Rendered page providers expose:

```text
render(url, timeout_seconds) -> RenderedPage
```

`RenderedPage` normalizes provider output:

```text
provider
url
final_url
status
title
text_or_markdown
links
html_length
latency_ms
error
metadata
```

Current eval provider boundaries:

- `trafilatura`: static HTTP + local extraction baseline.
- `firecrawl`: optional Firecrawl scrape path when configured.
- `browserless`: current placeholder boundary; live rendering is not implemented.
- `apify`: not a general rendered-page provider in this repo; exposed as unsupported here.
- `crawl4ai`: experimental local/open-source extraction adapter. It is excluded
  from default production fallbacks and live use is blocked unless
  `KEYSTONE_ENABLE_EXPERIMENTAL_CRAWL4AI=true` is set for an explicit controlled
  evaluation. Chromium redirect interception is not yet implemented.
- `playwright`: read-only rendered diagnostics, not routine extraction.

## Dataset

Seed cases live in:

```bash
evals/provider/browser_extraction_cases.jsonl
```

Rows include:

```text
id
mode: wide | focused | specific
url
category
difficulty_tags
expected_signals
forbidden_signals
timeout_seconds
notes
```

The seed set covers directories, conferences, grant/procurement pages, focused company pages,
trial/literature/regulatory pages, and job boards.

## Running

Dry-run mode validates the case file, provider registry, scoring, and artifact writing without
network or browser execution:

```bash
.venv/bin/python scripts/run_browser_extraction_eval.py \
  --provider crawl4ai \
  --cases evals/provider/browser_extraction_cases.jsonl \
  --output artifacts/browser_extraction_evals \
  --json
```

Compare current configured providers:

```bash
.venv/bin/python scripts/run_browser_extraction_eval.py \
  --provider firecrawl \
  --provider crawl4ai \
  --cases evals/provider/browser_extraction_cases.jsonl \
  --output artifacts/browser_extraction_evals/current-providers \
  --live --no-dry-run \
  --json
```

`trafilatura` is included as the baseline unless it is the only selected provider.

## Scoring

The eval scores agent-useful outcomes, not just page load status:

- extraction success rate
- expected signal recall
- forbidden/captcha/access-denied detection
- title preservation
- useful text length
- boilerplate ratio
- extraction quality bucket: `strong`, `partial`, `weak`, or `unreadable`
- compact quality diagnosis for missing signals, blocked pages, low text,
  high boilerplate, missing titles, and provider errors
- useful internal links found
- latency p50/p95
- timeout/error rate
- repeatability across repeated runs
- improvement over the `trafilatura` baseline

Reports also include provider diagnostic specs: intended role, budget class,
promotion status, readiness, default use, live requirement, benchmark focus,
next validation, and promotion rule. Treat `trafilatura` as the static
extraction baseline, Firecrawl as an explicit managed scrape fallback,
Playwright as read-only diagnostics, Crawl4AI as an experimental local eval lane,
and Browserless/Apify as future boundaries until reviewed live adapters exist.

## Extraction Readiness Matrix

This is the selected-page counterpart to the search-provider matrix in
`docs/SEARCH_COVERAGE_EVALS.md`.

| Provider | Current fit | Readiness | Gap | Useful benchmark | Next small validation |
| --- | --- | --- | --- | --- | --- |
| Trafilatura | Static HTTP extraction baseline | Ready as the default selected-page baseline | Can be weak on JS-heavy, blocked, or boilerplate-heavy pages | Signal recall, useful text length, boilerplate ratio, blocked-page rate | Rerun as baseline for every selected-page extraction comparison |
| Firecrawl | Managed scrape/extraction fallback | Credential-gated fallback candidate | Needs credit/rate and attribution evidence over Trafilatura | Signal recall and blocked-page recovery over Trafilatura | Small Firecrawl-vs-Trafilatura selected-page probe with explicit credit budget |
| Playwright diagnostics | Local rendered-page diagnostics | Ready for explicit read-only diagnostics, not routine extraction | Does not become a production extractor just because search review is requested | Console/page-error/request-failure diagnosis for selected URLs | Use only after static extraction is weak or rendered diagnostics are requested |
| Browserless | Rendered-browser boundary | Placeholder/eval boundary | Live production adapter and safety constraints are not implemented | JS-heavy selected pages after adapter review | Keep out of production until adapter, safety constraints, and attribution tests exist |
| Apify | Actor-based future extraction boundary | Not implemented | No reviewed adapter | Not applicable yet | Add adapter/tests before any provider comparison |
| Crawl4AI | Local/open-source selected-page extractor | Experimental eval-only adapter | Browser redirects are not intercepted before navigation; repeatable quality and latency evidence is also missing | Local extraction lift over Trafilatura on controlled JS-heavy pages | Keep blocked by default; add private-address request interception before any production promotion |

Promotion criteria should stay conservative:

- JS-heavy-only benefit: optional fallback after `trafilatura`.
- Broad focused-page benefit with acceptable latency: experimental extractor.
- Unstable or mostly blocked: keep eval-only.
- Do not promote a browser to `SearchProvider` unless it wraps a legitimate search index/API.
