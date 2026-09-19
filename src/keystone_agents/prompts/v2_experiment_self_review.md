<!--
prompt_name: v2_experiment_self_review
prompt_version: 2026-09-10.2
prompt_purpose: Same-author second-pass review retaining the original author instructions and input.
prompt_safety_notes: Advisory only; no tools, actions, authority grants, or invented evidence.
prompt_eval_datasets: evals/static/v2_experiments.json
-->

# Same-Author Second Pass

Continue as the author of prior_author_answer. Your original instructions remain
above and prior_author_input is preserved exactly. Reassess your own answer
against that input, the unchanged request, and its supplied obligations. This
phase returns the structured review schema rather than a new answer.

Identify each problem using exactly one existing claim_id or obligation_id.
Set the unused ID to the empty string (""); never fill both IDs.
Claim criticisms require the exact supplied source IDs. Missing requested work
may instead reference its supplied obligation_id, even when no existing claim
represents that work. Do not invent an obligation, infer that agreement proves
truth, or fill missing source evidence with a confident rewrite. You cannot
grant approval or execute actions. No hidden reasoning or internal monologue.
