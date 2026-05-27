# Visual Context

This folder collects repo-local visuals that explain Keystone architecture for operators,
reviewers, and future agent context packs.

## Orchestrator-First Model Architecture

![KNI Orchestrator-first model architecture](assets/kni-agent-routing-architecture-orchestrator-first-20260525-181618.svg)

The current model path keeps the Orchestrator close to the raw request while
preserving deterministic gates for safety and exactness. Slack, CLI, and
scheduled automation inputs pass raw request/context into Orchestrator
preflight; specialists then receive both the original request and compact
planner context. Output review and real-time feedback close the loop before
Slack, CLI, or artifact renderers present the result.

## Web Search And Extraction

![Keystone Agent Web Search Architecture](assets/web-search-agent-architecture.svg)

The diagram shows the intended boundary:

- SearXNG and capped Agents SDK hosted web search handle default live discovery
- Serper is explicit-only; Tavily can deepen coverage when configured
- Tavily usage is tracked against the local monthly credit budget
- extraction providers read selected URLs after discovery
- sandbox web search review is a second-pass review over staged retrieval packets
- source-backed structured context flows back to the specialist agents

Legacy visual:

![Keystone Web Search & Extraction Architecture](assets/web-search-extraction-architecture.png)
