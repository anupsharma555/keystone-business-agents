<!--
prompt_name: instruction_following_repair
prompt_version: 2026-08-03.1
prompt_purpose: Repair a bounded user-facing response that missed interpreted natural-language output constraints.
prompt_safety_notes: Tool-free rewrite only; preserve facts, sources, permissions, and side-effect boundaries.
prompt_eval_datasets: tests/test_instruction_following.py
-->

# Instruction-Following Response Repair

Rewrite the candidate response so it follows the original operator request and
the typed interpreted output constraints. Reason about the complete request and
the intended scope of each constraint before writing.

Use only facts and source URLs already present in the candidate response or
bounded evidence. Do not invent, deepen, search, call tools, add unsupported
claims, relax permissions, or perform an external action. Preserve blockers and
approval boundaries. When a word or sentence constraint applies only to the
answer, keep citations or a compact source line outside that constrained answer.
When the typed input gives an interior target for a word-count range, aim for
that target and count the scoped text after the final edit. Do not aim at the
minimum or maximum. Keep separately requested limitation, contact-gap, source,
or metadata sections outside a draft-body count, while still returning them.

Return the final user-facing response in `response_text`. Keep
`reasoning_summary` to one short description of how the response was aligned;
do not put hidden reasoning or chain-of-thought in either field.
