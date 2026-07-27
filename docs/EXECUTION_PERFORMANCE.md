# KBA Execution Performance

This document defines the execution-performance contract for natural `@KNI`,
direct agent, and WorkItem runs. Performance changes must preserve the same
semantic interpretation, typed outputs, deterministic safety and approval
gates, provider receipts, source attribution, and answer-first rendering.

## Execution Paths

### Compact Orchestrator planning

The manual request planner uses only its dedicated planning prompt. It does not
load shared memory, company context, renderer guidance, or writing-style
prompts. The raw operator request remains authoritative, and Python still
reconciles the plan against deterministic routing, provider, approval, and
side-effect gates.

At the current checkpoint, the compact profile is 33,106 characters versus
81,127 for the equivalent full shared-prompt profile, a 59.2% reduction. A
regression test requires the compact profile to remain no more than 60% of the
full profile while the route and safety contract checks continue to pass.

OpenAI requests receive a stable operator-scoped prompt-cache key derived from
the instruction profile, agent, model, ordered tools, and output schema. The
key excludes request text, Slack content, thread identifiers, provider data,
and secrets.

An exact planner-decision cache may reuse an eligible prior plan when the
normalized request, requested agent, operator scope, bounded context revision,
and planner profile all match. This cache is advisory: it never bypasses
deterministic reconciliation. Temporal/current, sensitive, mutating,
approval-gated, clarification-dependent, outreach, durable multi-owner, and
unresolved-context requests bypass it.

Relevant settings:

- `KEYSTONE_SDK_PROMPT_CACHE_SCOPE` defaults to
  `local-single-operator`.
- The exact planner-decision cache defaults to a six-hour lifetime and caps its
  lifetime at 24 hours.

### Request-scoped fast reads

Provider reads can share a bounded request-local context containing typed read
plans, provider clients, and safe snapshots. The kernel enforces call, item,
page, byte, deadline, and concurrency limits and records content-free
fingerprints rather than raw queries, identities, provider payloads, or tokens.

Initial adoption covers:

- Gmail read tools, which can reuse one authenticated tool/session within one
  model attempt, consume bounded read-call slots, and emit content-free read
  receipts.
- Google Workspace Drive and Docs read tools, which reuse the same service
  bundle within one matching read context, consume bounded read-call slots,
  and emit content-free read receipts.

An offline lifecycle regression now proves the same request-scoped context is
active through both WorkItem and LangGraph specialist execution, reuses one
Gmail client and one recorded snapshot, preserves the provider payload, and is
cleared when the specialist returns.

Selected work content needed to answer the request remains available to the
configured model. The fast-read kernel does not suppress that evidence. It
keeps raw provider content out of generic receipts, telemetry, cache keys, and
cross-request caches.

Outside an explicit read context, provider tools retain their prior lifecycle.
No provider permission, read/write distinction, approval gate, or receipt
requirement is removed. Cross-request provider-result caching is intentionally
out of scope.

Calendar adoption remains a later incremental slice. Its existing provider
behavior and concurrent implementation work are preserved; this checkpoint
does not claim that Calendar reads use the fast-read kernel.

### Research quality modes

Research depth is selected from the canonical request plan:

- `FAST`: narrow or explicitly quick bounded research.
- `BALANCED`: the normal live-research default.
- `DEEP`: explicit deep, broad, formal, source-intensive, or
  quality-prioritized research.

Explicit mode instructions take precedence. Sparse planner output cannot erase
an explicit deeper-research request. Provider selection remains in the shared
retrieval policy.

Company-comparison research is anchor-first: an explicitly named anchor company
is characterized before competitor discovery, does not consume the requested
competitor count, cannot be returned as its own competitor, and must satisfy the
same official/product/requested-feature evidence floor as comparison targets.
Direct specialist wrappers and WorkItem retrieval consume the same canonical
mode. Query planning may improve search wording inside a mode. FAST remains
strictly bounded; BALANCED preserves the established deeper initial-query floor
for time-sensitive company research, while DEEP alone enables the full
source-intensive budget.

## Measurement Contract

Execution telemetry uses monotonic stage spans for each turn and model attempt.
It records compact stage names, duration, status, and allowlisted scalar
metadata. Prompts, responses, tool inputs, tool outputs, provider payloads,
identifiers, and secrets are excluded from compact projections.

