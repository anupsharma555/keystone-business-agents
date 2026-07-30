# Agent Cost And Prompt Cache Implementation Queue

This queue turns the Slack-thread Agent SDK cost audit into implementation tasks.
The objective is to maximize cached input tokens for repeated Slack follow-ups
while preserving deterministic safety gates, dry-run defaults, structured
outputs, source attribution, and agent quality.

## 2026-07-27 Execution-Performance Checkpoint

- The manual request planner now uses a dedicated compact prompt profile rather
  than the full shared memory, company, renderer, and writing prompt stack.
- OpenAI requests use a stable privacy-scoped prompt-cache key derived from
  static execution structure rather than request, thread, session, or provider
  content.
- An advisory exact planner-decision cache can reuse eligible plans across
  identical asks while bypassing temporal, sensitive, mutating, approval,
  clarification, outreach, durable multi-owner, and unresolved-context work.
- Redacted per-turn/per-attempt telemetry now projects compact stage timing into
  direct runs, Orchestrator preflight, WorkItem events, trace summaries, and
  local audit reports without entering normal Slack answer copy.
- A content-free CLI output observer now measures the first delegated write and
  the post-flush final boundary without changing stdout or stderr. Direct runs
  persist the compact projection internally; WorkItems record a dedicated
  entrypoint timing event. Slack-visible timing remains owned by the parent
  Slack bridge.
- The first request-scoped fast-read adoption reuses Gmail and Google Workspace
  provider clients only within a bounded model attempt. Gmail plus core Google
  Workspace Drive/Docs reads now consume read-call budgets and emit
  content-free receipts. They do not cache provider results across requests or
  weaken provider gates.
- WorkItem and LangGraph lifecycle coverage proves one request-scoped Gmail
  client and snapshot are reused and then cleared. Calendar adoption remains a
  later incremental slice.
- Canonical research plans now select `FAST`, `BALANCED`, or `DEEP`, and
  multi-target research characterizes an explicit anchor company before
  discovering comparison targets. Direct research wrappers use the same plan,
  and query planning cannot expand FAST or BALANCED result ceilings to DEEP.
- The reconciled checkpoint passes 4,995 tests with one intentional skip,
  repository-wide Ruff, and `git diff --check`. Live Slack/provider latency and
  paid-model cache behavior remain the next acceptance boundary.
- The first paid-model probes confirmed stable-key cacheability and exact
  planner-decision reuse, but did not pass user-answer acceptance. One repeated
  Google Workspace plan reported about 95% cached input before a provider-scope
  reconciliation defect removed Drive tools; one repeated public-research plan
  reused the exact planner decision before a direct-route defect omitted the
  competitor set. Both defects are fixed offline and await post-fix live proof.
- A zero-call degraded replay now preserves Business Research ownership, the
  explicit comparison anchor, the requested competitor count, the
  multi-target artifact, and DEEP budgets even under a conservative Slack
  transport profile.

See `docs/EXECUTION_PERFORMANCE.md` for the current contract.

## Principles

- Keep the static prefix stable: repo/developer guidance, shared Keystone
  context, safety policy, agent prompt, tool definitions, output schemas, and
  static examples.
- Keep dynamic data in the suffix: runtime metadata, Slack ids, thread content,
  latest operator follow-up, retrieved sources, tool outputs, and run-specific
  WorkItem state.
- Prefer exact deterministic rendering over semantic summaries when the goal is
  provider prompt-prefix caching.
- Reuse a stable thread-specific cache/session grouping for Slack follow-ups.
- Instrument before optimizing so cache hit rate is observable.

## Queue

### P0 - Usage Observability - Done

- Preserve `input_tokens`, `cached_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`, and `total_tokens` in local audit storage.
- Add `cache_hit_rate = cached_input_tokens / input_tokens`.
- Record whether the SDK supplied/generated a prompt cache key, without storing
  the raw key.
- Render cache hit rate in local reports and Slack-safe summaries.

Acceptance:
- Local `agent_runs.output_json` keeps numeric token fields unredacted.
- Reports show cached input and cache hit rate.
- Tests prove secret redaction still redacts real credentials.

Status:
- Implemented SDK usage `cache_hit_rate`, prompt-cache-key presence/hash
  metadata, local storage redaction allowlist for non-secret token counters, and
  report rendering for cache hit rate.
- Covered by focused SDK usage and storage redaction tests.

