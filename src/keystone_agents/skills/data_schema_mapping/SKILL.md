---
skill_id: data_schema_mapping
skill_version: 2026-06-28.1
skill_purpose: Map evidence-backed input facts onto target system schemas with model reasoning and deterministic validation.
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
  - tests/test_chief_of_staff.py
safety_notes:
  - This skill does not grant write permission; schema mapping still requires the target tool, live flags, and approval gates.
---

# Data Schema Mapping

## Purpose

Use model reasoning to map evidence from PDFs, images, messages, tables,
records, or context packs into the right target fields for Airtable, Google
Sheets, CRM-like records, local stores, and other structured systems. Keep the
model responsible for understanding the input and target semantics, while
helpers validate field names, types, select options, arithmetic, record
identity, attachments, and side-effect gates.

## Required Behavior

1. Read the input evidence before proposing field values. Evidence may be an
   attached file, local path, document, table row, message, or prior artifact.
2. Inspect or load the target schema before assigning destination fields.
3. Separate source facts, derived values, target field mapping, write
   eligibility, and review notes.
4. Use exact destination field names from the inspected schema. Do not invent
   field names because they sound plausible.
5. Let deterministic helpers validate numeric totals, dates, estimated periods,
   select/multiselect choices, computed fields, attachment fields, and required
   approvals.
6. If model-extracted critical facts conflict with deterministic extraction or
   existing records, block or ask for review instead of writing.

## Flexible Behavior

- For receipt or invoice tasks, extract vendor, date, total, currency,
  description, order number, payment summary, and line-item details from the
  artifact, then derive accounting periods from the receipt date.
- For Airtable, use schema tools before record reads or writes. Prefer bounded
  domain tools when they exist, such as receipt-create helpers; otherwise use a
  generic write plan or exact `airtable_write_record` call only after mapping is
  unambiguous.
- For Google Sheets or Workspace records, map evidence to sheet tabs and column
  names only after confirming the sheet structure.
- For ambiguous targets, propose ranked candidate targets with reasons and ask
  the smallest clarification needed.
- For model-to-tool handoff, pass model-extracted source facts separately from
  exact target field values when the tool supports both.

## Output Contract

- Name the evidence source and whether it was read.
- Name the target system, base/workbook/table/tab, and fields used.
- Show source facts separately from derived values such as estimated tax period.
- Identify every field that is skipped because it is missing, computed,
  incompatible, unsupported, or approval-gated.
- For write execution, include the approval reference and exact bounded tool
  used; otherwise return a dry-run write plan.

## Failure Modes

- If evidence is unreadable, must not map from filename alone.
- If schema is unavailable, must not fabricate field names or options.
- If target fields are ambiguous, must not silently pick the first plausible
  field when a clarification or ranked plan is safer.
- If live write gates are missing, must not create a record, upload an
  attachment, send a message, or mutate a system.
- If model facts and deterministic validation conflict on vendor, date, amount,
  identity, or required target fields, must not proceed without review.

## Eval Criteria

- Reads evidence and schema before mapping.
- Uses exact schema field names and compatible value types.
- Preserves model reasoning while deterministic helpers validate risky details.
- Blocks unsafe writes and explains the unresolved mapping or approval gap.
- Handles nonstandard field names without hard-coded phrase branches.

## Reasoning Questions

- What is the source evidence and which facts are directly supported by it?
- What target schema was inspected, and which field names are exact matches?
- Which values are derived rather than directly present in the evidence?
- Which fields require helper validation, approval, or human review?
- Is this a create, update, attach, summarize, or draft-only action?

## Decision Rubric

- Prefer schema-read, model-map, helper-validate, then bounded-write.
- Prefer exact field and record identity over semantic similarity alone.
- Prefer a dry-run write plan when live gates or field confidence are missing.
- Prefer one bounded tool call that owns validation over multiple loose calls.

## Tie-Breakers

- Use the operator's requested target when it maps clearly to a known schema.
- Use more specific evidence over surrounding Slack or CLI prose.
- Use configured schema choices over model-normalized labels.
- Use human review for close matches, unsupported transformations, or critical
  amount/date conflicts.

## Boundaries

- Must not authorize writes, uploads, sends, deletes, schema changes, or bulk
  mutations.
- Must not encode one-off natural-language shortcuts for a single prompt.
- Must not use hidden chain-of-thought as an audit trail; provide compact
  rationale, field mapping notes, and validation results instead.
- Must not place secrets, PHI, raw private messages, or unnecessary personal
  data into mapped records.
