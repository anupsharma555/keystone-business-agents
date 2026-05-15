# Keystone Business Agents Guide

## Project Model

This repository is an OpenAI Agents SDK project, not a plain scripting project. Implement Keystone business capabilities as SDK agents with prompt files, structured outputs, explicit tools, and dry-run-safe execution.

For non-trivial changes:

1. Plan the approach.
2. Implement the smallest maintainable change.
3. Verify correctness with tests or a clear validation plan.

Keep final responses concise. Avoid distracting implementation metadata. If file references are useful, group them at the end.

## Agent Architecture

Every Keystone agent must:

- Expose a `build_*_agent()` function that returns an SDK `Agent`.
- Load instructions from markdown prompt files. Do not embed long prompts directly in Python modules.
- Receive shared prompt context through `compose_instructions()`, including the
  shared memory policy and writing style policy.
- Use Pydantic structured output models.
- Use explicit tool wrappers. Agent modules compose tools; tool modules own integration boundaries.
- Support dry-run fixture mode for development and tests.
- Require an explicit flag for every live integration.
- Have an `AgentSpec` registry entry with builder, schema, prompts, tools,
  live flags, eval or validation paths, handoff description, and safety notes.

The registry in `src/keystone_agents/agent_registry.py` is the canonical agent
card source for navigation, CLI inspection, and extension tests. Keep registry
metadata current when adding prompts, tools, schemas, live flags, or eval
coverage.

## Tool And Search Architecture

Tool modules own provider boundaries. Agent modules may compose tools from
`src/keystone_agents/tools/`, but they should not import provider SDKs or make
network calls directly.

Search-heavy agents use the shared `SearchProvider` contract. Current provider
implementations are:

- `dry-run`: default, no network calls.
- `searxng`: broad-recall live search when explicitly live-enabled.
- `agents-web-search`: OpenAI Agents SDK hosted web search, enabled as a capped
  parallel lane beside SearXNG for default live research.
- `serper`: precision-oriented live search when explicitly selected.
- `firecrawl`: explicit live search provider when configured with
  `FIRECRAWL_API_KEY`.
- `tavily`: optional configured deepening provider with local credit tracking.

The SDK `search_web` tool is inert unless live mode, non-dry-run mode, and live
research are enabled. With no `SEARCH_PROVIDER` override, it resolves to
SearXNG plus a capped Agents hosted web-search parallel lane. Serper remains
available through explicit provider selection, not as an automatic fallback.
Planner agents may set live-search intent and constraints, but provider
selection stays in the shared Python retrieval policy.

Website extraction is a separate live-gated path. Use Trafilatura by default for
selected company pages, or Firecrawl when `KEYSTONE_WEBSITE_EXTRACTOR=firecrawl`.
`KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK` may switch between `trafilatura` and
`firecrawl` when the primary extractor fails or returns no usable claims.

Apify and Browserless wrappers remain placeholder/dry-run boundaries. Do not
document or implement them as live production paths until a reviewed provider
adapter, explicit live flags, credential checks, tests, and source-attribution
rules exist.

## WorkItem Context Packs

Stateful WorkItem execution uses typed context packs as the contract between
orchestration, deterministic gates, and specialist agents. Do not pass loose
hand-built JSON summaries to specialists when a WorkItem context pack is
available.

Context packs must be derived from WorkItem state and related local references:

- target, route, request text, and next action
- approved facts and source refs
- selected artifact refs and retrieval metadata
- blockers and approval gates
- recipient, thread, objective, or channel metadata where relevant

Use the four specialist packs:

- `ResearchContextPack`
- `OpportunityContextPack`
- `OutreachContextPack`
- `GmailContextPack`

Python gates are authoritative. The orchestrator may recommend routing,
clarification, more research, or approval, but it must not bypass these gates:

- outreach recipient readiness
- approved claims readiness
- source sufficiency
- research sufficiency
- opportunity objective clarity
- Gmail thread readiness
- external-use approval readiness

