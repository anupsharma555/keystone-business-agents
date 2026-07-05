# Tech Stack

## Core Stack

Keystone Business Agents uses the OpenAI Agents SDK as the required agent framework. Agent builders should return SDK `Agent` objects or the local SDK compatibility wrapper used in tests. Do not replace the core agent layer with LangChain, LangGraph, or provider-specific abstractions.

Pydantic schemas define all agent inputs and outputs. These schemas are the contract between agents, tools, scripts, tests, storage, and future UI/API surfaces.

Testing and quality gates use pytest and ruff. GitHub Actions should run the same checks used locally:

```bash
pytest
ruff check .
```

SQLite is the default local audit log and artifact store. It is used for deterministic local runs, source records, approval status, agent run metadata, draft records, and data-only follow-up schedule recommendations without requiring a hosted database.

Direct provider implementations remain first-class:

- Gmail API: direct Gmail OAuth and REST integration for message reads, labels, and draft-only replies.
- Slack API: direct Slack approval notification paths.
- SearXNG: optional self-hosted or private metasearch through `SearchProvider`; not required for tests.
- Agents SDK hosted web search: capped parallel discovery lane beside SearXNG
  for default live research when enabled.
- Exa: AI-oriented semantic search lane for capped deepening when configured,
  and explicit Exa-first comparison runs.
- Tavily: optional deeper-research search lane with local credit tracking.
- Serper: disabled while API credits are unavailable; requires
  `KEYSTONE_SERPER_ENABLED=true` after credits are restored.
- Firecrawl: optional live search provider and optional website extraction provider when explicitly configured.
- Trafilatura: default live-gated website extraction provider for selected company pages.
- Apify and Browserless: placeholder wrappers only; live operations are not implemented.

## Optional Integrations

Optional integrations must not be required for tests. They should be introduced behind adapter implementations, configuration flags, and dry-run mocks.

- Gemini OpenAI compatibility: primary Gemini path for Gmail Triage, using Google's direct OpenAI-compatible endpoint with `GEMINI_API_KEY` and the existing OpenAI Agents SDK provider.
- LiteLLM: optional model routing and cost control layer through an external OpenAI-compatible gateway. It can sit behind `ModelProvider` via `LITELLM_BASE_URL` or `KEYSTONE_*_BASE_URL`, while OpenAI Agents SDK remains the required agent framework. Do not add the Python `litellm` package as a Keystone runtime dependency unless its OpenAI client requirements match the Agents SDK baseline.
- LlamaIndex: optional local or cloud document knowledge layer. It can back `KnowledgeProvider` for retrieval from internal docs, folders, or indexed stores.
- Firecrawl: optional LLM-ready search and website extraction provider through
  `SearchProvider` and the website extraction tool. It requires `FIRECRAWL_API_KEY`
  for live use.
- Crawl4AI: optional future extraction provider. It is not implemented today.
- Browserless cloud features: Smart Scrape, Search, Map, and Crawl are future-consideration
  candidates only. They are not implemented, not wired into any current agent, and not available
  to agent tools today. If added later, treat them as provider implementations only, not as
  prompt-only behavior. They must stay dry-run by default, require explicit live flags and
  credentials, preserve source attribution, and respect site terms, privacy, and legal constraints.
- SearXNG: optional search backend for lower-cost, self-hosted, or privacy-sensitive metasearch. It implements `SearchProvider` and stays dry-run by default. Live use requires explicit live mode and base URL.
- Composio: optional OAuth and app-action abstraction. It can implement Gmail, Slack, or other app providers, but direct Gmail and Slack integrations must remain available.
- CRM adapters: HubSpot, Airtable, and Google Sheets CRM providers are future work only.
  Version 1 exposes a local table-mirror-backed `CRMProvider` without network calls or live
  dependencies.
- MCP servers: optional local filesystem, private provider, or standardized
  tool access. They should be treated as provider implementations, not as
  mandatory runtime dependencies. Use `docs/AGENTS_SDK_REVIEW.md` before adding
  one; direct SDK function tools remain the default for Keystone-owned provider
  boundaries.
- LangGraph: optional durable orchestration for resumable WorkItem workflows. It lives behind
  the `orchestration` extra and runs graph-native WorkItem nodes. It must not replace the
  OpenAI Agents SDK agent contracts, canonical WorkItems, context packs, or approval gates.

## Search Provider Configuration

Dry-run, SearXNG, Agents SDK hosted web search, Exa, Firecrawl, and Tavily
implement the shared `SearchProvider` interface. Live search is gated by
`--live-search --no-dry-run`; SearXNG requires `SEARXNG_BASE_URL`, Exa requires
`EXA_API_KEY` when selected or enabled as capped deepening, Firecrawl requires
`FIRECRAWL_API_KEY`, and Tavily requires `TAVILY_API_KEY`. Serper is disabled
until credits are restored.

Search coverage is measured separately from page extraction. `scripts/run_search_coverage_eval.py`
compares query-level provider results against expected source lanes and domains in
`evals/provider/search_coverage_cases.jsonl`. Use it to decide whether broader search, longer search,
or a future provider is likely to find websites the current retrieval ladder misses.

Search provider environment names:

