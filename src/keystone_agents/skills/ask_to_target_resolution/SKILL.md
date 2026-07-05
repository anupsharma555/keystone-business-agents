---
skill_id: ask_to_target_resolution
skill_version: 2026-06-28.1
skill_purpose: Convert diverse operator asks into concrete system targets before tool use or clarification.
applies_to:
  - chief_of_staff
  - airtable_context_agent
  - google_workspace_context_agent
  - gmail_triage
  - outreach_composer
  - opportunity_scout
  - business_research_analyst
  - orchestrator
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - tests/test_prompt_contracts.py
  - tests/test_cli.py
  - tests/test_chief_of_staff.py
safety_notes:
  - Resolves likely targets only; does not grant write permission or bypass approval gates.
---

# Ask To Target Resolution

## Purpose

When the operator describes a business object, artifact, record, or evidence
source in natural language, infer the likely target system and object before
asking for clarification. Use schema and context tools to verify the target, then
ask only for the remaining information that cannot be resolved safely.

## Required Behavior

1. Parse the ask into: requested action, target system, business object, evidence
   source, desired output, and side-effect scope.
2. Infer likely target identifiers from established Keystone mappings, such as
   Airtable finance tracker tables, Google Workspace folders/docs, Gmail
   threads, Slack channels, Zotero collections, or local document paths.
3. Preserve inferred targets in structured context: base alias, table, record
   key, field candidates, file path, source basis, approval state, and operation.
4. Verify inferred targets with the narrowest schema, metadata, or read tool
   before reading records or writing.
5. Use deterministic helpers for exact arithmetic, record identity,
   deduplication, date/period placement, field matching, and write-plan
   construction.
6. Ask for clarification only after the target cannot be inferred or verified,
   or when multiple candidates remain plausible.

## Flexible Behavior

- Use project-specific aliases when the ask clearly names a known business
  object, such as finance tracker expense tables, KNI Ops Workspace artifacts,
  Gmail threads, Slack channels, or Zotero collections.
- Use the operator's exact wording when it overrides the default mapping.
- Prefer a bounded write plan when the target is clear but live gates or schema
  fields remain unresolved.

## Airtable Finance Receipt Mapping

For requests such as "add a business expense to Airtable business expenses based
on this receipt PDF/image":

- Infer `base_alias="finance_tax_tracker"` from the finance/tax tracker context.
- Infer table `Business Expenses` for business-expense asks and `Personal
  Expenses` for personal-expense asks.
- Treat the local PDF/image path as evidence for the record fields, not as a
  filename-only hint.
- Inspect Airtable schema before field mapping.
- Extract receipt-backed vendor, item/service, order or receipt number, date,
  subtotal, shipping/fees, purchase-level taxes, total, currency, and payment
  method summary.
- Derive `Estimated Tax Periods` from the receipt date using tracker period
  rules. Do not use the current date unless the receipt lacks a date and the
  operator approves that fallback.
- Create/update only through approved typed Airtable tools and attach the receipt
  only after record identity and an attachment field are known.
- Do not block by asking for base/table when the ask already names Airtable
  business or personal expenses. Block on unresolved schema field names, missing
  receipt evidence, missing attachment support, missing approval, or disabled
  live gates.

## Clarification Standard

A clarification should name the exact unresolved item and the attempted inference
path. Poor: "Confirm the Airtable base/table/fields." Better: "I inferred
`finance_tax_tracker` / `Business Expenses`; schema did not expose a
  multiple-attachment receipt field, so I can create the expense but cannot attach
  the receipt unless you identify the attachment field or update schema."

## Output Contract

- State the inferred system target and why it follows from the ask.
- Preserve exact identifiers in structured fields or context entries.
- Name which tools or schema reads should verify the target.
- Name only unresolved blockers after the inference/tool pass.

## Failure Modes

- If no known target mapping applies, return a narrow clarification instead of
  guessing a base, table, folder, thread, or collection.
- If schema contradicts the inferred mapping, prefer live schema evidence and
  report the conflict.
- If a receipt, file, record, or source cannot be read, do not infer its
  contents from the filename.

## Eval Criteria

- Correctly maps a natural-language ask to the likely system target.
- Verifies or plans verification through bounded tools.
- Avoids asking the operator to restate information that the ask already implies.
- Preserves approval and side-effect gates.

## Reasoning Questions

- What system and object does the business wording imply?
- What evidence or schema read would prove the inference?
- Which fields or identifiers are still unresolved after target inference?
- Is the next step a read, a write plan, an approved write, or a clarification?

## Decision Rubric

- Infer when the business object maps to a known Keystone target.
- Verify with schema/tools before reading broadly or writing.
- Clarify only when the target is absent, contradicted, or ambiguous.
- Never treat inferred target as approval for mutation.

## Tie-Breakers

- Prefer explicit operator wording over default project mappings.
- Prefer live schema evidence over stale prompt memory.
- Prefer no-write plans over partial or risky writes.
- Prefer exact IDs and field names over display names when available.

## Boundaries

- This skill does not approve writes, sends, posts, scheduling, schema changes,
  deletes, or external publication.
- Inferred targets must remain overrideable by explicit operator wording or
  live schema evidence.
- Must not invent schema fields, record IDs, receipt values, tax treatment,
  source facts, or approval references.