Keep legacy context helpers as compatibility shims only if needed; new code
should prefer `build_context_pack_for_route()` or the route-specific typed pack
builders.

## Runtime Model Policy

Keystone supports per-agent runtime model configuration. Keep model selection
centralized in the model provider helpers and agent builders; do not hardcode
model names inside prompts, tools, or CLI branches.

- The Orchestrator is the control plane. It defaults to `gpt-5.4-mini` through
  `KEYSTONE_ORCHESTRATOR_MODEL` for hybrid LLM/deterministic review. Python
  remains authoritative for safety gates, approvals, and side-effect blocking.
- Business Research Analyst and Opportunity Scout default to OpenAI-compatible
  models because source synthesis, routing, trace handling, and SDK handoffs
  should stay on the same audited provider path unless explicitly changed.
- OpenAI-backed defaults use `gpt-5.4-mini` unless explicitly overridden. Gmail
  Triage and Outreach Composer default to Gemini Flash through Google's direct
  OpenAI-compatible Gemini endpoint. The runtime still uses the OpenAI Agents
  SDK provider path with Chat Completions compatibility; it does not use a
  separate Gemini SDK adapter.
  LiteLLM remains an optional external gateway override through
  `LITELLM_BASE_URL` or agent-specific base URLs. Do not require or import the
  Python `litellm` package in this repo unless a future dependency review proves
  it is compatible with the pinned OpenAI Agents SDK baseline.
- Explicit builder arguments override agent-specific environment variables.
  Agent-specific variables override the global `MODEL_PROVIDER` and
  `OPENAI_MODEL` settings.
- Live model execution must fail before a model call when the selected provider
  lacks required credentials or gateway configuration.
- Live SDK runs can hit organization/project model rate limits, especially TPM
  limits on large structured prompts. Treat HTTP 429 as a recoverable provider
  throttle: wait for the reported reset or retry-after window, then retry with
  bounded exponential backoff and jitter. Do not hot-loop retries because failed
  requests can still count against per-minute limits.
- Prefer reducing the prompt and output budget before retrying large eval runs:
  pass compact source-backed facts instead of full nested profiles, cap expected
  output size when supported, and use a smaller approved model such as a mini or
  nano variant for smoke/eval runs when quality requirements allow. Request a
  higher OpenAI limit only when compact prompts and reasonable backoff are not
  enough.

## Output Formatting Model

Agent final outputs are structured data first. Use Pydantic schemas as the
contract between agents, tools, storage, tests, and renderers. Do not rely on
free-form model markdown as the canonical output shape.

Human-facing narrative should be LLM-synthesized from bounded structured inputs
whenever live or local SDK synthesis is explicitly requested, including company
research summaries, opportunity summaries, Gmail triage notes, outreach drafts,
approval rationales, and recommendations. Deterministic renderers still own the
final Slack, Markdown, report, dashboard, and CLI layout, and Python remains
authoritative for schema validation, safety gates, source attribution,
approval/no-send rules, and side-effect blocking. The LLM may vary copy inside
bounded fields such as `draft_reply`, `email_body`, `linkedin_note`, summaries,
rationales, and recommendations, but not the canonical output shape.

Outbound email fields must be plain text, draft-only, approval-gated, and source
backed. Approved aggregate email style profiles may guide greetings, paragraph
shape, preferred phrases, CTA style, and signoffs. A style profile may specify a
personal signoff such as `Sincerely,\nAnup`; that affects draft text only and
must never weaken approval, no-send, length, PHI, or unsupported-claim rules.

## Codex Agent Mention Shorthand

When the operator sends a Codex message that starts with `@KNI`, treat it as a
request to run the local natural-language agent CLI. Prepend the repo-local
runner and pass the mention through unchanged:

```bash
.venv/bin/python scripts/ask_agent.py @KNI orchestrator agent "find 5 behavioral health AI companies"
```

