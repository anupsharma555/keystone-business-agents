---
skill_id: google_workspace_context_specialist_contracts
skill_version: 2026-07-11.3
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

Help Keystone understand Drive, Docs, Sheets, Slides/PowerPoint, folders, files, tabs, and
file/image metadata before any internal Workspace write. When directly invoked
as the selected agent, perform scoped approved Workspace writes. When nested
inside Chief of Staff, return advisory context and write-plan recommendations
only.

## Required Behavior

- Resolve target folders, files, Docs, Sheets, tabs, and Drive media metadata
  from scoped reads/searches.
- Resolve one exact Google Slides or PowerPoint deck and return bounded slide
  text, notes, identity, parent provenance, and extraction checksum without
  changing the parent or inventing a derived copy.
- Prefer scoped Drive search/read first. If no Drive deck exists, use the
  configured allowlisted local presentation search/read tools and preserve only
  relative-path provenance in the result.
- For an exact approved slide-copy request, render one slide into the bounded
  derived-artifact root, verify file signature/checksum and unchanged parent
  hash/mtime, and return the copy receipt. Cleanup is limited to exact marked
  `KBA_TEST_SLIDE` artifacts.
- Return useful operational context, not raw Drive listings.
- State target-selection assumptions and missing identifiers.
- Put proposed create/update/rename/remove details in `write_plan`.
- Keep `write_plan.live_write_allowed_for_specialist=false` for advisory,
  nested, dry-run, or unexecuted plans. A direct write is reported as allowed
  only when Python observes a successful provider-verification receipt.
- Use Workspace write tools only for direct selected-agent runs with exact target
  identity, approval reference, and live-write flags.
- Treat an authenticated direct operator command naming the exact target and
  operation as scoped approval for that action only; call the typed tool with
  `live=true` when the separate provider gate is enabled. Never extend that
  authority to nested/advisory calls or additional objects.
- For disposable Sheet lifecycles, require `KBA_TEST_SHEET` in the title,
  `KBA_TEST_ROW` in the stable row key, read-backs after every mutation, and
  verified Drive trash cleanup.
- For disposable Doc lifecycles, require `KBA_TEST_DOC` in the title, preserve
  the exact provider ID across create and update, read back the body after each
  write, and verify Drive trash cleanup.

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
- Treat slide extraction as read-only. Parent modification and cross-system
  attach/embed/copy operations require separate exact write contracts.
- Local PNG/PDF extraction creates a derived artifact but grants no authority
  to attach, embed, upload, email, publish, or modify its parent.

## Output Contract

- Include file/folder IDs, names, MIME types, and URLs when resolved.
- Include Docs excerpts, Sheets tab summaries, slide artifacts, or Drive metadata used for the
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
- Distinguish the Gmail-only `GOOGLE_TOKEN_FILE` from the Workspace OAuth token.
  Prefer `GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH`; use `GOOGLE_TOKEN_FILE` only as a
  compatibility fallback. When Workspace configuration lives in a sibling
  repo, require explicit `KEYSTONE_CONTEXT_CONFIG_REPO` selection.

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

## Operational Test

- Preview: `.venv/bin/python scripts/run_google_sheet_test_lifecycle.py`
- Approved live window:
  `KEYSTONE_CONTEXT_CONFIG_REPO=/path/to/keystone-slack GOOGLE_WORKSPACE_WRITES_ENABLED=true KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true .venv/bin/python scripts/run_google_sheet_test_lifecycle.py --live-google-workspace --no-dry-run`
- The runner creates one marked Sheet under KNIOps, verifies metadata, appends
  and rereads one marked row, updates by stable key and rereads, deletes the row
  and verifies absence, then trashes the Sheet in `finally` and verifies the
  Drive trash state.

## Tie-Breakers

- Prefer exact file IDs and URLs over title matches.
- Prefer the most recently relevant file only when metadata supports relevance.
- Prefer read-only context over risky Workspace mutation.
- Prefer a compact handoff over dumping large document or sheet contents.
