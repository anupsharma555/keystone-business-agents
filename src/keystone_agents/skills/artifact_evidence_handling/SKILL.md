---
skill_id: artifact_evidence_handling
skill_version: 2026-06-28.1
skill_purpose: Reason over operator-supplied files and generated artifacts as bounded evidence.
applies_to:
  - chief_of_staff
  - airtable_context_agent
  - google_workspace_context_agent
  - zotero_context_agent
  - gmail_triage
  - outreach_composer
  - opportunity_scout
  - business_research_analyst
  - orchestrator
eval_datasets:
  - evals/local/skill_contracts.jsonl
  - tests/test_prompt_contracts.py
  - tests/test_sdk_execution.py
safety_notes:
  - Local files are private evidence; this skill does not grant sharing, writing, or publication permission.
---

# Artifact Evidence Handling

## Purpose

Use operator-supplied local files, PDFs, images, documents, spreadsheets, and
generated artifacts as bounded evidence when the task depends on their content.
Treat the artifact as a source to inspect or attach, not as a mere filename.

## Required Behavior

1. Detect explicit local paths, uploaded files, Drive/Zotero/Workspace artifact
   references, and generated artifact refs in the ask or context.
2. Identify the artifact role: evidence to read, file to attach, prior output to
   revise, source packet to cite, or record-supporting documentation.
3. Preserve artifact identifiers in structured context: path, filename, MIME
   type, artifact id, source basis, and privacy boundary.
4. Read or attach artifact contents only through approved tools and runtime
   input mechanisms.
5. Extract only content-backed facts from the artifact. Do not infer values from
   filename, folder, sender, or surrounding prose alone.
6. If the artifact is unavailable, unreadable, too large, unsupported, or
   blocked by policy, return that as the exact blocker and continue with a safe
   partial plan when possible.

## Flexible Behavior

- For PDFs/images in live OpenAI SDK runs, use the model-attached file/image
  content when available and cite the filename as the evidence source.
- For generated artifacts, use artifact metadata and reviewed content rather
  than hidden run state.
- For Google Workspace, Zotero, Slack, or local context references, prefer the
  narrowest read tool that can retrieve bounded metadata or content.
- For record writes, use artifact facts to populate fields only after schema
  confirms the target field names.

## Receipt And Invoice Evidence

- Extract vendor, item/service, date, order/receipt number, line items,
  subtotal, shipping/fees, purchase-level taxes, total, currency, and payment
  method summary only when visible in the receipt/invoice evidence.
- Derive accounting or tax-period placement from the artifact date and the
  configured period rules.
- Attach the original receipt/invoice only after the target record exists and
  the destination exposes an attachment field.
- Keep address, phone, email, and payment details minimized unless required by
  schema or the operator explicitly asks for them.

## Output Contract

- State which artifact was used and whether its contents were actually read.
- Separate artifact-backed facts from assumptions, schema guesses, and pending
  field mappings.
- List unresolved artifact blockers precisely.
- For write plans, include the artifact source basis and whether the artifact
  should be attached after record creation.

## Failure Modes

- If artifact content cannot be read, must not fill fields from filename alone.
- If the artifact is private/local, must not present it as a public source or
  share it externally without explicit approval.
- If OCR/PDF/image extraction is uncertain, state uncertainty and ask for review
  rather than overclaiming.

## Eval Criteria

- Uses artifact content as evidence when available.
- Blocks or qualifies when artifact content is unavailable.
- Preserves privacy and side-effect boundaries.
- Does not invent receipt/document fields or source facts.

## Reasoning Questions

- What artifact was supplied and what role does it play?
- Was the artifact content actually readable in this run?
- Which extracted facts are directly supported by the artifact?
- Which destination fields or attachments still require schema/tool verification?

## Decision Rubric

- Use artifact content over filename and surrounding prose.
- Use structured tools or model file inputs before freehand inference.
- Prefer exact artifact blockers over generic clarification.
- Never let artifact access imply permission to write, send, post, or share.

## Tie-Breakers

- Prefer the most direct artifact supplied by the operator.
- Prefer reviewed/generated artifact content over hidden run metadata.
- Prefer privacy-preserving summaries over broad raw dumps.
- Prefer human review when artifact extraction quality is uncertain.

## Boundaries

- Must not read credential paths, token files, hidden dotfiles, or broad home
  folders.
- Must not send, publish, upload, attach, or share private artifacts unless a
  scoped tool, approval reference, and live gate allow that exact action.
- Must not invent facts from unread artifacts, unavailable pages, or filenames.
