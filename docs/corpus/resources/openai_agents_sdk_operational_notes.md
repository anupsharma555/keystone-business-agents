# OpenAI Agents SDK Operational Notes For Keystone

Purpose: provide local corpus content for Keystone agents about the SDK features
that this repo uses or has intentionally enabled.

## SDK Sessions

Agents SDK sessions are client-side conversation memory for multi-turn runs.
When a session is passed to the runner, the SDK retrieves prior session items,
prepends them to the current input, and stores new items after the run. This is
useful for Slack thread follow-ups, `keystone ask` continuity, and WorkItem
clarification loops.

Keystone boundary:

- Sessions are optional continuity, not canonical state.
- WorkItems, SQLite audit records, approval items, structured outputs, source
  refs, and artifacts remain authoritative.
- Do not store raw PHI, secrets, credential material, or unapproved outbound
  drafts in reusable session history.
- Use stable, scoped, hashed session ids for Slack flows, such as a hash of
  team id, channel id, thread timestamp, and user id.
- Use file-backed SQLite sessions for local continuity; use in-memory sessions
  only for tests or one-off smoke runs.

## Agent.as_tool

`Agent.as_tool()` exposes an agent as a callable tool for another agent. The
called agent receives generated input and returns tool output; the original
agent continues the conversation afterward. This is different from a handoff,
where a specialist agent takes over the conversation.

Keystone boundary:

- Use `as_tool` only for read-only internal synthesis.
- Keep default Orchestrator behavior unchanged unless an explicit runtime flag
  enables specialist tools.
- Do not let specialist tools send email, post Slack messages, schedule events,
  update CRM, create approval items, or persist memory unless a future reviewed
  flow explicitly adds that behavior with tests.
- Python safety gates, WorkItem readiness gates, source sufficiency checks, and
  approval/no-send policy remain authoritative.

Current Keystone prototype:

- The Orchestrator can opt into `business_research_analyst_research_brief`.
- The nested Business Research Analyst tool removes memory-write tools before
  being exposed.
- Hosted `file_search` may remain available inside that specialist tool when
  explicitly configured because it is retrieval-only.

## RunConfig And Tracing

Keystone builds live and local SDK run configs centrally. Live execution requires
explicit credentials and live flags. Trace metadata must be scalar operational
metadata only and must not contain prompts, email bodies, draft copy, secrets,
PHI, patient-specific content, or approval artifacts.

## Hosted Tools

Keystone uses hosted `WebSearchTool` and `FileSearchTool` only behind explicit
runtime configuration. Repo-owned function tools remain the default integration
boundary for Gmail, Slack, local context, storage, memory, search providers,
approval queues, and operations publishing.

Sources summarized:

- Agents SDK sessions: https://openai.github.io/openai-agents-python/sessions/
- Agents SDK Agent.as_tool reference: https://openai.github.io/openai-agents-python/ref/agent/
- Agents SDK overview: https://openai.github.io/openai-agents-python/
