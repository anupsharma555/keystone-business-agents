# KBA Validation Workflow

This workflow is authoritative only for `<repo>` and complements the repo's
current `AGENTS.md`, operational status, and human execution jobs.

## Evidence Ladder

1. Semantic natural-language interpretation and correct ownership.
2. Typed tool selection and consumption with a fake/local model.
3. Provider read or reversible marked lifecycle without a model where possible.
4. Useful structured reasoning over bounded supplied or provider context.
5. Direct single-agent continuation or modification proof.
6. WorkItem and optional LangGraph handoff, identity preservation, review, repair, and approval checkpoint.
7. Slack connector acceptance: delivery, thread identity, requested content, sources, uncertainty, safety, duplication, truncation, and debug leakage.
8. Slack desktop visual confirmation for new renderers, route changes, failures, or sampled live runs.

Framework coverage does not substitute for provider or model evidence. Provider primitives do not prove agent-selected execution.

## Live OpenAI Gate

Before every approved live batch:

- State the exact scenario, model, expected request count, maximum request count, and dollar cap.
- Use the repo's approved cost-monitoring path when current billing evidence is available; otherwise state that only the explicit test cap is authoritative.
- Use isolated sessions, no live search unless approved, and serial execution.
- Prefer `--max-openai-requests`; use `--no-live-manual-plan` when deterministic preflight is sufficient.
- Capture model, command, fixture, usage, local cost estimate, trace/run IDs, tools/providers, structured output, and side-effect evidence.
- Record the request ceiling, conservative preflight estimate, and actual requests made as separate values. A preflight-blocked run may consume zero requests.

Stop immediately after the first shared failure or ceiling breach. Fix and verify offline before requesting another live allowance.

## Human-Job Lifecycle

For systems that support mutation, prove bounded identity and schema, read context, marked creation, read-back, same-object modification, verification, exact cleanup when supported, absence of unrelated side effects, and correct natural-language tool selection.

## Major Checkpoint Format

- Capability: `PASS`, `PARTIAL`, `FAIL`, or `BLOCKED`, with the failed dimension.
- Evidence: route, typed tool, provider IDs/source URLs, artifact or WorkItem ID.
- Execution path: bounded direct specialist, canonical Orchestrator/WorkItem, or optional LangGraph backend, with why that shape was selected.
- Quality: what the result did well and what remained inaccurate.
- Slack acceptance: connector content result, permalink/thread timestamp, and visual result when required.
- Safety: sends/posts/writes and cleanup outcome.
- Consumption: ceiling, estimate, actual requests, tokens, cost evidence, and billing snapshot when applicable.
- Regression: focused test and broader gate only when justified.
- Next proof: one bounded scenario, not a broad eval suite.
