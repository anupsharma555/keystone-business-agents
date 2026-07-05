# Local Documents

Use this folder for local, private source documents that should not be committed.

Suggested CV path:

```text
documents/CV_Operator_2026.docx
```

Suggested founder-fit profile path:

```text
documents/founder_fit_profile.json
```

Suggested private operator-context path:

```text
documents/operator_context.md
```

Before adding a CV, remove PHI, patient details, personal IDs, home address, private phone
numbers, credential files, secrets, and any private contract terms that should not be used in
agent prompts or reports.

To create a local CV review packet later:

```bash
.venv/bin/python scripts/review_founder_cv.py \
  --cv documents/CV_Operator_2026.docx \
  --output documents/founder_cv_review.json
```

Use `documents/operator_context.example.md` and
`docs/OPERATOR_CONTEXT_QUESTIONS.md` to decide which answers should become
approved prompt context, founder-fit profile fields, local memory, or private
session-only notes.