### P1 - Slack Thread Append-Only Prompt Shape - Done

- Sort and dedupe Slack thread messages by Slack `ts` before writing context.
- Preserve selected/root message plus prior replies in deterministic order.
- Render a compact transcript where prior thread messages come before the
  latest modal/operator follow-up.
- Store the transcript under Slack context metadata for specialist and final
  response prompts.
- Keep Slack timestamps, channel ids, permalinks, run ids, and warnings in the
  dynamic suffix, not in prompt files or static instructions.

Acceptance:
- Reordered incoming Slack thread payloads produce identical stored transcript
  order.
- A follow-up appends after the same prior thread text.
- Existing no-send and approval gates still pass tests.

Status:
- Implemented Slack message sort/dedupe by `ts`.
- Added deterministic Slack `thread_transcript`, `latest_user_follow_up`, and
  `prompt_context_layout` metadata to WorkItem Slack context.
- Covered by Slack action tests for ordering and transcript placement.

### P1 - Thread-Specific Cache Grouping - Partially Done

- Reuse one stable Slack-thread SDK session/cache grouping across manual planner,
  Orchestrator preflight, specialist execution, review, and final synthesis when
  live SDK execution is enabled.
- Use thread-specific grouping rather than broad agent-wide grouping:
  `keystone:{agent_name}:slack-thread:{thread_ts}` conceptually, or the SDK
  session-derived equivalent already used locally.
- Store only hashes/metadata for cache keys in audit output.

Acceptance:
- Slack live runs derive the same session group from team/channel/thread.
- Preflight and specialist calls can share the thread grouping.
- No raw Slack text or secret values are embedded in cache-key logs.

Status:
- Implemented thread-derived SDK session reuse for live Slack manual-planner
  preflight.
- Specialist and final response synthesis use context-file derived Slack
  sessions for selected-message and history-context Slack files.
- Added an explicit SDK usage test for generated prompt-cache-key metadata,
  recording only key presence and a short hash.
- Remaining: verify Orchestrator review and final response synthesis always
  receive the same live Slack session in every manager-loop path.

### P2 - Static Prefix Stability Audit - Done

- Add a regression check that `compose_instructions()` output for each agent is
  deterministic.
- Check tool names are emitted in stable order for each agent builder.
- Check structured output schemas are stable for unchanged code.
- Document intentional dynamic tool injection points, especially optional
  Orchestrator specialist-as-tool configuration.

Acceptance:
- A test or diagnostic command reports stable prompt/tool/schema fingerprints.
- Optional dynamic tool configuration is explicit and not accidental.

Status:
- Added registry-level static-prefix fingerprints for every registered agent:
  composed instructions, ordered tool names, and output schema JSON.
- Test builds each agent twice to prove deterministic fingerprints and tool
  ordering.
- Optional Orchestrator specialist-as-tool injection remains explicit and
  disabled by default.

### P2 - Global SDK Cache Defaults - Done

- Apply cache/cost telemetry defaults in the centralized SDK agent builder, not
  only in Slack-specific execution paths.
- Request SDK usage details by default with `KEYSTONE_SDK_INCLUDE_USAGE=true`.
- Request provider prompt-cache retention by default with
  `KEYSTONE_SDK_PROMPT_CACHE_RETENTION=24h` when the installed Agents SDK/model
  path supports it.
- Preserve explicit model settings supplied by a caller.

Acceptance:
- Every registered Keystone agent built through `build_sdk_agent()` carries
  `include_usage=True` and `prompt_cache_retention="24h"` by default.
- Operators can disable usage capture with `KEYSTONE_SDK_INCLUDE_USAGE=false`.
- Operators can disable prompt-cache retention with
  `KEYSTONE_SDK_PROMPT_CACHE_RETENTION=off` or use `in_memory` for shorter
  retention.

Status:
- Implemented in `src/keystone_agents/sdk.py`, so Orchestrator, specialist
  agents, final response synthesis, hosted web-search fallback synthesis, and
  HTML review share the same cache-friendly SDK settings.
- Added registry tests proving registered agents inherit the defaults.

### P2 - Runtime Model Cost Baseline - Done

- Set the default OpenAI runtime model for Keystone agents to `gpt-5.4-mini`
  so restarted live runners use the best available mini model while retaining
  the same audited OpenAI Agents SDK path.
