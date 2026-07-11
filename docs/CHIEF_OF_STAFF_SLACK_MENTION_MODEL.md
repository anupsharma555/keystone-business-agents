# Chief of Staff Slack Mention Decision

Status: **defer a separate Slack profile or alias**. Keep Chief of Staff
addressable through the existing KNI application as `@KNI chief of staff ...`.

## Recommendation

The safest current operator model is one Slack application identity with one
auditable entry surface. The KNI Slack bridge should preserve the raw mention,
bounded channel/thread/user context, and selected-message context, then invoke
the existing KBA path. KBA runs Orchestrator preflight before Chief synthesis,
uses WorkItems for durable execution, applies Python approval and identity
gates, and returns through the shared renderer.

A dedicated Chief Slack profile would add a second apparent authority without
adding a new capability. It would duplicate app identity, scopes, event
handling, deduplication, permissions, and audit interpretation while making it
less clear whether the request passed through the shared Orchestrator and
WorkItem controls. Do not implement that profile until observed operator usage
shows that the `@KNI chief of staff` phrase is itself a material usability
problem.

Action buttons or shortcuts may later provide convenience for bounded actions
such as reviewing blockers, continuing the current WorkItem, or requesting an
automation audit. They remain alternate UI controls for the same backend path,
not separate routing or approval authorities.

## Supported Chief Behavior From Slack

Chief may plan, summarize, diagnose, coordinate specialists, inspect selected
Slack context and WorkItem state, call bounded read/context capabilities, and
recommend the next safe action. It may hand durable work to the owning
specialist or typed provider handler.

An authenticated direct operator command may approve the exact supported write
and target it names, subject to the provider live flag, unique target identity,
typed tool contract, and read-back receipt. Addressing Chief does not itself
grant write authority. Chief must not infer extra recipients or targets, expand
one action into bulk writes, bypass an owning specialist, or treat a profile,
channel, button, or prior approval as blanket permission. Ambiguous, unsupported,
or incompletely scoped writes stop with a targeted blocker.

## Required Audit Contract

Every Chief-origin Slack run should retain or reference:

- raw Slack request and sanitized channel/thread/user context;
- selected route and backend, including direct runner versus LangGraph;
- WorkItem or run ID and the bounded context/source references used;
- approval state, exact provider action and target when applicable;
- specialist/tool receipts, review outcome, and no-send/no-write status when no
  external action occurred;
- final renderer status that distinguishes completed, blocked, partial, and
  failed execution instead of saying only that a command completed.

Raw private Slack text, secrets, PHI, and unapproved outbound copy are not
promoted into reusable memory.

## Ownership And Dependencies

KBA owns Orchestrator preflight, Chief behavior, WorkItems, typed provider
tools, approval/identity gates, review, and structured receipts. The
`keystone-slack` repository owns the KNI app identity, mention/event handling,
bounded Slack context, interactive controls, thread rendering, and Slack-side
deduplication.

No new implementation is required for a separate profile. Any future shortcut
or alias work depends on the same answer-first renderer and controlled Slack
pilot used by ANU-60 and ANU-61; it must not fork the execution path established
by ANU-124.

## Revisit Trigger

Reconsider a dedicated profile only after measured evidence shows repeated
misrouting or discoverability failures that cannot be solved with help text,
autocomplete examples, shortcuts, or clearer completion receipts. A future
proposal must include a scope/identity migration plan, duplicate-event controls,
permission parity tests, and proof that both entrypoints create the same
Orchestrator/WorkItem audit trail.