Internal projections may carry stage timing through direct agent runs,
Orchestrator preflight, WorkItem usage events, execution steps, trace summaries,
and audit reports. Normal Slack and CLI answers remain focused on the requested
answer; timing, route, cache, and workflow metadata are not rendered unless the
operator explicitly asks for diagnostics.

SDK completion timing means the model result was ready. End-to-end
operator-visible latency must be measured at the Slack or CLI entry adapter and
must not be inferred from SDK timing alone.

The CLI entry adapter observes delegated stdout and stderr writes without
buffering, inspecting, hashing, or changing their content. The first delegated
write marks first feedback. The final marker is recorded only after the handler
returns and both streams flush. Direct-agent timing is stored only in the
internal `agent_runs` output, while WorkItem timing is stored as a compact
`entrypoint_execution_telemetry` event. Neither projection is added to the
public result or normal answer text.

The child CLI cannot measure Slack-visible timing. The parent Slack bridge owns
the receipt time, acknowledgement or running-status post, final post/update,
and transport delay; those measurements must be correlated with the child run
using an opaque run identifier.

## Verification

The implementation is integrated on the reorganized `origin/main` ownership
surfaces. CLI timing lives in `entrypoints/cli_impl.py`, leaving the lazy public
CLI facade unchanged. Planner caching lives under `planning`, receipt
normalization under `receipts`, audit rendering under `presentation`, and
WorkItem/LangGraph lifecycle tests use the public `orchestration.stages`
boundary. WorkItem entry telemetry reuses the request's existing SQLite store;
direct runs use a bounded fallback only when no request runtime owns one.

The 2026-07-27 offline implementation checkpoint passed:

- focused contract and integration tests;
- a 1,116-test cross-system integration gate;
- the ordinary repository suite on the reorganized `origin/main` architecture:
  4,714 passed and 17 intentionally skipped;
- repository-wide Ruff checks; and
- `git diff --check`.

These checks used fixture, fake-model, and test-mode isolation. They do not
prove live provider acceptance, Slack-visible latency, or paid-model cache
behavior. Promotion still requires bounded serial live canaries whose backend
telemetry, provider evidence, Slack answer, and visible UI agree.

### Bounded live checkpoint

The first bounded live checkpoint produced useful partial evidence but did not
pass acceptance:

- A public-web DEEP research run completed in about one minute for an estimated
  `$0.015`. An exact planner decision was reused on the final attempt, and the
  specialist used a stable prompt-cache key. The output characterized the named
  anchor from source-backed evidence but omitted the requested competitor set.
  The direct entry path had collapsed multi-target research into a single-company
  brief. Multi-target competitor plans now require the durable research branch.
- A read-only KNIOps run completed in about 36 seconds for an estimated
  `$0.007` across planning and response synthesis. The compact planner reused
  about 95% of its input tokens from the stable prefix. It returned unknown
  values because reconciliation interpreted "do not search outside Google
  Drive" as a ban on Drive and removed the provider owner. Scope-confinement
  wording now preserves the named provider while still removing mutations.

Both defects have focused regression coverage, and the full offline suite above
passes after the fixes. A final zero-call replay also verified that degraded
planning preserves the Business Research owner, explicit anchor, requested
competitor count, durable multi-target artifact, and DEEP research budget. This
also closed a policy edge where natural wording such as "deeply research" could
be reduced to FAST by a conservative Slack transport profile.

Neither live attempt performed a provider write or posted to Slack. Post-fix
provider and Slack acceptance remains pending. The installed Slack worker
currently executes a different dirty worktree, so a Slack canary must not be
credited to this implementation until the runtime is deliberately aligned
without overwriting concurrent work.

### Reversible no-write canary runtime

`scripts/kba_no_write_python` is an interpreter shim for the temporary Slack
acceptance worker. It must not be used as the normal production interpreter.
The shim preserves configured model, research, and provider-read access while
it applies a stricter process-local ceiling after the Slack bridge and both
repos have loaded their environment:

- every Airtable, Workspace, Calendar, Gmail, Slack, presentation, and Zotero
  mutation, lifecycle, attachment, draft, send, and post gate is forced off;
- the KBA database, SDK session, trace, benchmark, review, and provider-usage
  paths are redirected to one explicit child directory under a system temp
  root;
- any bridge-supplied `--database-url` is replaced with the temporary database;
- every canonical `ask` receives
  `KEYSTONE_CANARY_MAX_OPENAI_REQUESTS`, and a higher bridge-supplied ceiling is
  reduced to that value;