```bash
SEARCH_PROVIDER=searxng
EXA_API_KEY=
EXA_BASE_URL=https://api.exa.ai
EXA_SERVICE_API_KEY=
EXA_API_KEY_ID=
EXA_API_KEY_NAME=default
SERPER_API_KEY=
SEARXNG_BASE_URL=http://127.0.0.1:18080
SEARXNG_API_KEY=
KEYSTONE_SEARXNG_TRANSIENT=true
FIRECRAWL_API_KEY=
FIRECRAWL_BASE_URL=https://api.firecrawl.dev
TAVILY_API_KEY=
TAVILY_BASE_URL=https://api.tavily.com
TAVILY_MCP_LINK=
TAVILY_SEARCH_DEPTH=basic
KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT=1000
KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT=850
KEYSTONE_TAVILY_CREDIT_ENFORCEMENT=warn
KEYSTONE_TAVILY_USAGE_PATH=
KEYSTONE_TAVILY_SEARCH_FALLBACK=false
KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN=2
KEYSTONE_EXA_MONTHLY_FREE_REQUEST_LIMIT=1000
KEYSTONE_EXA_SEARCH_FALLBACK=true
KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN=2
KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK=true
KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL=true
KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN=2
KEYSTONE_AGENT_HTML_REVIEW=true
KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES=2
```

Rules for live search providers are the same:

- dry-run by default
- no network calls during pytest
- explicit live flag/config path required before search
- source links preserved on every factual claim
- primary discovery lanes are SearXNG, Agents SDK hosted web search, Exa, and
  Tavily. Search-capable agents call the shared `search_web` tool; Python
  retrieval policy decides which provider lanes to attempt and records
  provider diagnostics.
- when no provider is explicitly selected, the retrieval ladder defaults to
  SearXNG broad recall plus a capped Agents SDK hosted web-search parallel lane.
  Exa is the capped semantic deepening lane when configured; Tavily is the
  capped deeper-research lane when explicitly enabled or selected by a formal
  research/opportunity policy.
- Trafilatura, Firecrawl, and Crawl4AI are secondary extraction providers for
  selected URLs after discovery. Playwright/browser diagnostics are rendered-page
  inspection, not primary search.
- Serper is disabled while API credits are unavailable. Keep
  `KEYSTONE_SERPER_ENABLED=false`; set it true only after credits are restored
  and an explicit Serper comparison is intended.
- Exa is treated as a metered/free-tier lane. The working assumption is 1,000
  credits/month, so Exa is capped by
  `KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN` when enabled. Use explicit
  `SEARCH_PROVIDER=exa` for provider comparison or targeted Exa-first runs.
  For Friday API usage reports, `scripts/check_exa_usage.py` can call Exa's
  admin usage endpoint when `EXA_SERVICE_API_KEY` plus either `EXA_API_KEY_ID`
  or `EXA_API_KEY_NAME=default` are set;
  it estimates remaining free-tier credits against
  `KEYSTONE_EXA_MONTHLY_FREE_REQUEST_LIMIT` and links the dashboard for
  authoritative credit balance review.
- Tavily credit tracking is warn-by-default: `basic`, `fast`, and `ultra-fast`
  search cost 1 credit/request, `advanced` costs 2, and local monthly usage is
  tracked under the runtime state directory unless `KEYSTONE_TAVILY_USAGE_PATH`
  overrides it. SDK search can reach Tavily for explicit deeper-search/provider
  comparison asks when `TAVILY_API_KEY` is configured, even if broad Tavily
  fallback is off. Set `KEYSTONE_TAVILY_CREDIT_ENFORCEMENT=block` only when a
  hard local cap is desired.

Opportunity Scout adds a deterministic search layer on top of `SearchProvider`.
It builds lane-specific queries for company growth, collaboration, researcher,
institute, conference, grant, trial, and role discovery. Broad multi-lane prompts
do not treat every lane term as a global constraint. If accepted results under-fill,
Scout can run bounded adaptive follow-up queries and SearXNG-compatible page
deepening. `KEYSTONE_OPPORTUNITY_FOLLOWUP_RESULT_CAP` defaults to 8 and is the
hard cap for follow-up results per query.

Website extraction is separate from search:

```bash
KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true
KEYSTONE_WEBSITE_EXTRACTOR=trafilatura
KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK=crawl4ai
KEYSTONE_AGENT_HTML_REVIEW=true
KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES=2
KEYSTONE_ENABLE_SOURCE_API_ENRICHMENT=false
```

The extractor supports `trafilatura`, `crawl4ai`, and `firecrawl`. It should only process
selected URLs, convert extracted text into source-backed claim candidates, and
remain disabled by default.
Agent HTML review is an optional second pass over already retrieved page text.
Use it for weak/no-claim extractions, keep the page cap low, and retain the
original URL-backed source record as the canonical evidence container.

`KEYSTONE_ENABLE_SOURCE_API_ENRICHMENT=true` lets live research supplement known
ClinicalTrials.gov, PubMed, and DOI references with structured public API fields.
Keep it disabled for offline tests or when a run should only use already-retrieved
source text.

## Adapter Rules

Adapters define seams, not mandatory dependencies.

- Every provider must support mock or dry-run mode.
- Tests must pass without optional provider packages or live API keys.
- Direct implementations must remain available.
- No platform lock-in: provider interfaces should expose Keystone domain operations rather than vendor-specific APIs.
- Live implementations must use timeouts, clear errors, secret redaction, and audit logging.
- Sending external messages is not part of the adapter contract unless a future approval and audit design explicitly adds it.

## Current Provider Boundaries

- `ModelProvider`: model routing and SDK run configuration.
- `SearchProvider`: source-attributed web search.
- `ScrapeProvider`: URL or page extraction into text/metadata.
- `GmailProvider`: Gmail read, label, and draft-only operations.
- `SlackProvider`: approval and notification operations.
- `CRMProvider`: local fixture/table-mirror lead listing, account context lookup, and
  approval-aware dry-run CRM write previews. No live CRM provider is implemented.
- `KnowledgeProvider`: document retrieval and citation-ready context.
- `StorageProvider`: audit and artifact persistence.
