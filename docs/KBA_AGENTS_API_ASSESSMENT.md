# KBA V2 addendum — OpenAI Agents API

Reviewed 2026-09-10. This is an implementation assessment, not a deployed adapter
or a live test of the new API.

**Recommendation:** evaluate the Agents API as an optional managed research or
document-review backend. Preserve KBA's local LangGraph and direct SDK execution.
Start with one bounded read-only child task, with KBA retaining business identity,
approvals, effect receipts, validation, and delivery.

## What changes

The September 10 public beta exposes a managed Codex harness. OpenAI operates the
model/tool loop, durable sessions, compaction, and orchestration. The application
supplies tools and can choose no environment, an OpenAI-hosted sandbox, or its own
compute. This is a different execution boundary from importing the OpenAI Agents
SDK into KBA. [Announcement](https://openai.com/index/introducing-the-agents-api/),
[overview](https://developers.openai.com/api/docs/guides/agents-api/overview)

| KBA need | Potential contribution | KBA responsibility retained |
|---|---|---|
| Long research or document jobs | Managed context and session lifecycle | Exact task/evidence version, required deliverables, source grounding |
| Complementary investigators | Independent reasoning contexts and managed coordination | Nonoverlapping assignments, source deduplication, disagreement handling |
| Large tool registry | Dynamic tool discovery and programmatic tool calling | Which tools are permitted, bounded schemas, provider limits |
| Laptop-independent work | Hosted harness; optional hosted compute | Reliable application-side function handler and delivery worker |
| Recovery and inspection | Saved session items and turn outcomes | Provider intent, unknown outcomes, reconciliation, business checkpoint state |

The API can run without a sandbox (`environment.type: "none"`). Function calls
still execute in application code: KBA can broker narrowly scoped existing tools
without mounting its repository, home directory, or operational databases. If
that handler goes offline, work can wait for its result. A hosted harness alone
does not make local providers continuously available.
[Architecture](https://developers.openai.com/api/docs/guides/agents-api/architecture)

## Boundaries that affect this project

- **Independent context is not independent storage.** Subagents have separate
  contexts, but agents using an environment share its filesystem. A concurrency
  ceiling limits simultaneous subagents; it is not a total spend or request cap.
  [Multi-agent guide](https://developers.openai.com/api/docs/guides/agents-api/multi-agent)
- **Managed recovery does not establish exactly-once effects.** The self-hosted
  lifecycle documentation explicitly leaves compute/files with the application,
  does not guarantee recovery of pending input after a process crash, and warns
  that a disconnect can fail a tool even when the turn completes. The V2 journal
  and unknown-outcome handling remain useful.
  [Lifecycle](https://developers.openai.com/api/docs/guides/agents-api/environments/lifecycle)
- **Completion is a layered claim.** Check the turn outcome and its tool results.
  Streams do not replay missed events; retrieve saved session items after a
  disconnect. Persist KBA's event cursor and accepted output separately from the
  visible Slack acknowledgment.
  [Sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions)
- **Tool dispatch still needs exact identity.** Handle current required actions,
  not historical function-call items. Bind session, turn and call IDs to a KBA
  operation before executing; return the saved result on duplicate delivery.
  Keep approval checks at execution time.
  [Functions](https://developers.openai.com/api/docs/guides/agents-api/tools/functions)
- **Hosting is not a cost guarantee.** Model/tool/container charges apply. Join
  root and child turns and paginate usage. Counts can be missing or revised and
  are not a final bill; cache-write charges may not be reconstructible from the
  exposed usage fields. Compare aggregate task cost and quality.
  [Usage](https://developers.openai.com/api/docs/guides/agents-api/observability)
- **Self-hosting does not keep model context local.** The harness is hosted.
  Keep third-party credentials outside agent compute, use a broker, and treat
  environment-key access separately from application API permissions.
  [Sandbox security](https://developers.openai.com/api/docs/guides/agents-api/environments/security)

## Concrete first experiment

1. Add an isolated adapter behind the existing execution contract. Accept a
   sanitized request, fixed evidence bundle, output schema, stage ID, and explicit
   limits. Return source-bound output plus session/turn/usage references.
2. Use no environment, no provider writes, and no business credentials. First run
   one author; then test two complementary evidence roles only if justified.
3. Compare the same evidence against current direct execution, local graph
   execution, and the managed child. Hold model and aggregate budget constant
   where supported; report model/topology confounding otherwise.
4. Inject a disconnected stream, duplicate required-action delivery, unavailable
   function handler, cancellation, and correction during a long session. Require
   correct recovery, preserved evidence, and no repeated business operation.
5. Promote only if quality, context continuity, latency, or operator burden
   improves enough to justify integration and hosting dependence. A useful
   managed child could occupy one LangGraph node; it need not own the entire KBA
   graph. Preserve direct and non-graph modes as independently tested choices.

## Compatibility and unresolved questions

The reviewed local OpenAI client 3.12.0 does not contain a `resources/beta/agents`
module. Therefore, the new API is not established as callable through this pinned
client. Test a newer compatible client in a separate environment before changing
KBA's shared dependency baseline for this purpose. The quickstart requires
`api.agents.read`, `api.agents.write`, and `api.responses.write`; account/key access
has not been tested. No session, vault, sandbox, or managed agent was created.
[Quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart)

Before implementation, verify supported models and output-schema behavior,
compaction fidelity for exact source/approval identities, enforceable aggregate
spend limits, session retention/export/deletion, and required-action retry
semantics. Marketing customer improvements are not KBA acceptance evidence.
