# Vendored Official Reference Material

This directory contains selected official docs and machine-readable API specs
for FileSearch ingestion. It is intentionally narrower than each provider's full
documentation set.

## Sources

- `openai-agents-python/`: OpenAI Agents Python SDK documentation files from
  `openai/openai-agents-python`, MIT licensed.
- `openai-openapi/`: OpenAI OpenAPI schema from `openai/openai-openapi`, MIT
  licensed.
- `google-gmail-api/`: Gmail API v1 discovery document. Google Developers docs
  are generally CC BY 4.0 except as otherwise noted; code samples are generally
  Apache 2.0.
- `slack-api-specs/`: Slack API specs from `slackapi/slack-api-specs`, MIT
  licensed.
- `langgraph-docs/`: selected LangGraph docs from `langchain-ai/docs`, MIT
  licensed, focused on graph orchestration, persistence, interrupts, subgraphs,
  fault tolerance, and testing.

## Refresh Policy

Refresh these files deliberately, not as a background scrape. After refresh,
update `docs/corpus/seed_manifest.json` with source URLs, retrieval date, and
license notes.