- Update local `.env`, `.env.example`, and model-provider constants for the
  global OpenAI model plus Orchestrator, Gmail Triage, Business Research
  Analyst, Opportunity Scout, Outreach Composer, and Chief of Staff.

Acceptance:
- `get_model_config()` defaults to `gpt-5.4-mini` with no model env overrides.
- `get_runtime_agent_model_config()` returns `gpt-5.4-mini` for each registered
  OpenAI-backed agent by default.
- A restarted Slack runner should report `model=gpt-5.4-mini` in future SDK usage
  and cost diagnostics unless a shell/launchd environment explicitly overrides
  it.

### P2 - Per-Run Request Cache Diagnostics - Done

- Record audit-safe fingerprints for every centralized SDK run:
  instructions hash, ordered tool-name hash, output-schema hash, combined static
  prefix hash, dynamic prompt hash, dynamic prompt size, and whether an SDK
  session was attached.
- Store this outside token usage so cache diagnostics do not get confused with
  provider-reported billing fields.
- Do not store raw prompts, raw instructions, raw session ids, raw Slack ids, or
  raw tool schemas in the request-cache metadata.

Acceptance:
- Saved SDK agent runs include `_sdk_request_cache`.
- CLI SDK synthesis payloads include `request_cache`.
- Fingerprints are stable enough to compare repeated runs and identify whether
  low cache hit rate came from static-prefix drift or dynamic prompt changes.

Status:
- Implemented in the centralized `run_typed_sdk_agent()` path, so Gmail,
  Business Research, Opportunity Scout, Outreach Composer, Chief of Staff,
  Orchestrator planner/review calls, and WorkItem manager-loop specialist calls
  share the same diagnostics.
- WorkItem `workflow_sdk_usage` events now preserve SDK request-cache
  fingerprints, dynamic prompt size, and SDK session hash/scope/source for local
  post-run comparisons.
- Routed Orchestrator LLM review/routing, optional HTML review, and hosted
  Agents web-search fallback through the same cost-tracked typed SDK wrapper.
- Added architecture guard tests to prevent agent/tool modules from bypassing
  the centralized SDK cost/cache instrumentation.

### P2 - Retrieved Context Placement - Done

- Confirm retrieved search results, local context snippets, and tool outputs are
  added only after stable instructions/tools/schema.
- Keep retrieved source bundles compact and sorted by deterministic ranking keys.
- Avoid inserting retrieved context into prompt files or agent instructions.

Acceptance:
- Specialist typed inputs include retrieved context only in runtime prompt
  payloads.
- Source bundles have deterministic serialization for identical inputs.

Status:
- Added regression tests proving run-specific research context appears in typed
  runtime prompts, not agent instructions.
- Added deterministic source-bundle serialization coverage for identical source
  inputs.

### P2 - Slack Context Bounding - Done

- Keep Slack context thread-first rather than channel-wide by default.
- Include selected Slack thread messages in deterministic order, capped to the
  most recent 7 days plus the selected message.
- Treat broader Slack history digests as exceptional dynamic context; compact and
  annotate them with a maximum 7-day policy.

Acceptance:
- Selected Slack context does not include whole-channel history.
- Thread messages older than the recent context window are dropped unless they
  are the explicitly selected message.
- Slack history context metadata records the bounded lookback policy.

Status:
- Implemented selected-thread recent-window filtering and explicit metadata.
- Reduced broad Slack history prompt text cap and capped history lookback
  metadata at 7 days.
- Covered by Slack action tests for recent-window selection and history context
  policy.

### P3 - Cost Reporting And Budget Feedback - Done

- Add per-run cost component reporting:
  uncached input, cached input, output, and reasoning output when available.
- Add a low-noise warning when cache hit rate is unexpectedly low on a Slack
  follow-up with prior thread context.
- Keep budget enforcement based on actual estimated total cost, not cache rate
  alone.

Acceptance:
- Reports distinguish cache miss from output-token-driven cost.
- Budget guards remain conservative and side-effect safe.

Status:
- Reports now surface cached/uncached/output cost components and billable token
  buckets.
- The local operator dashboard now includes both aggregate SDK cost/cache
  totals and a per-agent breakdown so expensive agents and low-cache agents are
  visible without inspecting raw audit rows.
- Request-cache diagnostics now include an audit-safe SDK session hash when a
  local session is attached, which lets repeated Slack-thread runs be grouped
  without storing raw Slack team/channel/thread identifiers.
