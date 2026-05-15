# Keystone FileSearch Corpus Policy

Purpose: give Keystone agents searchable local reference material while keeping
private runtime context, credentials, and approval-gated artifacts out of hosted
vector stores.

## What FileSearch Is For

Use hosted FileSearch for durable, approved reference material:

- repo operating docs and runbooks
- agent architecture and prompt contracts
- safety, approval, no-send, and source-attribution policies
- curated notes about OpenAI Agents SDK, Slack interactivity, Gmail drafts, and
  Keystone integration behavior
- human-approved research briefs or operations summaries after review

Do not use FileSearch as a dumping ground for raw runtime data. Selected Slack
messages, Gmail threads, WorkItem context packs, and local artifacts should be
passed as scoped local context for the current run.

## Vector Store Model

OpenAI vector stores are hosted containers for searchable uploaded files. When a
file is added to a vector store, the platform parses, chunks, embeds, and indexes
it for semantic retrieval. A runtime agent can use `FileSearchTool` only for the
configured vector store ids. This means local filesystem permissions must be
enforced before upload, not at model runtime.

## Keystone Upload Boundary

An ingestion script should:

- resolve every source path under the repo root
- reject symlinks or resolved paths outside the repo
- upload only files listed in a reviewed manifest
- attach upload metadata: `source_path`, `corpus`, `approved_for`,
  `sensitivity`, `generated_from`, and `source_url` when applicable
- fail closed on blocked filenames or patterns
- print only file paths and ids, never file contents that might include secrets

## Never Upload

- `.env`, `.env.*`, API keys, OAuth tokens, service account files, or credential
  JSON
- local SQLite databases or cache directories
- raw Slack exports, selected Slack messages, Gmail bodies, Gmail attachments,
  private customer notes, PHI, or patient-specific material
- unreviewed artifacts under `artifacts/`
- broad home-directory or root filesystem content

## Agent Behavior

Business Research Analyst:

- Use `file_search` for approved internal context, policies, templates, and
  durable research notes.
- Use live search for current public facts when explicitly enabled.
- Preserve source attribution and distinguish internal reference from public
  evidence.

Chief of Staff:

- Use `file_search` for operations docs, runbooks, Slack business-agent mode,
  automation notes, and approved internal process summaries.
- Keep Slack posting approval-gated. Retrieval from corpus never grants posting
  permission.

Outreach Composer:

- FileSearch should not be used for open-ended prospect research.
- Outreach may use only approved context and approved style/template material.
  Any external-use draft remains approval-gated.

Sources summarized:

- OpenAI Retrieval and vector stores: https://developers.openai.com/api/docs/guides/retrieval
- Keystone FileSearch implementation: `src/keystone_agents/file_search.py`
