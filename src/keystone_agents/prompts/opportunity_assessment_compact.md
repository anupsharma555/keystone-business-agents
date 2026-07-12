<!--
prompt_name: opportunity_assessment_compact
prompt_version: 2026-07-12.1
prompt_purpose: Compact source-bound assessment of one supplied opportunity.
prompt_safety_notes: Review-only; no tools, search, outreach, writes, or unsupported claims.
prompt_eval_datasets: tests/test_compact_opportunity_assessment.py
-->

# Compact Opportunity Assessment

Assess exactly one supplied opportunity for Keystone Neuroinformatics. Use only
the supplied packet. Do not search, call tools, draft outreach, or perform an
external action.

Return the required structured schema with:

- concise confirmed facts that cite only retained source IDs;
- interpretation clearly separated from confirmed facts;
- a bounded Keystone fit assessment without inventing a buyer, relationship,
  consulting path, revenue path, eligibility, geography, or prize;
- explicit timing and geography status;
- missing evidence and one next safe action;
- each retained source exactly once;
- `outreach_recommended=false` and `external_action_performed=false`.

Prefer uncertainty over unsupported specificity. Keep the result compact and
operator-readable.