- Added an operator-supplied OpenAI Platform actual-vs-estimated comparison
  helper and report rendering shape.
- Added a repeated-run cache experiment summarizer for comparing first vs repeat
  cache hit rate and estimated cost.
- Added a local comparison CLI for saved SDK payloads or `agent_runs` rows:
  `scripts/compare_agent_run_costs.py`.
- The comparison CLI can list recent audit-safe SDK session groups to find the
  first/follow-up run ids for a Slack thread experiment without exposing raw
  Slack identifiers.
- Operator-provided OpenAI Platform actual costs can be persisted back onto
  selected `agent_runs` as an `estimate_vs_actual` comparison for later reports.
- Added a low-cache note for Slack follow-up payloads that should benefit from
  caching but show a low cache hit rate.
- Added Slack cost controls for normal thread-triggered research:
  no automatic manager-loop repair unless deep/more research is explicit, no
  contact enrichment unless contact/outreach intent is explicit, a reduced
  current-research query set, WorkItem research reuse for ordinary follow-ups,
  and hosted Agents web search as capped backup/deepening rather than parallel
  fanout.
- Applied Slack cost controls to generic CLI WorkItem
  runs when they carry a Slack selected-message or history context file, or
  continue an existing Slack-context WorkItem. This covers socket/app-mention
  runners that invoke `keystone ask` or `work-items advance` instead of the
  Slack modal handler directly.
- Added a workflow-runner backstop that detects Slack context at execution time
  and normalizes otherwise-standard WorkItem requests to the conservative Slack
  profile before retrieval, repair decisions, contact enrichment, and hosted web
  search are evaluated.
- Added a Business Agents-side Slack bridge environment fallback so generic
  Slack mentions that omit `--context-file` still receive conservative cost
  controls after the Slack socket is restarted onto this code.
- Added WorkItem-level retrieval and final-synthesis usage events so future
  Slack runs can be compared even when the charge-producing SDK calls do not
  create ordinary `agent_runs` rows.

## Validation Checklist

- `python3 -m py_compile` for touched Python modules.
- Focused tests for Slack context ordering and WorkItem metadata.
- Focused tests for SDK usage extraction and redaction.
- Existing Slack action tests for no-send and provenance behavior.
- Manual dry-run Slack action fixture with reordered thread messages.

## Repeated Slack Thread Experiment

Use this only in a no-side-effect live SDK test path.

1. Run the same selected Slack thread once and capture local `usage`, `cost`,
   `model`, run id, and prompt-cache metadata from the agent-run audit output.
2. Append one follow-up in the same Slack thread and run again with the same
   thread-derived SDK session.
3. Compare:
   - `cache_hit_rate = cached_input_tokens / input_tokens`
   - uncached input, cached input, output, and reasoning tokens
   - `cost.components_usd`
   - `workflow_retrieval_usage.aggregate_usage`
   - `workflow_sdk_usage.usage` and `workflow_sdk_usage.cost`
   - OpenAI Platform actual cost copied by the operator for the same run/window
4. Expected result: the second run should have materially higher cached input
   tokens if the stable prefix and prior Slack thread text are unchanged and the
   new follow-up is appended.
5. For normal Slack research, also expect fewer outer SearXNG requests than the
   prior 41-query broad pass, zero contact-candidate artifact unless requested,
   and hosted web-search usage only when quality gates trigger backup/deepening.

Local comparison command examples:

```bash
.venv/bin/python scripts/compare_agent_run_costs.py \
  --database-url sqlite:///path/to/keystone.db \
  --list-sessions \
  --limit 10 \
  --min-session-runs 2 \
  --markdown
```

```bash
.venv/bin/python scripts/compare_agent_run_costs.py \
  --database-url sqlite:///path/to/keystone.db \
  --run-id FIRST_AGENT_RUN_ID \
  --run-id REPEAT_AGENT_RUN_ID \
  --actual-usd 0.16 \
  --actual-usd 0.06 \
  --markdown
```

```bash
.venv/bin/python scripts/compare_agent_run_costs.py \
  --database-url sqlite:///path/to/keystone.db \
  --latest-repeated-session \
  --limit 2 \
  --markdown
```

```bash
.venv/bin/python scripts/compare_agent_run_costs.py \
  --database-url sqlite:///path/to/keystone.db \
  --same-session-as-run-id REPEAT_AGENT_RUN_ID \
  --limit 2 \
  --markdown
```