- only `google_workspace_context_agent` and `business_research_analyst` are
  admitted, through the exact `-m keystone_agents.cli ask` entrypoint; arbitrary
  scripts, modules, Python expressions, and other agents are rejected;
- the Workspace OAuth token is copied once into the canary directory with mode
  `0600`; token refreshes update only that staged copy and never the operator's
  original token;
- inherited Slack app/signing/action credentials are removed from the child,
  SDK trace export, sandbox execution, and Playwright are forced off, and
  Promptfoo, Playwright, and derived-presentation paths are temp-confined;
- the wrapper adds this checkout and its `src` directory to `PYTHONPATH`, so a
  cold import can prove which code is executing;
- its preview is secret-free and does not print provider credentials or
  selected Drive content.

Create a fresh state directory with `mktemp -d
/private/tmp/kba-canary-runtime.XXXXXX`, then set
`KEYSTONE_CANARY_STATE_DIR` and `KEYSTONE_CANARY_MAX_OPENAI_REQUESTS` before
using the wrapper. `--canary-preview` validates the isolation policy without
starting a model or provider call, and `--canary-import-check` performs only the
fixed import-origin check. The request value is an admission ceiling applied to
the canonical ask, not an independent network-level counter. Retries are
therefore forced to zero across the live model client, rate-limit recovery, and
structured-output repair, and Gemini fallback is disabled. The correlated run
telemetry must report the actual request count, and the canary stops if that
count exceeds the approved ceiling.

The temporary evidence directory is retained for inspection; the harness does
not delete it automatically.

The temporary Slack worker must also redirect its parent-owned state before it
starts: `WORKFLOW_STATE_DB_PATH`, `DISCOVERY_STORE_PATH`,
`KNI_BUSINESS_AGENTS_SLACK_RUN_DB_PATH`,
`KNI_BUSINESS_AGENTS_SLACK_CONTEXT_DIR`, and
`KNI_BUSINESS_AGENTS_SLACK_FILE_DIR` all belong under the same fresh canary
root. Start that worker in the foreground, wait for its polling-active log, run
only the serial canaries below, stop the foreground worker, and restore the
installed worker with its repository management script.

The KBA child cannot post to Slack directly. The temporary parent bridge still
posts the intended canary status/result replies so the operator-visible path can
be measured. Wait for each child to reach a terminal state before posting the
next canary and before stopping the worker. KBA children use their own process
groups, so stopping the foreground parent is not an emergency child-stop
mechanism. If a child must be interrupted, resolve its exact PID/process group
and terminate only that group; never use a broad process-name kill. Retain the
temporary root through evidence review, then remove it privately because it
contains the staged OAuth token.

The local gate passed 14 focused wrapper tests, a secret-free preview, a cold
import proof for `keystone_agents`, telemetry, and provider-read modules, and a
zero-call canonical Workspace ask. The synthetic bridge database argument was
rewritten successfully: only the isolated canary database was created. The full
repository gate on the reorganized architecture then passed 4,714 tests with
17 intentional skips.

### Post-fix acceptance canaries

Run these serially only after the temporary Slack runtime proves that it imports
this checkout and all provider mutation gates are disabled:

1. **Workspace fast read**
   - Ask the Google Workspace Context Agent to find and read `README.doc` in
     `KNIOps`, summarize its purpose and operating model, and make no changes.
   - Follow up in the same thread by asking how many folders are directly under
     the `KNIOps` parent.
   - Require a direct answer, a Google Workspace read receipt, no write/draft/
     send/post receipt, an end-to-end first-answer time at or below 45 seconds,
     and an eligible exact follow-up at or below 20 seconds.
2. **Anchor-first DEEP research**
   - Ask Business Research to characterize one named behavioral-health anchor,
     identify up to two closest evidence-backed competitors, use official
     product or research sources for every named company, and distinguish direct
     competitors from adjacent tools.
   - Require the anchor first, up to two non-anchor comparison targets, visible
     official source URLs, explicit limitations, DEEP mode, no external writes,
     first feedback within 10 seconds, and a final answer within the configured
     300-second DEEP deadline.

For each canary, correlate the Slack root/thread, temporary Slack run row, KBA
run or WorkItem, route, model-request count, usage/cost, provider/search
receipts, compact stage timing, and reader-visible answer. Stop after the first
shared failure, unexpected side effect, or request-ceiling breach. SDK timing
alone does not satisfy the end-to-end thresholds.
