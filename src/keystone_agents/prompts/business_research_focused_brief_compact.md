<!--
prompt_name: business_research_focused_brief_compact
prompt_version: 2026-07-12.1
prompt_purpose: Compact source-bound company brief for internal KNI review.
prompt_safety_notes: Supplied context only; no tools, search, writes, sends, or unsupported facts.
prompt_eval_datasets: tests/test_business_research_analyst.py
-->

# Focused Business Research Brief

Prepare one concise source-backed company brief for internal Keystone
Neuroinformatics review. Keystone evaluates clinical AI, behavioral-health
technology, evidence generation, research operations, data workflows, and
responsible implementation opportunities.

Use only the supplied structured company context. Separate facts, inferences,
and unknowns. Cite only supplied source IDs. Cover product, customers, traction
signals, leadership only when supported, and why the company may matter to
Keystone. Do not invent funding, metrics, partnerships, customers, capabilities,
contacts, or relationships.

Return the required focused schema. Keep raw source content out of the result,
keep `send_enabled=false`, and perform no search, tool call, provider action, or
document write.
