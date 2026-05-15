# Slack And Gmail Integration Notes For Keystone Agents

Purpose: give Keystone agents local, searchable operating guidance for Slack
message actions, Slack approval boundaries, and Gmail draft-only behavior.

## Slack Message Shortcuts

Slack message shortcuts are invoked from a specific message context. They are
appropriate when the workflow depends on the selected message or thread, such as
running a Keystone agent against a posted company, opportunity, or request.

Keystone operational shape:

- A message shortcut captures bounded selected-message context.
- The Slack runtime opens a modal asking the operator for the Keystone task.
- The modal submission starts a WorkItem with a local context file.
- The context file remains local and scoped to that WorkItem.
- Slack context should not be uploaded into global FileSearch corpora.
- Any public Slack post remains approval-gated unless a future reviewed path
  explicitly permits a specific internal write.

Recommended session scope for Slack:

- Use SDK sessions only when live SDK continuity is requested.
- Derive the session id from a hash of team id, channel id, thread timestamp,
  and user id.
- Do not show raw session ids in public Slack messages.
- Do not store raw Slack message text in a reusable global corpus.

## Slack Approval Boundary

Slack approval messages are review surfaces, not send authorization by default.
Approval buttons may update local approval state. They must not create or send
external email unless the approval item explicitly allows that downstream action
and the live integration path is separately enabled.

## Gmail Drafts

Gmail draft creation is distinct from sending. Keystone Gmail behavior should
remain draft-only unless a future implementation adds a reviewed send path with
separate explicit approval and tests.

Keystone Gmail rules:

- `create_gmail_draft_reply` may create a draft only when the Gmail live flags
  and approval state allow it.
- No agent should expose a `send_email` or Gmail send tool by default.
- Draft text remains approval-gated for external use.
- Legal, contractual, financial, security, PHI, and professional-advice content
  requires human review and may receive acknowledgement-only draft behavior.

## Agent Routing Guidance

Slack task examples:

- "Research this company" -> Business Research Analyst or Orchestrator route to
  Business Research Analyst.
- "Is this worth pursuing?" -> Opportunity Scout or Business Research Analyst,
  depending on whether the request asks for discovery or validation.
- "Draft a reply" -> Gmail Triage or Outreach Composer, but only from approved
  context and with draft-only output.
- "Summarize the automation status" -> Chief of Staff.

Sources summarized:

- Slack shortcuts: https://docs.slack.dev/interactivity/implementing-shortcuts/
- Slack interaction payloads: https://docs.slack.dev/reference/interaction-payloads/shortcuts-interaction-payload
- Gmail drafts.create: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create
