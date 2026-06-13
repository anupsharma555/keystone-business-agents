# FileSearch Corpus Seeds

This directory contains approved local material for hosted FileSearch ingestion.
It is intentionally text-first: agents can retrieve these files directly from a
vector store without needing to browse source URLs at runtime.

## Corpus Buckets

- `keystone-business-agents-reference`: stable repo operating docs, safety policy,
  agent architecture, prompt contracts, tool boundaries, and curated external
  platform notes.
- `keystone-business-agents-approved-knowledge`: human-approved business research,
  opportunity summaries, reusable outreach guidance, and operations summaries.
  Add files here only after review.
- `keystone-business-agents-public-vendor-reference`: public official docs and
  specs only. Use this for live vector-store smoke tests when private repo docs
  should not leave the local workspace.
- `keystone-business-agents-openai-agents-python-public`: public OpenAI Agents
  Python SDK docs only. Use this first when testing hosted FileSearch for
  agent architecture, tools, handoffs, sessions, and SDK-native behavior.
- `vendor/`: selected official docs and machine-readable API specs that can be
  uploaded to FileSearch directly. These are intentionally scoped to OpenAI
  Agents SDK behavior, OpenAI API schemas, Gmail API schemas, Slack API schemas,
  and LangGraph orchestration concepts relevant to this repo.

## Ingestion Rules

- Ingest only files listed in `seed_manifest.json` or a reviewed successor
  manifest.
- Do not recursively upload broad directories.
- Do not upload `.env`, credentials, OAuth tokens, local databases, artifacts,
  raw Gmail bodies, raw Slack exports, PHI, patient-specific content, or private
  customer notes.
- Prefer repo-owned summaries for third-party docs. Keep source URLs in metadata
  for refresh and attribution, but do not vendor full external documentation
  unless license and terms have been reviewed.
- When vendor files are listed, keep the adjacent `SOURCE.md` or `LICENSE` file
  in the same upload batch so attribution survives retrieval.
- Use metadata on upload: `source_path`, `source_url` when applicable, `corpus`,
  `approved_for`, `sensitivity`, and `generated_from`.

## Runtime Use

Business Research Analyst, Chief of Staff, and Orchestrator can attach hosted
`file_search` when a vector store id is configured through
`KEYSTONE_FILE_SEARCH_VECTOR_STORE_IDS` or an agent-specific override.
FileSearch is for approved internal reference material. Selected Slack
messages, Gmail threads, and WorkItem context should remain local context, not
global corpus content.

Source URLs in the manifest are for attribution and refresh. Agents should use
the local files after those files are uploaded to the configured vector store;
they do not need to browse the URLs at runtime.

Preview the upload set before any live call:

```bash
.venv/bin/python scripts/ingest_file_search_corpus.py --json
```

For live smoke tests from Codex or other automated workflows, upload only the
public vendor corpus:

```bash
.venv/bin/python scripts/ingest_file_search_corpus.py \
  --corpus keystone-business-agents-public-vendor-reference \
  --live \
  --create-vector-store \
  --vector-store-name "kba-public-vendor-corpus-smoke"
```

For the narrower OpenAI Agents Python SDK docs store, use:

```bash
.venv/bin/python scripts/ingest_file_search_corpus.py \
  --corpus keystone-business-agents-openai-agents-python-public \
  --live \
  --create-vector-store \
  --vector-store-name "kba-openai-agents-python-public" \
  --write-local-config
```

The JSON output includes `runtime_configuration.agent_env`. Prefer those
agent-specific environment variables for Business Research Analyst, Chief of
Staff, and Orchestrator instead of the global variable, unless the vector store
is appropriate for every future FileSearch-enabled Keystone agent. The output
also includes `runtime_configuration.config_file.example` for the local JSON
configuration path.

The full `keystone-business-agents-reference` corpus includes internal repo
docs and prompts. The ingestion helper blocks live upload of that non-public
corpus unless an operator deliberately passes `--allow-internal-corpus-upload`
after reviewing the manifest in a trusted environment.

Upload to an existing vector store requires an explicit live flag and vector
store target:

```bash
.venv/bin/python scripts/ingest_file_search_corpus.py \
  --corpus keystone-business-agents-public-vendor-reference \
  --live \
  --vector-store-id "vs_..."
```

Then configure agents to use that store:

```bash
export KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS="vs_..."
export KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_VECTOR_STORE_IDS="vs_..."
export KEYSTONE_ORCHESTRATOR_FILE_SEARCH_VECTOR_STORE_IDS="vs_..."
```

Or keep public/approved vector-store IDs in the ignored local config file:

```json
{
  "schema": "keystone.file_search_vector_stores.v1",
  "agents": {
    "business_research_analyst": {
      "vector_store_ids": ["vs_..."]
    },
    "chief_of_staff": {
      "vector_store_ids": ["vs_..."]
    },
    "orchestrator": {
      "vector_store_ids": ["vs_..."]
    }
  }
}
```

The default path is `.local/file-search-vector-stores.json`. Agent-specific
environment variables still override the local config file when both are set.
The ingestion helper writes or updates this file automatically when
`--write-local-config` is used with `--live`.
