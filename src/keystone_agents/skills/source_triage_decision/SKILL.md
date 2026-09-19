---
skill_id: source_triage_decision
skill_version: 2026-06-09.1
skill_purpose: Decide which retrieved web sources should be retained, reviewed, rejected, or deepened before synthesis.
applies_to:
  - gmail_triage
  - business_research_analyst
  - rag_retrieval_specialist
  - opportunity_scout
  - outreach_composer
  - zotero_context_agent
  - rss_context_agent
  - preprints_context_agent
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/source_attribution.jsonl
validation_paths:
  - tests/test_prompt_contracts.py
  - tests/test_retrieval_policy.py
safety_notes:
  - Source triage does not grant permission to send, publish, draft externally, or expose private/internal context.
---

# Source Triage Decision

## Purpose

Use this skill when search, retrieval, provider lanes, source extraction, or
source-backed synthesis returns candidate sources that must be selected before
the final answer is written.

## Applicable Agents

All registered Keystone agents may use this shared contract when they receive
web-search, retrieval, extracted page, document-retrieval, or source-candidate
context. It is especially important for Chief of Staff, Business Research
Analyst, Opportunity Scout, and Orchestrator-mediated search workflows.

## Typical Inputs

- Raw user request and available Slack/thread context.
- Search queries, provider diagnostics, and retrieved source candidates from
  SearXNG, hosted Agents web search, Exa, Tavily, or future providers.
- Extracted page text, source snippets, source bundles, source refs, claim
  records, and prior WorkItem artifacts.
- Retrieval hints, quality gates, missing-evidence notes, and cost limits.

## Required Behavior

- Read the original user request before deciding whether a source is relevant.
- Classify each candidate source as `retain`, `review`, `reject`, or `deepen`.
- Retain only sources that directly support the requested answer, comparison,
  opportunity, company fact, current claim, or follow-up question.
- Move broad, adjacent, generic, stale, aggregator-heavy, or weakly matching
  sources to review rather than treating them as answer evidence.
- Reject sources whose own evidence contradicts the requested source type,
  eligibility, timing, or action path.
- Request deepening when a source is promising but search snippets are not
  enough to support a detailed synthesis.
- When the specialist, orchestrator, or source triage adds, reranks, or
  promotes additional candidate links during broaden/deepen repair, those links
  must be read/extracted or explicitly marked snippet-only before they can
  support the final synthesis.
- State recall gaps when provider results appear to miss a required source lane
  or when the retained set is too narrow for the requested depth.
- Keep source-triage rationale concise and tied to source text, not generic
  confidence language.

## Flexible Behavior

- May prefer official, primary, recent, and specific sources over broad
  secondary coverage.
- May retain fewer sources than requested when evidence is thin.
- May recommend one bounded broaden/deepen pass when exact matches are missing
  and the request asks for broad, deep, current, or source-backed research.
- May preserve review-only sources for context if they are clearly labeled as
  adjacent and not used as factual support.

## Boundaries

- Must not invent facts, source URLs, source IDs, deadlines, eligibility,
  provider usage, or extracted page content.
- Must not use provider metadata as answer evidence by itself.
- Must not let an available source satisfy the request just because it has
  matching keywords.
- Must not promote a candidate to retained status when its source text says it
  is not the requested kind of opportunity, program, grant, RFP, pilot, or
  formal source.
- Must not hide important source gaps in metadata only; material gaps belong in
  the answer or synthesis.

## Reasoning Questions

- What did the user actually ask to learn, compare, verify, or follow up on?
- Which source sentences or snippets directly answer that request?
- Which sources are only adjacent context, discovery pointers, or provider
  suggestions?
- Are any required source lanes missing, such as official opportunity pages,
  primary company pages, clinical trials, literature, regulatory sources, or
  procurement pages?
- Is more extraction or another bounded search pass needed before a detailed
  synthesis would be honest?

## Decision Rubric

- `retain`: directly relevant, source-visible, sufficiently specific, and safe
  to synthesize from.
- `review`: plausible or adjacent but missing exact evidence, timing,
  eligibility, extraction, or specificity.
- `reject`: off-topic, contradicted, stale for the task, noisy, duplicate,
  unsupported, or not the requested source type.
- `deepen`: likely useful but requires page extraction, follow-up query,
  independent corroboration, or another provider lane before synthesis.

## Tie-Breakers

- Official primary sources beat provider snippets, aggregators, and summaries.
- For formal opportunities, contradiction beats keyword overlap.
- For detailed synthesis, extracted page text beats title/snippet-only evidence.
- When retained source count is thin, recommend one bounded deepen/broaden pass
  instead of padding with adjacent sources.
- Provider diversity is useful diagnostics, but source relevance and directness
  decide what can support the answer.

## Output Contract

Populate the active schema or context pack with retained source IDs/URLs,
review-only candidates, rejected candidates with reasons, deepening requests,
recall gaps, extraction/read status, and the final source basis used for
synthesis. If a broaden/deepen/rerank pass changes the retained source set, the
newly retained links must appear in source refs with extracted/read content
when available, or with a snippet-only limitation. The final user answer should
synthesize from retained and extracted context, not raw provider lists.

## Failure Modes

- Keyword-only matching can retain off-topic sources.
- Search snippets alone can produce shallow synthesis.
- Provider result metadata can overwhelm the answer if not separated from
  evidence.
- A single weak retained source can prematurely stop a needed broaden/deepen
  pass.

## Eval Criteria

- Off-topic or contradictory sources are not retained.
- Exact-match requests can return zero retained sources without padding.
- Detailed answers cite and synthesize retained source content, not only URLs.
- Additional links introduced by specialist deepening or reranking are read or
  explicitly marked snippet-only before final synthesis.
- Review and rejected sources remain auditable but do not dominate Slack output.
- Broaden/deepen recommendations are bounded and cost-aware.