```bash
.venv/bin/python scripts/compare_agent_run_costs.py \
  --database-url sqlite:///path/to/keystone.db \
  --same-session-as-run-id REPEAT_AGENT_RUN_ID \
  --limit 2 \
  --actual-usd 0.16 \
  --actual-usd 0.06 \
  --actual-reference-id OPENAI_PLATFORM_WINDOW_OR_REQUEST_ID \
  --save-actuals \
  --markdown
```

```bash
.venv/bin/python scripts/compare_agent_run_costs.py \
  --payload-file artifacts/first-sdk-run.json \
  --payload-file artifacts/repeat-sdk-run.json
```

For Slack WorkItem runs, use workflow-level events because retrieval and final
response synthesis may create platform cost without corresponding `agent_runs`
rows:

```bash
.venv/bin/python scripts/compare_agent_run_costs.py \
  --database-url sqlite:///keystone_agents.db \
  --latest-work-item-cost \
  --actual-usd 0.16 \
  --actual-reference-id OPENAI_PLATFORM_WINDOW_OR_REQUEST_ID \
  --markdown
```

Expected Slack cost-control checks:

- `advance_started.metadata.cost_profile` should be one of the route-aware
  Slack profiles for normal Slack-triggered runs, including generic Slack
  bridge `ask` invocations that do not pass a Slack context file.
- `advance_started.metadata.include_contact_enrichment` should be `false`
  unless the user explicitly asks for contact, email, LinkedIn, or outreach.
- `advance_started.metadata.allow_manager_loop_repair` should be `false`
  unless the user explicitly asks for deep/more research, run again, contact, or
  outreach.
- `workflow_retrieval_usage.query_count` should be materially below the prior
  41-query broad pass for normal company research.
- `workflow_retrieval_usage.provider_usage["agents-web-search"].requests_succeeded`
  should be 0 unless SearXNG/trafilatura quality gates triggered backup, and
  should normally be at most 1.
- `manager_loop_efficiency.repair_count` should be 0 unless the Slack ask was
  explicitly more/deep research, run again, or contact-focused.
- `workflow_sdk_usage.usage.cache_hit_rate` should be present when the live SDK
  returns token usage.

For exact platform comparison, attach operator-provided actuals under:

```json
{
  "cost": {
    "estimate_vs_actual": {
      "available": true,
      "source": "operator_openai_platform",
      "reference_id": "openai-platform-run-or-window-id",
      "estimated_usd": 0.16,
      "actual_usd": 0.15,
      "delta_usd": 0.01,
      "actual_minus_estimate_usd": -0.01,
      "estimate_coverage_rate": 1.0667,
      "delta_percent_of_actual": 6.67
    }
  }
}
```

### 2026-05-27 Slack Cost Regression Notes

Two Spring Health Slack tests exposed the relevant cost failure mode:

- 12:15 ET run `wi_1f9559a96f1a40138c562da506db9c6f`: actual platform cost
  was `$0.17`; local estimate was `$0.082639`; estimate coverage was `48.61%`.
- 12:41 ET run `wi_3782f76408374032a18fb1b8c95994ba`: actual platform cost
  was `$0.36`; local estimate was `$0.227807`; estimate coverage was `63.28%`.

Both runs used the wrong execution shape for normal Slack research:

- `cost_profile=standard`
- `hosted_web_search_max_calls=null`
- `include_contact_enrichment=true`
- `allow_manager_loop_repair=true`
- `repair_count=1`
- `workflow_retrieval_usage.query_count=123`
- hosted Agents web-search calls `=6`

The 12:41 run cost more because the priced retrieval and final-synthesis model
work was materially more expensive, not because the Slack prompt explicitly
selected a larger model. The latest run recorded regular `gpt-5.4` for final
synthesis. The older 12:15 run was not yet recording provider/model on
workflow SDK usage, so compare by observed local cost components, not by
assuming both runs used identical model pricing.

Implemented follow-up:

- Added a workflow-runner fallback that treats KNI Slack bridge environment
  hints as Slack-origin when the generic `ask` command omits `--context-file`.
- Switched the default OpenAI runtime model back to `gpt-5.4-mini` after the
  12:41 run confirmed regular `gpt-5.4` did not reduce observed platform cost
  enough for Slack research runs.
