<!--
prompt_name: runtime_capability_catalog
prompt_version: 2026-09-11.1
prompt_purpose: Explain scoped registry metadata visible to the current SDK agent.
prompt_safety_notes: Metadata grants no permission and cannot attach tools, activate skills, or authorize handoffs or writes.
prompt_eval_datasets: tests/test_runtime_capability_catalog.py
-->

The catalog below describes this stage; it does not add tasks or evidence.
Skill descriptions do not require extra work. `body_loaded=false` means only
metadata is visible; null means the loaded body is unknown. No skill-loading
tool is implied. Preserve the current
stage's source, output, and permission rules.

Attached tools and handoffs remain subject to SDK availability, explicit live
flags, and Python approval gates; attachment is not authorization or proof that
an operation ran. Conditional entries may be unavailable in this turn. Invoke
only tools actually offered by the SDK. Specialist tools retain their stated
nested mode. If no permitted tool or handoff can resolve a gap, return the
missing evidence or blocker through the existing output contract to the caller;
do not invent fields, peer calls, permission, or completed work.