For example, a Codex message like:

```text
@KNI business research analyst "research Lindus Health"
```

should be executed as:

```bash
.venv/bin/python scripts/ask_agent.py @KNI business research analyst "research Lindus Health"
```

Default behavior remains dry-run-safe. In live-test/full-live environments, the
CLI auto-enables live SDK model execution for explicit named-agent mentions; use
`--no-live-sdk` when the operator asks to force dry-run / WorkItem handling.
This shorthand must never imply permission to send, publish, schedule, post to
Slack, create Gmail drafts, or write externally.

## Safety Rules

- No external email may be sent automatically.
- Outbound email, LinkedIn, Slack, CRM, or other outbound copy defaults to draft-only behavior.
- Human approval is required before outbound copy is sent, published, scheduled, or handed to a live integration.
- Do not process PHI or patient-specific information.
- Flag legal, financial, security, and contractual content for human review.
- Company, opportunity, local collection, and broader research must include source attribution.
- Store API keys, tokens, credentials, and secrets only in environment variables.
- Logs, traces, exceptions, fixtures, and test snapshots must not expose secrets.

## Sandbox Agent Rules

Sandbox Agents are optional workspace-review tools, not the default execution path. Use ordinary SDK agents when sanitized structured inputs are enough. Consider Sandbox Agents only when the task genuinely needs a filesystem workspace, such as mounted research folders, generated report review, document batches, local validation commands, or resumable workspace inspection.

Keep the harness and control plane outside sandbox compute. The host harness owns user intent, approvals, live-integration flags, model configuration, tracing policy, audit records, artifact review, and artifact release. The sandbox may inspect scoped files and create draft artifacts, but it must not trigger email, Slack, CRM, LinkedIn, scheduling, publication, or other live side effects.

For sensitive repositories or workspaces:

- Stage only the minimum files needed into a scoped temporary folder before mounting.
- Never mount home, root, broad unrelated workspaces, credential folders, `.ssh`, `.aws`, `.gnupg`, `.config`, `.env`, OAuth token files, or API credential files.
- Do not place secrets, PHI, patient-specific content, private customer notes, or credentials in prompts, manifests, logs, snapshots, or artifacts.
- Mount folders through explicit `Manifest` and `LocalDir` entries with workspace-relative paths.
- Require generated sandbox outputs to land under `artifacts/` and treat them as drafts.
- Review and redact artifacts in the host harness before moving them out of the sandbox or using them downstream.
- Keep sandbox execution optional for tests through import guards; tests must pass when Sandbox SDK classes are unavailable.
- Use `run_sandbox_workspace_review(..., execute=False)` for setup previews. Actual sandbox execution requires `execute=True` plus either `live=True` for credential-gated SDK execution or an injected fake/local runner in tests.

See `docs/SANDBOX_AGENTS.md` for the fuller setup plan and Unix-local scaffolding shape.

## Development Defaults

- Develop dry-run first with deterministic fixtures.
- Do not implement live integrations unless explicitly requested.
- Do not invent APIs, endpoints, credentials, schemas, or production behavior.
- Add or update tests when changing agents, prompts, tools, schemas, safety logic, or outbound workflows.

## Agent Improvement Test Pack

Use `docs/AGENT_IMPROVEMENT_TEST_PACK.md` as the standing backlog for deciding
whether Opportunity Scout, Business Research Analyst, Gmail Triage, Outreach
Composer, or cross-agent collaboration needs improvement. When iterating on an
agent, map the relevant pack cases to existing coverage first, then add the
smallest useful pytest fixture, static eval, local JSONL eval, or documented
manual validation path before changing behavior.

Do not treat a pack item as complete only because the document exists. A case is
complete only when it has passing automated coverage or an explicit validation
path, and any fix preserves dry-run defaults, approval gates, source
attribution, no-PHI handling, and no-send behavior.
