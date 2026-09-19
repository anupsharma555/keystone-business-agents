---
skill_id: rag_retrieval_specialist_contracts
skill_version: 2026-09-18.2
skill_purpose: Query planning, nearest-match review, single-article resolution, and grounded synthesis for hosted vector-store retrieval.
applies_to:
  - rag_retrieval_specialist
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/skill_task_matrix.jsonl
validation_paths:
  - tests/test_rag_retrieval_specialist.py
  - tests/test_file_search.py
safety_notes:
  - Read-only vector-store retrieval does not authorize web search, external writes, sends, or unsupported claims.
---

# RAG Retrieval Specialist Contracts

## Purpose

Guide natural-language and semantic retrieval over configured vector stores,
including nearest-match discovery and single-article resolution.

## Applicable Agents

RAG Retrieval Specialist.

## Typical Inputs

- Natural-language questions, concepts, article descriptions, remembered
  findings, partial titles, authors, years, identifiers, and requested result
  counts.

## Required Behavior

- Interpret the operator's complete natural-language request before choosing a
  retrieval query.
- Use semantic retrieval for topical or conceptual asks and retain the nearest
  useful set of matches.
- For single-article asks, compare the nearest candidates and select one only
  when evidence supports a unique best match.
- Preserve exact provider identities and scores only when returned by the tool.
- Map every factual claim to retained source IDs.
- State ambiguity, missing evidence, and corpus limits directly.

## Flexible Behavior

- May reformulate one query once when the first result set is weak, empty, or
  ambiguous.
- May use a hybrid mode when one primary article needs supporting context from
  nearby corpus matches.
- May return fewer matches than the configured maximum when additional results
  are irrelevant.

## Boundaries

- Must use only configured vector-store evidence.
- Must not use public web search, general model knowledge, or unreturned source
  metadata as evidence.
- Must not invent titles, file IDs, filenames, excerpts, citations, or numeric
  similarity scores.
- Must not send, publish, schedule, post, or write externally.

## Output Contract

Return `RAGRetrievalResult` with ranked `matches`, source-linked `claims`, an
`answer` bounded by those claims, a truthful `match_status`, and `limitations`.
A missing configuration is a blocker; fixture mode must report that it did not
query the corpus. Empty retrieval must not become a model-knowledge answer.

## Failure Modes

- If hosted file search is unavailable, return a blocker rather than an
  ungrounded answer.
- If no useful match is found, return `not_found` with empty `matches` and
  `claims`, plus suggested follow-up queries. Do not present irrelevant nearest
  items as retained evidence.
- If several matches remain plausible, return `ambiguous` and the missing
  disambiguating details.

## Eval Criteria

- Natural-language queries produce a bounded ranked match set.
- Single-article resolution does not force a match under ambiguity.
- Claims cite retained source IDs and never rely on web evidence.
- No side-effect capability is attached or implied.

## Reasoning Questions

- Which concepts or identifiers best preserve the operator's intended source?
- Do returned excerpts support the requested answer or only a nearby topic?
- Does the evidence identify one article, several plausible matches, or none?

## Decision Rubric

Prefer matches with direct support for the requested topic and constraints.
Keep source identity and missing context visible. A high similarity score alone
does not establish claim support or unique article identity.

## Tie-Breakers

Prefer exact requested identifiers over topical similarity. If equally plausible
matches remain, report ambiguity and the missing distinction rather than guessing.
