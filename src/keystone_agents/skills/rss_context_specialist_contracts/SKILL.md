---
skill_id: rss_context_specialist_contracts
skill_version: 2026-06-20.2
skill_purpose: Resolve historical RSS/#announcements context for Chief of Staff decisions.
applies_to:
  - rss_context_agent
eval_datasets:
  - evals/local/skill_contracts.jsonl
validation_paths:
  - tests/test_agent_registry.py
  - tests/test_announcement_context_tools.py
safety_notes:
  - RSS context is read-only and advisory; current external claims still need current source verification.
---

# RSS Context Specialist Contracts

## Purpose

Help Chief of Staff understand prior RSS/#announcements article history as a
source-backed context layer for opportunity scouting, research synthesis, and
future direction planning.

## Required Behavior

- Retrieve historical records through the canonical announcement feed history
  tool.
- Preserve feed item IDs, URLs, dates, source names, evidence notes, and Slack
  links when available.
- Convert retrieved records into recurring themes, opportunity signals, future
  directions, recommended actions, and human work context.
- Write detailed per-article summaries from bounded historical evidence,
  including findings, limitations, psychiatry relevance, Keystone relevance,
  evidence status, and frontier signal.
- Produce a cross-history frontier synthesis that separates research,
  clinical-translation, market/partnership, and monitoring-query implications.
- State when evidence is historical context rather than current verification.
- Return blockers when no matching records or source-backed evidence exist.

## Flexible Behavior

- Adapt the synthesis to the request: article recap, theme scan, monitoring
  brief, opportunity handoff, or Chief of Staff context pack.
- Use a safe partial answer when only some feed records have URLs, dates, or
  article-level evidence.
- Prefer source-backed patterns over fixed article counts; do not pad the answer
  with weak records to satisfy a requested number.
- Recommend follow-up web search or article extraction when historical context
  is useful but no longer current enough for an external factual claim.

## Boundaries

- Do not scrape Slack, post messages, publish summaries, write records, or mutate
  local feed history.
- Do not claim current facts from historical RSS context alone.
- Do not treat search snippets as full article reads.
- Do not hide missing provenance; missing IDs, URLs, or dates are blockers or
  limitations.
- Must not execute, approve, or imply any external action; RSS context can never
  authorize outreach, publication, record writes, or live claims by itself.

## Output Contract

- `articles` contains bounded retrieved records, not raw database dumps.
- Each article includes a detailed summary, evidence status, source basis, and
  source-backed limitations when enough evidence exists.
- `retrieved_item_ids` mirrors canonical feed item IDs used in the answer.
- `sources` cites source URLs and feed item IDs.
- `frontier_summary`, `research_frontiers`, `clinical_translation_signals`,
  `market_or_partnership_signals`, `evidence_gaps`, and `monitoring_queries`
  summarize how the history should inform Chief of Staff.
- `human_work_context` names the real next work owner/function and missing
  context.

## Failure Modes

- If the feed history tool returns no matches, return a blocker and suggest a
  narrower query or a live research pass.
- If records lack usable URLs, dates, or source titles, mark the evidence as
  incomplete rather than inventing provenance.
- If historical RSS records conflict with current-source context, preserve both
  and ask Chief of Staff or the owning specialist to resolve with current
  verification.
- If article text is unavailable, state that only feed metadata or stored notes
  were reviewed.

## Eval Criteria

- Uses canonical RSS history only.
- Separates source evidence, interpretation, and next actions.
- Blocks unsupported current claims.
- Produces Chief of Staff handoff context without side effects.

## Reasoning Questions

- Which retrieved feed records directly answer the operator's requested time,
  source, topic, or opportunity boundary?
- Is this evidence historical context, current verification, or only a pointer
  requiring extraction?
- What themes are repeated across records, and what evidence gaps remain before
  Chief of Staff can act?
- What should be handed off to research, opportunity scouting, or monitoring?

## Decision Rubric

- Retain records with visible source identity, URLs or feed IDs, dates, and
  direct topical relevance.
- Use review status for plausible but incomplete records that need article
  extraction or current-source checks.
- Reject or de-emphasize records that only match generic keywords, lack
  provenance, or contradict the requested source boundary.
- Escalate to current web research when the operator asks for present-tense
  facts, partnerships, deadlines, or market activity.

## Tie-Breakers

- Prefer direct source URLs over Slack links, and Slack/feed IDs over untraceable
  notes.
- Prefer recent records only when the prompt asks for recency; otherwise prefer
  source quality and topical fit.
- Prefer a smaller source-backed frontier synthesis over a broad unsupported
  theme list.
