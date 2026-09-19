---
skill_id: evidence_attribution_and_claim_mapping
skill_version: 2026-05-31.2
skill_purpose: Convert source material into claim-level evidence that agents can safely reuse.
applies_to:
  - gmail_triage
  - business_research_analyst
  - rag_retrieval_specialist
  - opportunity_scout
  - outreach_composer
  - airtable_context_agent
  - google_workspace_context_agent
  - zotero_context_agent
  - rss_context_agent
  - preprints_context_agent
  - orchestrator
  - chief_of_staff
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - evals/local/source_attribution.jsonl
  - evals/static/business_research_analyst_cases.json
  - evals/static/outreach_composer_cases.json
validation_paths:
  - tests/test_business_research_analyst.py
  - tests/test_outreach_composer.py
safety_notes:
  - Source records and claim records do not grant permission for external use.
---

# Evidence Attribution And Claim Mapping

## Purpose

Use this skill when an agent converts source excerpts, tool results, research
briefs, opportunity records, approved local context, or user-provided facts into
claims that downstream agents may reuse.

## Applicable Agents

All registered Keystone agents may use this shared contract when factual claims
or source-backed context cross tool, artifact, or agent boundaries.

## Typical Inputs

- Source excerpts, URLs, source IDs, article summaries, company pages, search
  results, internal notes, approved CRM/Gmail excerpts, and prior research
  briefs.

## Required Behavior

- Separate factual claims from inference, hypothesis, recommendation, and style
  guidance.
- Attach each material factual claim to source IDs when the source bundle
  provides them.
- Preserve source type, visibility, confidence, and freshness when relevant.
- Mark unsupported or weak claims as unsupported, missing evidence, risks, or
  limitations instead of deleting them silently or promoting them to facts.
- Include visible source URLs in user-facing summaries when the active workflow
  relies on public external facts and URLs are available.

## Flexible Behavior

- May group related claims when the output schema favors concise evidence.
- May lower confidence for indirect, stale, broad, or marketing-heavy sources.
- May recommend follow-up research when attribution is weak or contradictory.

## Boundaries

- Must not invent source IDs, URLs, or source support.
- Must not convert inference or template wording into factual claims.
- Must not expose private, Gmail, CRM, or internal source details in outreach
  unless the context is approved for that use.

## Reasoning Questions

- What exact claim is being made, and which words require evidence?
- Is the claim directly stated by a source, inferred from sourced facts,
  user-provided, internal-only, unsupported, stale, or contradicted?
- Does the source support the claim's specificity, date, scope, and confidence?
- Is the source visible and permitted for the current audience?

## Decision Rubric

- Directly sourced fact: source states the claim with matching scope.
- Supported inference: source facts imply the claim, but confidence should be
  lower and the inference should be labeled.
- User-provided assertion: usable only within the requested context and not as
  independent evidence.
- Internal-only fact: usable for internal reasoning only unless approved.
- Unsupported or contradicted claim: preserve as a gap, risk, or conflict.

## Tie-Breakers

- Prefer primary, recent, specific sources over broad articles or marketing
  pages.
- If a source supports the category but not the named customer, funding,
  deadline, eligibility, or outcome, split the supported and unsupported parts.
- If source IDs are missing, cite available URLs or mark attribution incomplete.

## Output Contract

Populate the current schema's claim, source, facts-used, unsupported-claim,
missing-evidence, confidence, and limitation fields so downstream code can audit
which facts were supported and which were not.

## Failure Modes

- Conflicting sources, missing source IDs, stale evidence, private-only context,
  and model-inferred claims should become conflicts, limitations, unsupported
  claims, or missing evidence.

## Eval Criteria

- Supported claims include source IDs.
- Unsupported claims are not promoted into outbound copy.
- Conflicts remain visible.
- Private/internal sources are not exposed unless approved for the use case.
