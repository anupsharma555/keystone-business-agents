# Agent Learning Loop

Keystone agents do not learn by silently changing model weights. They improve through an auditable
local loop that turns human review into memory, evals, prompt updates, and regression coverage.

## Promotion Path

```text
operator feedback
-> structured feedback record
-> prompt-safe memory item
-> fixture or eval case
-> prompt, tool, schema, or renderer change
-> local eval regression
-> prompt version bump
-> documented acceptance
```

## Rules

- Feedback records must stay linked to the approval item or reviewed artifact when available.
- Prompt-safe memory can guide later runs, but it cannot override safety policy, source
  attribution, approval gates, or no-send behavior.
- A recurring feedback theme should become a deterministic fixture, static eval, or documented
  manual validation path before it is treated as resolved.
- Prompt changes must bump prompt metadata versions and preserve prior eval coverage.
- Tool or schema changes need tests at the integration boundary and at the consuming agent output.
- Human-facing formatting belongs in deterministic renderers, not as an unstructured model-only
  contract.

## Operator Feedback Intake

Use the approval-linked response path for review feedback:

```bash
.venv/bin/python scripts/respond_feedback_request.py \
  --approval-id approval_... \
  --rating okay \
  --tag weak_personalization \
  --notes "Tighten the opening and make the CTA more specific."
```

By default, this records feedback and writes a prompt-safe `human_feedback` memory item for later
retrieval. Use `--no-save-memory` only when feedback should remain audit-only.

## Acceptance

A learning-loop change is accepted when:

- the feedback source is recorded
- the proposed behavior has an eval, fixture, or validation path
- safety and approval boundaries still pass
- prompt metadata is updated when prompts change
- `ruff` and relevant tests pass
