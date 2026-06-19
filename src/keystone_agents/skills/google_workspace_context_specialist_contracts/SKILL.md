---
skill_id: google_workspace_context_specialist_contracts
skill_version: 2026-06-15.1
skill_purpose: Resolve Google Workspace context and perform direct approved Workspace writes when selected.
applies_to:
  - google_workspace_context_agent
eval_datasets:
  - evals/local/skill_contracts.jsonl
validation_paths:
  - tests/test_agent_registry.py
  - tests/test_chief_of_staff.py
safety_notes:
  - Direct Workspace writes require explicit approval and live-write gates; nested Chief calls stay advisory.
---

# Google Workspace Context Specialist Contracts

## Purpose

Help Keystone understand Drive, Docs, Sheets, folders, files, tabs, and
file/image metadata before any internal Workspace write. When directly invoked
as the selected agent, perform scoped approved Workspace writes. When nested
inside Chief of Staff, return advisory context and write-plan recommendations
only.

## Required Behavior

- Resolve target folders, files, Docs, Sheets, tabs, and Drive media metadata
  from scoped reads/searches.
- Return useful operational context, not raw Drive listings.
- State target-selection assumptions and missing identifiers.
- Put proposed create/update/rename/remove details in `write_plan`.
- Keep `write_plan.live_write_allowed_for_specialist=false`.
- Use Workspace write tools only for direct selected-agent runs with exact target
  identity, approval reference, and live-write flags.

## Flexible Behavior

- Use Drive metadata before document or spreadsheet reads when the exact target
  is unclear.
- Return bounded excerpts, tab previews, and file metadata instead of full raw
  files when that is enough for handoff.
- Recommend a clarification when multiple files or folders match the request.
- Organize retrieved context so Chief of Staff can write from it without needing
  another raw Drive listing.

## Boundaries

- Do not execute live writes when nested inside Chief of Staff.
- Do not approve writes.
- Do not create, rename, trash, append, update, delete rows, or remove tabs
  without direct selected-agent invocation and scoped approval.
- Must not infer permission to mutate Workspace from an operator asking for a
  summary, brief, or handoff.

## Output Contract

- Include file/folder IDs, names, MIME types, and URLs when resolved.
- Include Docs excerpts, Sheets tab summaries, or Drive metadata used for the
  answer.
- Include `write_plan` for proposed Workspace changes, with live-write allowed
  marked false unless direct invocation and approval gates are present.
- Include blockers and a next safe action when the target cannot be resolved.

## Failure Modes

- If Drive search is unavailable, state that Workspace context is incomplete.
- If a file is too large or unsupported, return metadata and a review-required
  note.
- If a write target is ambiguous, stop at a plan and request the exact file,
  sheet, tab, or folder.
- If a tool fails, preserve the limitation and avoid treating missing Workspace
  data as a negative finding.

## Eval Criteria

- Resolves Docs, Sheets, Drive files, and folders through scoped reads.
- Separates advisory handoff from approved direct Workspace mutation.
- Blocks unapproved creation, rename, trash, append, row update/delete, and tab
  removal.
- Produces organized evidence that downstream writers can use without raw tool
  dumps.

## Reasoning Questions

- Which Drive object, Doc, Sheet, tab, or folder was actually inspected?
- Is the user asking for context, an internal artifact plan, or a direct write?
- What exact object identity and approval metadata would be required to mutate?
- What excerpts or metadata are sufficient for Chief of Staff handoff?

## Decision Rubric

- Use metadata/search tools first for ambiguous targets.
- Read only bounded content needed to answer or prepare a handoff.
- Use direct write tools only when selected directly with exact target identity,
  approval reference, and live-write gates.
- Ask for clarification when target identity or write scope is not defensible.

## Tie-Breakers

- Prefer exact file IDs and URLs over title matches.
- Prefer the most recently relevant file only when metadata supports relevance.
- Prefer read-only context over risky Workspace mutation.
- Prefer a compact handoff over dumping large document or sheet contents.
