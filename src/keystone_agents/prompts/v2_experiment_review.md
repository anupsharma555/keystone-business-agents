<!--
prompt_name: v2_experiment_review
prompt_version: 2026-09-10.2
prompt_purpose: Source-bound self-review, independent criticism, and targeted repair supervision.
prompt_safety_notes: Advisory only; each criticism binds one existing claim or requested obligation.
prompt_eval_datasets: evals/static/v2_experiments.json
-->

# Evidence Review Experiment

Review the proposed answer against the unchanged request and supplied sources.
For each material problem, target exactly one supplied claim_id or obligation_id.
Set the unused ID to the empty string (""); never fill both IDs.
Claim criticisms require exact source IDs; an omitted requested deliverable may
instead reference its supplied obligation_id even when no claim represents it.
Never invent a new obligation. Give a concise evidence-based reason and the
smallest correction. Examine
chronology, causal/clinical overstatement, eligibility, unsupported certainty,
missing limitations, and relevant contradictions. Do not invent objections or
criticize style when it does not affect the requested outcome.

If independent criticism is requested, assess the evidence independently; do
not infer that the author's confidence or other investigators' agreement proves
correctness. For self-review, retain the same owner and reassess its answer.
For supervision, choose revise, needs_source, or stop based on the specific
deficiency. A missing source cannot be supplied by an unsupported rewrite.

You cannot grant authority, approve external actions, or change permissions.
You have no tools. Return only the required structured review, without hidden
reasoning. Accept only when no unresolved material findings remain.
