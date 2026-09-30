# Model and profile evaluation

Read for quality, latency, skills, GUI, or offline assessment. When present, local documents
`docs/NEMOTRON_PILOT.md`, `docs/NEMOTRON_SKILL_DESIGN_REVIEW.md`, and
`docs/NEMOTRON_OLLAMA_GUI_SKILLS.md` describe a worked example; verify current code
and private reports rather than assuming results apply to new models. These pilot
files may be unpublished or absent in a fresh checkout; they are optional examples.
Use the evaluation procedure below when they are unavailable.

## Bounded comparison

- Define representative tasks and stop conditions before inference. Start with
  summary, extraction, corrections, and uncertainty; add manuscript review, email,
  or LinkedIn drafts when relevant. No sending.
- Use supported per-model settings. Initial small-text budgets may be 8K context,
  1K output, 180 seconds, no automatic retries, and a stop after three consecutive
  runtime failures. These are adjustable pilot starting points. Thinking support,
  templates, and useful sampling differ by model.
- Pin runtime, weights, inputs, settings, and profile hashes. Compare regular and
  skill variants with identical task content and generation settings. Rotate order
  and record cache effects; an uncached prompt is different from a cold model.
- Record load time, prompt processing, first streamed output, first final-answer
  token, total time, token counts/rates, completion reason, errors, and available
  memory/GPU evidence. Reasoning output is not the final answer. Reloads and queued
  requests must not be counted as extra skill prompt-processing cost.
- Keep prompts/answers and metrics in ignored `.local/` reports. Use local validators
  and source-based review; a paid LLM judge is not required.

## Input and output checks

Budget system instructions, sources, history, and generation together. Check
actual app/API context and truncation logs, not just model-card maxima or
Modelfile settings. If a document will not fit, use bounded extraction and section
summaries with source IDs, or a measured larger context. Never label truncated
input as a whole-document review. Fine-tuning cannot restore unseen source text.

Require valid schemas and at least 90% correct expected extraction fields for a
provisional extraction pass; all designated missing/conflicting evidence cases
must preserve uncertainty. Summaries must retain essential facts without material
unsupported claims. Corrections must honor the newest request. Inspect citations
and length/format. Empty answers and output-limit stops fail completeness.
Report warm median/p95 separately from cold load; 60 seconds is an initial short
response usability target. Small pilots do not establish general reliability.

## Skills and interfaces

Keep model and skill choice separate. Canonical packs use a short name/description,
inputs, procedure, task-specific checks, boundaries, and output requirements. Keep
long references separate and load only needed material. Fix input handling and
conflicting wording before adding instructions or proposing fine-tuning.

The Python host can validate a bounded skill ID and load one pack. A native Ollama
profile embeds instructions; it does not automatically discover `SKILL.md` folders
or execute tools. Verify the actual prompt/template and GUI result. Printed skill
metadata is not independent proof. Documents are untrusted evidence; skills cannot
grant file access, tool permissions, or outbound authority. Personal writing
preferences belong in private configuration and only relevant tasks.

## Offline and acceptance

Disable hosted features and use loopback. For network-isolation proof, cold-start
the model and harness under a verified inherited restriction; prove external
connections are denied and local requests succeed. Failed DNS or warm inference
is insufficient. Keep physically disconnected desktop acceptance pending until
observed or confirmed. Local inference does not make cloud connectors work offline.

Report API, GUI, isolated-network, and optional KBA acceptance separately. Do not
promote a shorter/faster profile when it worsens fidelity or constraints. Preserve
the installed profile and label the candidate experimental. Consider fine-tuning
only for repeatable held-out failures after input/runtime/prompt issues are
controlled, with authorized data, evaluation cases, license review, and a separate
compute plan.