- Restarted the Slack socket after the fallback patch.
- Added regression coverage for the exact generic Slack bridge path with live
  search/SDK enabled and no context file. The test verifies
  route-aware Slack cost profiles, hosted caps, no contact enrichment, no
  repair, and reduced query fanout.
- Added WorkItem cost-summary diagnosis for Slack cost-controlled runs that still
  use a non-mini OpenAI model, so a regular `gpt-5.4` or larger-model fallback
  is visible in the next cost report.

Next Slack test pass condition:

- The first `advance_started` event for the new WorkItem must show one of the
  Slack cost-controlled profiles:
  `slack_context_light`, `slack_manager_balanced`,
  `slack_research_balanced`, `slack_opportunity_balanced`, or
  `slack_opportunity_deep`, or `slack_research_deep`.
- The WorkItem SDK section should show `Models: gpt-5.4-mini`; a non-mini
  OpenAI model should now appear as a diagnosis warning.
- If it does not, inspect the child process environment and the Slack bridge
  command before comparing model costs.

## Current Highest-Impact Next Step

Run one fresh live, no-side-effect Slack-thread experiment after the socket
restart and first verify that the route-appropriate Slack cost profile was
applied. Only after that compare local `cache_hit_rate`, estimated cost
components, and the OpenAI Platform actual shown by the operator for the same
time window/request.

## 2026-05-27 12:59 Slack Retest

WorkItem `wi_38965c2371d340fb8b41067e0bbe43c3` verified the intended runtime
controls after restarting the Slack socket:

- `cost_profile=slack_conservative` at the time of the test. Follow-up work
  replaced this single profile with route-aware Slack cost profiles:
  context-light for Gmail/Outreach, manager-balanced for Orchestrator/Chief of
  Staff, research-balanced for Business Research, opportunity-balanced for
  Opportunity Scout, and explicit research-deep for operator-requested
  deepening/contact work.
- `pricing_model=gpt-5.4-mini`
- hosted Agents web-search calls `=1`
- SearXNG requests `=10`
- manager-loop repair count `=0`
- contact enrichment disabled
- SDK input tokens `=36,020`
- cached input tokens `=15,616`
- combined SDK cache hit rate `=0.4335`
- estimated workflow USD `=$0.030415`
- operator-provided OpenAI Platform actual `=$0.07`
- local estimate coverage `=43.45%`

The initial SDK event had zero cached input, consistent with the first run after
the shared prompt/model-prefix change. After the follow-up usage landed, the
same WorkItem showed cached-input reuse and the WorkItem diagnosis passed. The
operator-reported OpenAI Platform actual was persisted as a
`workflow_actual_cost` event on the WorkItem. The local estimate still covers
less than half of the platform actual, so the next estimation improvement is to
account for hosted tool/platform charges or billing-window effects that are not
visible in provider token usage.

Quality note: the operator reported that partnership information was limited.
That is an expected tradeoff from the conservative Slack profile: it reduced
search fanout and disabled repair/contact enrichment. Keep this as the default
Slack posture, but consider an explicit operator-triggered deepening path when
the first pass is source-limited.

## Route-Aware Slack Cost Profiles

The Slack bridge now lets the WorkItem runner choose a profile from route and
operator intent instead of forcing the same conservative profile on every
agent:

- `slack_context_light`: Gmail Triage and Outreach Composer. No hosted web
  search by default; use selected thread/context and approved artifacts first.
- `slack_manager_balanced`: Orchestrator and Chief of Staff. Keeps planning,
  review, and universal operational questions functional with a hosted
  web-search cap of 1.
- `slack_research_balanced`: Business Research Analyst. Hosted cap 1 and up to
  12 targeted SearXNG queries for current company/topic evidence.
- `slack_opportunity_balanced`: Opportunity Scout. Hosted cap 2 to allow broader
  opportunity/source discovery.
- `slack_opportunity_deep`: Opportunity Scout for formal grant, RFP, pilot, or
  call-for-proposals searches. Hosted cap 4 and one manager repair pass allowed
  when review fails. Formal opportunity runs also enable capped source-page
  verification so Trafilatura can extract selected candidate pages, Crawl4AI can
  serve as the preferred local/heavier extraction fallback, and Firecrawl is used
  only after local extraction is insufficient or explicitly configured. Tavily
  should be reserved for bounded precision retry/deepening, not broad fanout
  across every SearXNG query. Apify and Browserless remain out of this path until
  their live adapters, safety gates, and tests are reviewed.
