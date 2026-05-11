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
- Serper: optional live web search for higher-precision source-attributed research.
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
- MCP servers: optional local filesystem and standardized tool access. They should be treated as provider implementations, not as mandatory runtime dependencies.
- LangGraph: optional later durable orchestration for resumable, multi-step workflows. It should not replace the OpenAI Agents SDK agent contracts.

## Search Provider Configuration

Dry-run, SearXNG, Serper, and Firecrawl implement the shared `SearchProvider` interface.
Live search is gated by `--live-search --no-dry-run`; Serper requires `SERPER_API_KEY`,
SearXNG requires `SEARXNG_BASE_URL`, and Firecrawl requires `FIRECRAWL_API_KEY`.

Search provider environment names:

```bash
SEARCH_PROVIDER=serper
SERPER_API_KEY=
SEARXNG_BASE_URL=http://127.0.0.1:8080
SEARXNG_API_KEY=
FIRECRAWL_API_KEY=
FIRECRAWL_BASE_URL=https://api.firecrawl.dev
```

Rules for SearXNG, Serper, and Firecrawl search are the same:

- dry-run by default
- no network calls during pytest
- explicit live flag/config path required before search
- source links preserved on every factual claim
- when no provider is explicitly selected, the retrieval ladder defaults to
  SearXNG broad recall and can escalate to Serper precision search

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
KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK=firecrawl
KEYSTONE_ENABLE_SOURCE_API_ENRICHMENT=false
```

The extractor supports `trafilatura` and `firecrawl`. It should only process
selected URLs, convert extracted text into source-backed claim candidates, and
remain disabled by default.

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
