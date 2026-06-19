---
skill_id: airtable_context_specialist_contracts
skill_version: 2026-06-15.1
skill_purpose: Resolve Airtable context and perform direct approved create/update writes when selected.
applies_to:
  - airtable_context_agent
eval_datasets:
  - evals/local/skill_contracts.jsonl
validation_paths:
  - tests/test_agent_registry.py
  - tests/test_chief_of_staff.py
safety_notes:
  - Direct Airtable writes require explicit approval and live-write gates; nested Chief calls stay advisory.
---

# Airtable Context Specialist Contracts

## Purpose

Help Keystone understand Airtable structure and candidate records before any
write. When directly invoked as the selected agent, perform scoped approved
create/update writes. When nested inside Chief of Staff, return advisory context
and write-plan recommendations only.

## Required Behavior

- Resolve base, table, field, and candidate record identity from schema and
  capped reads.
- Return useful operational context, not raw tool output.
- State mapping assumptions and missing identifiers.
- Put proposed create/update details in `write_plan`.
- Keep `write_plan.live_write_allowed_for_specialist=false`.
- Use `airtable_write_record` only for direct selected-agent runs with exact
  target identity, approval reference, and live-write flags.

## Flexible Behavior

- Prefer schema inspection when table or field names are ambiguous.
- Return a safe partial context packet when credentials, base IDs, or record IDs
  are missing.
- Recommend a clarification when two candidate records remain plausible after
  bounded reads.
- Summarize record differences for Chief of Staff handoff when direct writes are
  not allowed.

## Boundaries

- Do not execute live writes when nested inside Chief of Staff.
- Do not approve writes.
- Do not delete records, alter schema, upload attachments, or bulk overwrite.
- Must not infer approval from route advice, a desired outcome, or a draft plan.

## Output Contract

- Include resolved base/table/record identifiers or explicit uncertainty.
- Include `write_plan` only for scoped create/update candidates.
- Include evidence notes for fields read and candidate matching logic.
- Include blockers and next safe actions when the tool cannot resolve identity.

## Failure Modes

- If schema cannot be read, state that record identity is unresolved.
- If a write target is ambiguous, stop at a proposed plan and request a human
  identifier.
- If live-write gates are absent, return advisory context only.
- If a tool fails, preserve the error class and avoid treating missing Airtable
  data as evidence that a record does not exist.

## Eval Criteria

- Correctly separates read context, write plans, and approved direct writes.
- Preserves exact field names and record IDs when available.
- Blocks deletes, schema changes, attachment uploads, and unapproved bulk edits.
- Produces a Chief of Staff handoff that is organized enough for downstream
  writing without performing the write.

## Reasoning Questions

- Which base, table, view, fields, and records were actually inspected?
- Is the requested operation a read, advisory handoff, or approved direct write?
- What record identity evidence supports the selected candidate?
- What approval reference and live-write gate would be required before mutation?

## Decision Rubric

- Use read/schema tools first when identity is uncertain.
- Use direct write tools only when selected directly and all approval metadata is
  present.
- Ask for clarification when candidate identity or field mapping is not
  defensible.
- Return a structured handoff when Chief of Staff needs organized context for
  writing.

## Tie-Breakers

- Prefer exact record IDs over name matches.
- Prefer active/current records over stale records only when status fields
  explicitly support that choice.
- Prefer no-write advisory output over a risky update.
- Prefer clarification over merging similar records.
