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
- `playwright` and `crawl4ai`: future provider placeholders.

## Dataset

Seed cases live in:

```bash
evals/browser_extraction_cases.jsonl
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
  --provider browserless \
  --cases evals/browser_extraction_cases.jsonl \
  --output artifacts/browser_extraction_evals \
  --json
```

Compare current configured providers:

```bash
.venv/bin/python scripts/run_browser_extraction_eval.py \
  --provider firecrawl \
  --provider browserless \
  --cases evals/browser_extraction_cases.jsonl \
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
- useful internal links found
- latency p50/p95
- timeout/error rate
- repeatability across repeated runs
- improvement over the `trafilatura` baseline

Promotion criteria should stay conservative:

- JS-heavy-only benefit: optional fallback after `trafilatura`.
- Broad focused-page benefit with acceptable latency: experimental extractor.
- Unstable or mostly blocked: keep eval-only.
- Do not promote a browser to `SearchProvider` unless it wraps a legitimate search index/API.