- `slack_research_deep`: explicit `more research`, `run again`, contact, email,
  LinkedIn, or similar deepening requests. Hosted cap 2 and manager repair
  allowed.

Specialist handoffs remain explicit. Specialists can recommend the next route,
missing context, and whether deeper paid search is justified, but Orchestrator
or the manager loop owns actual cross-agent execution, approval gates, and
cost-depth escalation.

## Cost Tracking Activation And Coverage

Cost tracking is centralized in WorkItem events and SDK payload metadata rather
than being owned by individual agent prompts. Normal saved WorkItem runs record
cost events when usage is available. Operators can also append:

```text
Also keep track of this run costs.
```

The shared cost-tracking helper detects that directive and removes it from the
task text before route selection so it does not pollute specialist prompts.
For WorkItems, the runner records `cost_tracking_requested=true` on
`advance_started`. For direct `ask --live-sdk` child-agent calls, the CLI strips
the directive before preflight and child execution, then marks the parent JSON
payload with `cost_tracking_requested=true`.

Coverage status:

- Central SDK usage and request-cache metadata applies to all registered agents
  through `run_typed_sdk_agent()` / `build_sdk_agent()`.
- A fake-model regression test now runs the direct SDK harness for Gmail,
  Business Research, Opportunity Scout, Outreach Composer, Chief of Staff, and
  Orchestrator, asserting the returned `TypedAgentRunResult` includes `usage`,
  `cost`, `budget_guard`, and `request_cache`.
- Orchestrator Slack workflow state now uses a strict-schema Slack context model
  instead of `dict[str, Any]`, keeping Orchestrator SDK output schema compatible
  with the Agents SDK strict structured-output path.
- Direct Orchestrator live-ask payloads expose the SDK `usage`, `cost`, and
  `request_cache` envelope instead of discarding it after `.output`.
- Direct Chief of Staff script JSON now includes SDK `usage`, `cost`, and
  `request_cache`, matching the other direct specialist scripts.
- Outreach Composer multi-variant SDK runs now aggregate `usage`, `cost`, and
  audit-safe `request_cache` diagnostics, including whether the static prefix,
  instructions, tool order, and output schema stayed stable across variant
  calls.
- WorkItem final response synthesis records `workflow_sdk_usage`.
- Business Research live retrieval records `workflow_retrieval_usage`.
- Opportunity Scout now records both live retrieval usage and live Opportunity
  Search Planner SDK usage.
- Orchestrator preflight/manual-planner SDK usage is now carried as audit-safe
  `sdk_usage_events` in the compact preflight payload and persisted as
  WorkItem-level `workflow_sdk_usage`, including state-only Slack follow-ups
  that do not run retrieval, a specialist LLM, or final synthesis.
- Cost summaries warn when a research/search-oriented run has SDK cost but no
  retrieval usage event, which catches under-estimated runs like the 8-cent
  Opportunity Scout test.

## 2026-05-27 16:15 Slack Outreach Retest

WorkItem `wi_4580d27cffc84d0a8a7b576e08fd4a06` was a light Outreach Composer
Slack run with one state-only follow-up:

- `cost_profile=slack_context_light`
- hosted Agents web-search calls `=0`
- SearXNG requests `=0`
- manager-loop repair count `=0`
- contact enrichment disabled
- existing research reuse enabled
- final user-facing synthesis skipped because the Outreach draft artifact was
  canonical
- operator-provided OpenAI Platform actual `=$0.04`

The local estimate was `$0` because no retrieval/final-synthesis/specialist SDK
usage events were recorded. The run metadata showed `manual_request_plan.source
= llm`, so the remaining platform cost was most likely Orchestrator
preflight/manual-planner SDK usage that happened before WorkItem execution and
was not previously persisted into workflow cost events.

Implemented follow-up:

- Added preflight SDK usage propagation from `resolve_manual_request_plan()` to
  `run_orchestrator_preflight()`.
- Preserved only audit-safe usage, cost, and request-cache fingerprints in the
  compact preflight payload.
- Recorded those events as WorkItem `workflow_sdk_usage` for both normal
  WorkItem advancement and state-only follow-ups.

Next Slack test pass condition:

- A light Slack Outreach/Gmail follow-up that only uses Orchestrator preflight
  should show at least one `workflow_sdk_usage` event for
  `manual_request_planner` instead of estimating `$0`.
