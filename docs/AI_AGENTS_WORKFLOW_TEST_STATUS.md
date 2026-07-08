# AI Agents Workflow Test Status

Last updated: 2026-07-06

This note tracks the focused `#ai-agents-workflow` diagnostic pass for KBA named-agent runs. It is intentionally compact and excludes secrets, local absolute paths, `.env` details, and private Slack links.

## Current Position

ANU-60 is complete through the no-live/pre-live boundary: KBA local proof,
sibling `keystone-slack` fixture coverage, and the full no-live preflight are
green. It is not live-complete. Use `docs/ANU60_LIVE_SLACK_PROOF_PLAN.md` as
the current handoff packet and `docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md` as
the capture form before any additional `#ai-agents-workflow` live probe.

Current no-live evidence on 2026-07-06:

- KBA prompt/doc and Slack action contract suite passed: `.venv/bin/python -m
  pytest tests/test_prompt_contracts.py tests/test_slack_action_contract.py -q`
  returned `111 passed`.
- KBA bridge validators passed:
  `scripts/validate_slack_bridge_contract.py` and
  `scripts/validate_slack_result_rendering_examples.py`.
- ANU-60 proof-packet validator passed:
  `npm run eval:slack:anu60-proof`.
- ANU-60 no-live preflight passed:
  `npm run eval:slack:anu60-preflight`.
- KBA Slack expansion gate passed `36/36` with no live Slack/API/model/search
  calls in `artifacts/anu60_expansion_gate_after_acceptance_map.json`.
- Sibling `keystone-slack` focused bridge tests passed `8` cases covering
  direct RSS/preprints `human_summary`, conversational Business Research
  routing, company-brief metadata suppression, timeout failure wording, and
  blocked preflight rendering.

Remaining proof boundary: do not move ANU-60 to Done until fresh live
`#ai-agents-workflow` probes produce Slack permalinks, local run ids, pass/fail
evidence, answer-first visible `human_summary`, no metadata-first body, correct
blocked wording, and no sends/writes/drafts/feed refreshes.

Live Slack/API testing is paused after the latest bounded probes to control API cost. Offline checks should be preferred until the next targeted Slack probe is worth the cost.

Offline verification on 2026-06-20 covered WorkItem manager behavior and did
not add live Slack/API/model/search calls. Direct dry-run `@KNI rss context
agent` checks now verify both sides of the posting boundary: negated "do not
post to Slack" language routes to `RssContextResult`, while an affirmative
"post this message to Slack" request blocks with `block_kind=send`.

A follow-up offline guard pass on 2026-06-20 fixed four pre-live issues without
adding Slack/API/model calls: affirmative context-agent send/write requests now
plan as blocked side effects, RSS and preprints context-agent manual plans
survive the WorkItem enum boundary through Chief-owned advisory specialist
traces, live user-facing synthesis failures preserve substantive specialist
summaries instead of replacing them with deterministic fallback text, and ready
multi-target Business Research comparisons finish as `done` instead of staying
`in_progress` solely because of a review-only next action.

After the guard fixes, the same focused no-live Promptfoo-provider expansion
slice was rerun as `eval-znV-2026-06-20T23:16:20` with 36/36 passing, 0
failures, and 0 provider errors.

After the 2026-06-21 conversational RSS/preprints Chief-wrapper patch, the
same 36-case expansion/readability slice was rerun locally through the dry-run
Promptfoo provider and assertion module. It passed 36/36 cases with 0 failures
and no live Slack/API/model/search calls. The rerun covered all operating and
context routes in `promptfoo/tests/slack_agent_expansion_15.yaml`.

Four bounded live Slack probes were run after the offline guard pass, then
live testing was paused for cost control. The first Business Research/Abridge
probe reached the live Business Research path, completed, and recorded local
SDK cost telemetry, but the visible Slack answer still began with provider,
model, retrieval, and timing metadata instead of the reader-facing answer. The
second, more conversational Nabla prompt exposed a sibling Slack bridge routing
gap: "could the business research analyst..." was caught as an unsupported
slash-command-style KNI command before it reached KBA, even though KBA's own
manual planner maps that wording to `business_research_analyst`. KBA now has
focused no-live coverage for the same polite named-agent wording: it routes to
`business_research_analyst`, extracts `Nabla` as the company target, avoids the
Opportunity Scout clarification path, and renders an answer-first summary. The
third
Corti probe completed locally with a clean `script_payload.human_summary` but
did not expose that summary at the top-level CLI envelope. KBA now promotes the
child script `human_summary` to the top-level named-agent envelope. The fourth
Suki probe completed locally with both top-level `human_summary` and
`script_payload.human_summary` present, but the sibling Slack bridge still
rendered its legacy metadata-heavy Company Research block. KBA now publishes
the same answer-first Business Research summary under `human_summary`,
`slack_display_text`, `display_text`, and `summary`, and the exported Slack
business-agent contract prefers those fields before nested summaries or generic
status messages. The sibling Slack bridge should consume KBA
`business_agent_result_display_text()` or those display fields for named-agent
results before falling back to route-specific metadata formatting. The exported
rendering examples now include a Suki-shaped metadata-heavy Business Research
payload that must render `slack_display_text` before model/search/profile-link
diagnostics, so this can be verified offline before another live probe. The
remaining bridge-side work is tracked as `SLACK-BRIDGE-NAMED-001` in the
Orchestrator bridge backlog.

The focused no-live Promptfoo-provider snapshot for
`promptfoo/tests/slack_agent_expansion_15.yaml` now passes 36/36 cases with 0
provider errors. This covers all listed operating and context routes in that
pack: Orchestrator, Chief of Staff, Business Research, Opportunity Scout,
Outreach Composer, Gmail Triage, Airtable Context, Google Workspace Context,
Zotero Context, RSS Context, and Preprints Context. This is still local
dry-run/provider verification only; it does not prove live Slack/API/model cost
movement.

The full local Promptfoo JSON suite was rerun after the named-agent display
field promotion, Gmail/outreach approval wording, Chief of Staff specific
branch ordering, and Slack-facing eval heading alignment. The final offline run
`eval-RWa-2026-06-21T01:54:13` passed 102/102 cases with 0 failures and 0
provider errors. This validates KBA-side routing, summary readability, and
no-side-effect behavior locally; it still does not prove that the sibling Slack
bridge has adopted the KBA result-display helper in production.

The same focused expansion/readability gate was rerun after the latest
Business Research title/heading and planner target-type fixes as
`eval-bqu-2026-06-20T22:53:17`. It again passed 36/36 cases with 0 failures and
0 provider errors, with reported token usage 0 for the local provider run.

After the source-provided Business Research mode-neutral wording patch, the
same no-live expansion slice was rerun as `eval-YzC-2026-06-20T22:18:32` with
36/36 passing, 0 failures, and 0 provider errors.

The same no-live snapshot now runs under a stricter Slack readability invariant:
human-facing summaries must expose a visible `Answer:` section and must not
fall back to raw JSON, snake_case workflow fields, or known provider/workflow
metadata terms. This is a renderer/eval contract only; it preserves specialist
reasoning and live synthesis paths for future runs.

A later no-live `@KNI chief of staff agent` CLI probe used a diverse Baylight
Rehab physical-therapy dashboard prompt. It verified the real local manager
loop now advances `chief_of_staff -> opportunity_scout`, suppresses external
research for "use only sanitized inline context", produces two source-provided
opportunity directions, and avoids the earlier "no strong exact matches"
fallback.

Follow-up offline Slack-action checks also covered channel and thread routing.
KBA no longer invents `#ai-agents-workflow` as a fallback target for Chief of
Staff internal summary actions when source channel context is missing; it uses
the source channel when present and blocks the dry-run summary when no source
channel is available. A Slack-thread follow-up request with an explicit
`chief of staff` mention now remains on `chief_of_staff` instead of drifting to
`gmail_triage` because the text included "thread" and "follow-up".

A fresh no-live `@KNI opportunity scout agent` check used a Cedar Harbor OT
occupational-therapy dashboard prompt. The local ask path returned
`route=opportunity_scout`, `status=done`, two opportunity artifacts, no
blockers, and a human summary with `Answer`, `Detailed Summary`, and
`Review notes` sections. The summary did not leak `Business Agents`,
`Metadata`, `WorkItem`, or "no strong exact matches" language, and it stated
that only inline context was used with no external search or side effects.

A fresh no-live `@KNI business research analyst` check used a Signal Yard
Robotics warehouse-safety dashboard prompt. The local ask path returned
`route=business_research_analyst`, `status=done`, one source-provided artifact,
and no blockers. The summary preserved the company name, approved inline facts,
incident-trend dashboard context, and three diligence questions while avoiding
`Business Agents`, `Metadata`, `WorkItem`, and diagnostic-case leakage. It also
stated that no live search or external side effect was used.

Two bounded live Slack probes were then run before pausing additional live/API
testing. The Signal Yard Robotics Business Research prompt completed in Slack,
created a WorkItem, recorded live SDK/provider cost telemetry locally, and
returned a clean source-provided brief with bold `Answer`, `Detailed Summary`,
and `Useful references` sections. The Cobalt Yard Operations Chief of Staff
probe exposed a separate direct-runner gap: Slack called
`scripts/run_chief_of_staff.py`, which returned a textual recommendation to
Business Research Agent but did not execute the downstream WorkItem handoff, and
the Slack bridge added a noisy `Metadata` block from negated outreach/write
terms in the original prompt. The KBA direct runner now converts Chief
recommendations for WorkItem-capable agents into a delegated manager-loop run
and returns the downstream summary in a Chief-shaped payload; sibling Slack
bridge metadata cleanup is still needed for prompts that contain negated
outreach/write language.

A follow-up no-live `@KNI chief of staff agent` ask used a Northline Imaging
radiology scheduling dashboard prompt. It verified that the normal
`scripts/ask_agent.py` path advances `chief_of_staff -> business_research_analyst`
without live SDK or search calls, preserves the approved inline radiology
dashboard fact, separates `Answer`, `Detailed Summary`, and `Useful references`,
and no longer treats instruction text such as "Recommend the best next..." as
source evidence. The source-provided Business Research fallback was also patched
so it does not invent behavioral-health product or buyer labels when the
provided excerpt is from another domain.

A bounded live Slack probe on 2026-06-20 used a diverse Harborline Logistics
warehouse-safety dashboard prompt in `#ai-agents-workflow`. The visible Slack
status completed with `Route: business_research_analyst`, proving the Chief of
Staff direct-runner path executed the downstream handoff instead of only
recommending it. Local WorkItem evidence shows the item finished `done` on
`business_research_analyst`, route sequence `chief_of_staff ->
business_research_analyst`, `live_sdk=true`, `live_search=false`, and local
estimated SDK cost of about `$0.0607` from planner/Chief usage. The downstream
Business Research response used source-provided context rather than live search,
so this is evidence for live Chief reasoning and handoff/cost telemetry, not a
live web-research signoff. A follow-up renderer patch removed live-inappropriate
"dry-run case/eval" wording from source-provided Business Research summaries.

A no-live `@KNI preprints context agent` ask used an adolescent depression,
digital phenotyping, and wearable-sensor monitoring prompt. It verified KBA-side
dry-run routing and text-mode rendering for the preprints context agent: the CLI
now prints the context-agent `Answer`, `Detailed Summary`, and `Useful references`
sections in text mode, keeps the result read-only, and filters prompt scaffolding
words such as "externally", "concise", and "handoff" out of recurring themes.
The output remains explicit that item-level preprint titles, dates, URLs, and
source text were not read in dry-run mode.

A matching no-live `@KNI rss context agent` ask used clinical AI validation,
remote monitoring implementation, and operations dashboard governance. It
verified KBA-side dry-run routing and text-mode rendering for the RSS context
agent, with the same sectioned output contract and no external writes. The feed
topic parser now prefers specific domain phrases and avoids duplicating narrower
phrases such as "remote monitoring" when "remote monitoring implementation" is
already present.

Focused KBA-side RSS/preprints bridge-readiness checks on 2026-06-20 confirm
the exported Slack business-agent contract advertises `RssContextResult` and
`PreprintsContextResult` under `result_rendering.context_agents`, with
`human_summary` as the preferred text field. Feed context CLI tests also pass
for direct and bare aliases, and Orchestrator safety checks preserve explicit
feed-context routes while refusing affirmative Slack-post requests at the
clarification/approval gate.

KBA now also exposes a side-effect-free `business_agent_result_display_text()`
contract helper and focused tests proving `RssContextResult` and
`PreprintsContextResult` payloads resolve to sectioned `human_summary` text
instead of generic WorkItem completion messages. This does not prove the sibling
Slack bridge has adopted the helper, but it makes the expected bridge behavior
executable on the KBA side.

A follow-up no-live replay used more conversational RSS and preprints prompts
of the form "could the <context agent> take a read-only look at...". The manual
plan still selects the requested context specialist while the WorkItem route
uses the existing Chief of Staff advisory wrapper. That wrapper now produces a
specific, sectioned context-agent advisory summary with the requested focus,
read-only boundary, `Answer`, `Detailed Summary`, `Next step`, and `Review
notes` sections. It no longer falls back to unrelated OpenAI SDK source text or
generic Slack-operations clarification language.

An offline route check now covers the prepared RSS, preprints, and direct
Business Research live probes before any additional Slack/API spend. The
Business Research probe uses an explicit colon-style `@KNI business research
analyst:` mention; the planner now preserves that named-agent route instead of
falling back to Chief of Staff or Opportunity Scout heuristics, so future live
runs should still reach the specialist synthesis path with the original request.

A follow-up no-live `scripts/ask_agent.py --no-live-sdk` probe used the prepared
direct Business Research/Abridge wording through the real WorkItem manager path.
It completed as a single `business_research_analyst` step, extracted `Abridge`
as the WorkItem target despite the diagnostic-case prefix, kept send/write
actions disabled, and stopped before Opportunity Scout because no explicit
multi-step workflow was requested.

A current-state no-live rerun of the same direct Business Research/Abridge
shape exposed and fixed two final pre-live hygiene issues: the persisted
WorkItem title could still keep the diagnostic prompt prefix, and the
source-backed fallback summary used unbolded `Answer`/`Detailed Summary`
headings plus `Source evidence` instead of the cleaner Slack section contract.
The rerun now stores `Research: Abridge`, keeps the target as a `company`
rather than misclassifying `lab` from unrelated words such as `available`,
returns `route=business_research_analyst`/`status=done`, and renders bold
`Answer`, `Detailed Summary`, `Useful references`, and `Review notes` sections
without using live SDK, live search, or Slack.

A no-live `@KNI gmail triage agent` ask used a Cedar Valley Rehab
home-exercise adherence dashboard prompt. It verified that read-only inline
Gmail triage still returns structured triage evidence while the human summary
uses separated bold `Answer`, `Detailed Summary`, and `Review notes` sections.
The inline email parser now stops `Subject:` at `From:` labels, avoids repeated
no-write caveats, and exposes Gmail-specific review fields to the manager
reviewer so a useful completed triage no longer appears as a failed warning.

A no-live `@KNI zotero context agent` ask used a foundational depression and
psychiatric-diagnosis background-scan prompt. It exposed and then fixed a
dry-run fixture issue where Zotero context output used generic
behavioral-health AI validation boilerplate. The KBA dry-run path now extracts
the requested Zotero topic, keeps operation constraints such as import or
collection creation out of the topic focus, displays collection/context hints
and evidence guidance, and states that useful references require local
item-level Zotero reads.

A no-live `@KNI google workspace context agent` ask used a KNIOps onboarding and
operations SOP handoff prompt. It exposed and then fixed the same class of
dry-run fixture drift: the Workspace context output had been hard-coded around
eval artifacts. The KBA dry-run path now extracts the requested Workspace topic,
returns candidate folder/doc/sheet hints for onboarding or operations handoffs,
and states that useful references require live Workspace reads before naming or
relying on specific files.

A no-live `@KNI airtable context agent` ask used the 2026 Finance & Tax Tracker
Tax Payments / Estimated Tax Period 2 prompt. It exposed and then fixed Airtable
dry-run fixture drift where the answer defaulted to eval-tracker boilerplate.
The KBA dry-run path now keeps the finance/tax topic, target table, field hints,
record-filter guidance, and live-read boundary visible without claiming record
values were read.

A no-live Promptfoo-provider sample for `business_research_analyst` used the
MetricBridge source-provided claim-mapping case. It exposed and then fixed a
source-provided fallback issue where repeated prompt text and instructions were
mixed into the Business Research answer. The output now maps supported
PHQ-9/GAD-7 workflow claims, moderate operational evidence, and unsupported
depression-outcome claims from the provided excerpt, and the manager review now
passes without live search or SDK calls.

A broader no-live Promptfoo-provider slice for `business_research_analyst`
covered MetricBridge claim mapping, a caveated behavioral-health quality
measurement market memo, an unsupported investment-style forecast request, and
a CareNav AI labeled diligence note. All four now pass manager review without
live Slack/API/model calls. The investment-style forecast remains blocked with
sectioned safety output, and the caveated market memo avoids repeating brittle
winner/IPO phrasing while preserving the source-limited caution.

A no-live Promptfoo-provider sample for `opportunity_scout` used a
source-provided integrated-care conference comparison table case. It exposed and
then fixed a review/rendering gap where a useful source-provided table could be
treated as generic artifact metadata. The output now preserves the comparison
rows, separates bold `Answer`, `Detailed Summary`, `Useful references`, and
`Review notes` sections, remains no-external/no-write, and passes manager review
without live search or SDK calls.

A second no-live Promptfoo-provider `opportunity_scout` sample used a deliberately
broad "find good opportunities" ask with missing buyer, geography, sector, and
source context. It remains blocked, but the blocked state is now a useful
clarification result with bold `Answer`, `Detailed Summary`, and `Next step`
sections, an explicit no-fabrication/no-live-search boundary, and a passing
manager review. This preserves the distinction between expected clarification
blocks and runtime failures.

A no-live Promptfoo-provider slice for `outreach_composer` covered approved-facts
LinkedIn-note wording without approved WorkItem context, missing recipient
context, two-tone variant wording without an approved brief, and a PHI/patient
story block. All four remain correctly blocked before specialist execution or
external action. The Slack-facing summaries now use bold `Answer`,
`Detailed Summary`, and `Next step` sections and do not repeat generic approval
boilerplate or internal schema/state terms in the main answer.

A no-live Promptfoo-provider slice for `gmail_triage` covered pasted-thread
commitment extraction without selected message context, ambiguous missing-thread
identity, contract-clause review gating, and no-mutation label planning. All
four remain blocked at the expected Gmail context gate, now with bold `Answer`,
`Detailed Summary`, and `Next step` sections, explicit no-Gmail-mutation
evidence, and passing manager review. Completed inline Gmail triage behavior
remains covered separately by the Cedar Valley Rehab read-only triage test.

A no-live Promptfoo-provider slice for `orchestrator` covered finance-context
read routing, ambiguous research-plus-outreach decomposition, source-provided
vendor-table routing, and natural score-save follow-up. The source-table case
continues to route to Business Research with clean source-provided output, and
the ambiguous outreach case remains blocked at the approved-context gate. The
Chief fallback outputs for finance context and score-save follow-up now use
sectioned `Answer`, `Detailed Summary`, `Next step`, and `Review notes` output,
avoid irrelevant fixture source references, and pass manager review without
live Slack/API/model calls.

A no-live Promptfoo-provider slice for `chief_of_staff` covered an owner/action
log, an eval-gap advisory summary, a three-section executive brief, a
source-integrity block, Airtable tracker-field and Google Workspace
artifact-placement advisory plans, a Zotero evidence-collection plan, and a
combined Airtable/Workspace/Zotero evidence-packet advisory plan. All nine now
use sectioned low-metadata output with bold `Answer`, `Detailed Summary` when
applicable, `Next step`, and `Review notes` sections. Context-agent advisory
routes and context sources remain visible in the structured payload, expected
blocked cases remain blocked, and no generic OpenAI/Slack fixture references or
approval boilerplate leak into the human summaries.

The current repo registry exposes nine specialist routes:

- `gmail_triage`
- `business_research_analyst`
- `opportunity_scout`
- `outreach_composer`
- `airtable_context_agent`
- `google_workspace_context_agent`
- `zotero_context_agent`
- `rss_context_agent`
- `preprints_context_agent`

## Agent Coverage Matrix

| Agent route | Current evidence | Status | Remaining gap |
| --- | --- | --- | --- |
| `gmail_triage` | Recent live `agent_runs` success rows and Slack-visible low-metadata triage answer from inline sanitized email context. A no-live Cedar Valley Rehab prompt now returns separated `Answer`, `Detailed Summary`, and `Review notes` sections, correct inline `Subject:` parsing, no repeated no-write caveat, and a passing manager review. A no-live Promptfoo-provider slice now confirms missing selected thread/message context, ambiguous thread identity, contract-clause review gating, and label-planning requests block cleanly with sectioned `Answer`, `Detailed Summary`, and `Next step` output plus no-mutation evidence. | Passing for read-only inline triage and expected Gmail context-gate blocks. | Future live test should use a different non-Example-Health prompt before broader signoff if cost budget allows. |
| `business_research_analyst` | Recent live `agent_runs` success rows and WorkItems from direct asks and Chief of Staff handoffs. A no-live Signal Yard Robotics prompt returned a clean source-provided research brief with company facts, incident-trend dashboard context, and three diligence questions. A no-live Promptfoo-provider slice now covers MetricBridge claim mapping, caveated market memo synthesis, unsupported investment-style forecast blocking, and CareNav AI labeled diligence fields with passing manager review. The 2026-06-20 Harborline live Slack probe reached Business Research through a Chief handoff and returned a clean source-provided brief. | Passing for bounded source-provided research, labeled diligence, caveated synthesis, expected safety blocking, and Chief-to-Business-Research live handoff. | Future live test should check a non-healthcare/non-Example-Health direct company ask with visible primary source URLs when cost budget allows. |
| `opportunity_scout` | Recent live `agent_runs` success rows and later WorkItems marked `done`; earlier useful answer was incorrectly surfaced as blocked before status handling was improved. A no-live Cedar Harbor OT prompt returned two clean source-provided directions with no metadata leakage. A no-live integrated-care conference comparison sample now preserves source-provided table rows, uses separated bold sections, and passes manager review. A broad missing-scope sample now remains blocked but asks for buyer/geography/sector/source context or live-search approval with a passing manager review. | Passing for internal opportunity directions, source-provided comparison tables, and expected clarification blocks after follow-up tests. | Future live test should keep prompts diverse and confirm Slack-visible status remains `done` when the answer is useful. |
| `outreach_composer` | Recent Slack-visible output separated `Email draft` and `Review notes`; the 2026-06-20 offline Northstar Sleep Lab check now returns `needs_approval` for draft-only outreach and does not pull prompt metadata into the email body. A no-live Promptfoo-provider slice now confirms approved-facts LinkedIn-note wording without approved WorkItem context, missing recipient, variant drafting without approved brief, and PHI/patient-story requests produce sectioned blocked summaries without duplicate approval boilerplate or internal schema/state terms. | Passing for draft-only approval gates, expected blocked states, and no-live output hygiene. | Future live test should check a different recipient/use case and keep review notes visually separate without replacing specialist synthesis. |
| `airtable_context_agent` | Recent live `agent_runs` success rows; Period 2 tax payments answer matched visible Airtable context. Focused offline formatter tests now require separated bold `Answer`, `Detailed Summary`, and optional `Useful references` sections. A no-live finance/tax prompt now returns topic-aware Tax Payments/Estimated Tax Period 2/Q2 Summary guidance instead of eval-tracker boilerplate. | Passing for read-only table/schema lookup, offline section formatting, and dry-run topic recognition. | Future live probe should confirm the Slack bridge shows the updated section contract and names only actually returned Airtable records. |
| `google_workspace_context_agent` | Recent live `agent_runs` success rows; KNIOps Drive read-only context was accessible. Focused offline formatter tests now keep approval/write boundaries and blockers in `Detailed Summary` instead of dense inline prose. A no-live onboarding/SOP prompt now returns topic-aware dry-run output with candidate folder/doc/sheet hints and explicit live-read limits instead of eval-artifact boilerplate. | Passing for read-only Drive context lookup, offline section formatting, and dry-run topic recognition. | Future live probe should confirm the Slack bridge shows the updated section contract and that live Workspace reads name only actually returned files. |
| `zotero_context_agent` | Recent live `agent_runs` success rows; foundational collection context resolved from local Zotero cache. A no-live depression/psychiatric-diagnosis prompt now returns topic-aware dry-run output with collection/context hints, evidence guidance, and explicit item-level evidence limits instead of generic behavioral-health boilerplate. | Passing for read-only local cache context and dry-run topic recognition. | Page extraction and/or local item reads remain needed before relying on book/webpage records externally. |
| `rss_context_agent` | Local live KBA row selected `rss_context_agent` and produced a valid `RssContextResult`; dry-run CLI also passes; renderer tests cover feed item summaries, themes, evidence gaps, and useful references; CLI/provider payload tests require `output_type=RssContextResult` plus sectioned `human_summary`; Slack expansion seed pack now includes direct RSS context-agent coverage; orchestrator dry-run checks distinguish read-only/no-post constraints from real post requests; full local Promptfoo JSON run passes `slack_rss_context_announcement_history_001` with `route=rss_context_agent`, `status=done`, `output_type=RssContextResult`, and `run_mode=dry_run`. A no-live `@KNI rss context agent` text-mode probe now shows sectioned `Answer`, `Detailed Summary`, and `Useful references` output with domain-specific themes and dry-run item-evidence limits. A conversational "could the rss context agent..." WorkItem replay now returns a specific Chief context-agent advisory wrapper instead of generic Slack/OpenAI source text. | KBA route/output and offline eval coverage passing; Slack rendering not passing. | Sibling Slack bridge does not yet render `RssContextResult`, so Slack displayed a generic WorkItem completion. |
| `preprints_context_agent` | Dry-run CLI route/output passes with `--no-live-sdk`; registry and parser coverage are present; shared feed/preprint renderer tests cover item-level human summaries; CLI/provider payload tests require `output_type=PreprintsContextResult` plus sectioned `human_summary`; Slack expansion seed pack now includes direct preprints context-agent coverage; full local Promptfoo JSON run passes `slack_preprints_context_preliminary_evidence_001` with `route=preprints_context_agent`, `status=done`, `output_type=PreprintsContextResult`, and `run_mode=dry_run`. A no-live `@KNI preprints context agent` text-mode probe now shows sectioned `Answer`, `Detailed Summary`, and `Useful references` output with domain-specific themes and dry-run evidence limits. KBA contract tests confirm sibling consumers should render `human_summary` for `PreprintsContextResult`. A conversational "could the preprints context agent..." WorkItem replay now returns a specific Chief context-agent advisory wrapper instead of generic Slack/OpenAI source text. | KBA route/output, contract, and offline eval coverage passing. | Needs one future live Slack/KBA probe after RSS/preprints renderer support is fixed. |

## Control-Plane Routes

| Route | Current evidence | Status | Remaining gap |
| --- | --- | --- | --- |
| `chief_of_staff` | Focused WorkItem tests cover output-driven handoff to Opportunity Scout and advisory-only/no-handoff behavior. Chief recommendations naming Business Research Analyst, Opportunity Scout, Gmail Triage, or Outreach Composer now execute that downstream route unless constrained as advisory-only. A no-live local `@KNI chief of staff agent` CLI probe confirmed a Baylight Rehab prompt reaches Opportunity Scout and returns source-provided directions. A direct-runner regression now covers the Slack bridge path where `scripts/run_chief_of_staff.py` receives a Chief live-SDK result that recommends Business Research and must execute a delegated WorkItem manager-loop handoff. A later no-live Northline Imaging prompt confirmed the normal ask path advances Chief to Business Research with clean source-provided output and no instruction-as-evidence leakage. A live Harborline Logistics Slack probe confirmed the direct-runner path completed with `Route: business_research_analyst` and WorkItem route sequence `chief_of_staff -> business_research_analyst`. A no-live Promptfoo-provider slice now covers owner/action logs, eval-gap advisory summaries, executive briefs, source-integrity blocking, and Airtable/Workspace/Zotero advisory context plans with sectioned low-metadata output and passing manager review. | Passing for recommendation-to-handoff behavior offline and in one live Slack direct-runner probe. | Sibling Slack bridge cleanup is still needed to suppress metadata derived from negated outreach/write terms; avoid those prompts in further live probes until that lands. |
| `orchestrator` | Recent offline and live diagnostics show named-agent routing through Orchestrator preflight before specialist execution. A no-live Promptfoo-provider slice now covers finance-context read routing, ambiguous research-plus-outreach decomposition, source-provided vendor-table routing, and natural score-save follow-up with clean Chief fallback summaries and passing manager review. | Passing for tested named-agent routes, decomposition blocks, source-provided routing, and score-save read-only follow-up. | Keep future probes bounded and diverse; avoid broad route sweeps until RSS/preprints bridge rendering is fixed. |

## Slack Channel Routing

KBA-side offline tests now prove selected-message and Chief action payloads
preserve the source channel id for downstream handling. The exported KBA Slack
contract now also advertises `source_channel_response_routing` and a
`slack_response_routing` section requiring bridge consumers to reply in the
source channel/thread rather than funneling named-agent or context-agent
results into `#ai-agents-workflow` or `#evals` unless that was the source
channel. This still does not by itself prove the sibling Slack app posts replies
into every listed workspace channel; that final proof requires one live Slack
probe in a non `#ai-agents-workflow` channel after renderer support is in place.

Additional no-live Promptfoo-provider diagnostics now expose
`slack_channel_id`, `slack_channel_name`, thread timestamp, and selected-message
timestamp in compact outputs. A parametrized local test confirms those fields
are preserved for the visible workspace channels: `ai-agents-workflow`,
`announcements`, `calendar`, `collaborations`, `docs`, `evals`, `general`,
`git`, `gmail`, `grants-and-funding`, `knowledge-hub`, `meetings`, and
`runtime-updates`.

## Current Known Gaps

- `business_research_analyst` named-agent rendering requires sibling Slack
  bridge support. KBA now promotes child `human_summary` fields to the
  top-level named-agent envelope and advertises named-agent renderers under
  `result_rendering.named_agents`, but the sibling bridge still needs to
  prefer that summary contract over legacy route-specific metadata formatting.
  This is tracked as `SLACK-BRIDGE-NAMED-001`.
- `rss_context_agent` and likely `preprints_context_agent` require sibling
  Slack bridge renderer support. KBA now routes bridge-stripped context-agent
  text, produces structured RSS/preprints context outputs, and advertises all
  five context result types in the Slack business-agent contract under
  `result_rendering.context_agents`. The sibling bridge still needs to consume
  that contract or otherwise render `RssContextResult` and
  `PreprintsContextResult` from `human_summary`.

## Reasoning Preservation Audit

Current KBA changes are intended to preserve future live agent reasoning. The
formatting fixes operate after specialist execution, on structured outputs or
renderer summaries. They do not replace Orchestrator preflight, manager-loop
routing, WorkItem context packs, specialist SDK synthesis, deterministic safety
gates, or provider cost telemetry.

Proven by the current checks:

- The 36-case no-live Promptfoo-provider snapshot still exercises the named
  agent routes and requires each Slack-facing summary to expose a visible
  `Answer:` section without leaking raw workflow metadata.
- The 2026-06-20 Harborline live Slack probe executed a Chief of Staff
  downstream handoff to Business Research through the WorkItem manager loop and
  recorded live SDK/cost telemetry locally.
- Source-provided Business Research cleanup now removes live-inappropriate
  "dry-run case/eval" wording while preserving approved inline facts and
  source-limited caveats. Follow-up checks also keep source-provided market
  memo/caveat helpers mode-neutral, so live source-provided runs are not framed
  as fixtures or dry runs.
- KBA exposes named-agent and context-agent `human_summary` rendering rules in
  the Slack contract so sibling renderers can display the agent-authored answer
  rather than falling back to generic WorkItem status or route metadata. Focused
  KBA contract tests now pin result-display resolution for every advertised
  named-agent and context-agent renderer, including route-only,
  selected-agent-only, and output-type-only payload shapes a bridge may receive
  from different child wrappers. KBA also publishes executable rendering
  examples, including a Suki-shaped Business Research legacy-display fallback,
  in `contracts/keystone_slack_result_rendering_examples.v1.json` so the
  sibling bridge can test expected display text without another live run:
  `.venv/bin/python scripts/validate_slack_result_rendering_examples.py`.
  The conversational Nabla class is also covered no-live at the planner and
  WorkItem layers so KBA no longer reroutes that shape to Opportunity Scout.
  The fuller bridge pre-live gate is
  `.venv/bin/python scripts/validate_slack_bridge_contract.py`, which checks the
  exported contract artifact, result-rendering examples, and source-channel
  routing without Slack or model calls.

Not yet proven:

- Live Slack rendering for `RssContextResult` and `PreprintsContextResult`
  after sibling bridge support lands.
- Live reply routing in every visible Slack channel, beyond KBA-side channel
  preservation tests.
- A direct Business Research live-source run with visible primary URLs rendered
  through the named-agent `human_summary` contract.
- Immediate OpenAI billing-balance movement; local SDK telemetry is the current
  auditable cost signal.

## Next Low-Cost Test Plan

No additional live Slack/API probes should run under the current four-probe
budget. After the sibling Slack bridge supports named-agent and RSS/preprints
`human_summary` rendering, and only with a fresh explicit budget, use at most
one live Slack probe for each of:

1. `business_research_analyst`: non-healthcare, non-Example-Health live-source
   company research with visible primary URLs and low-metadata Slack rendering.
2. `rss_context_agent`: non-mutating announcement/feed-history lookup with a
   prompt that should either return item context or a clear retrieval miss.
3. `preprints_context_agent`: non-mutating preprint-history lookup with a
   bounded topic and visible source/result limits.
4. One non-`#ai-agents-workflow` channel-routing probe, only after the answer
   renderer checks above are clean.

Do not run broader live testing until these specific renderer and coverage gaps are closed.

## Prepared Live Probe Queue

Use these prompts only after confirming the sibling Slack bridge is rendering
named-agent and context-agent `human_summary` fields. Run them sequentially in
`#ai-agents-workflow`, stop after any failure, and inspect the Slack-visible
answer plus local WorkItem events before starting the next one. Keep the total
to 2-3 live probes unless the first results are clean, cost remains acceptable,
and a fresh live-test budget has been approved.

Offline preflight status: all three prepared prompts below route to the intended
single-agent path with `send_enabled=false` and `send_email` forbidden. This is
route/safety evidence only; it is not a live model/search or Slack rendering
proof.

1. Direct Business Research named-agent rendering and live-source probe:

   `@KNI business research analyst: diagnostic case diag_business_research_20260620_live_sources_002 Research Samsara as an operations technology company. Use live source-backed research if available. Return a concise Answer, Detailed Summary, and Useful references with visible primary URLs. Focus on product/workflow, buyer fit, evidence or deployment signals, and what remains unverified. Do not draft outreach, send, schedule, write files, create CRM records, publish, or post elsewhere.`

   Expected proof: Slack-visible answer renders the KBA `human_summary` with
   source-backed facts and primary URLs, avoids unsupported claims, and stays
   low-metadata. Local WorkItem telemetry should show live search/source reads
   and SDK usage/cost events.

2. RSS context rendering probe:

   `@KNI rss context agent: diagnostic case diag_rss_context_20260620_live_render_001 Read-only feed context test for clinical AI validation and remote monitoring operations updates. Use RSS/feed context tools only. Return a concise Answer, Detailed Summary, and Useful references if items are available. State clearly if item-level feed evidence is unavailable. Do not post elsewhere, refresh feeds, write files, create records, draft, send, publish, or schedule.`

   Expected proof: Slack displays the sectioned RSS answer from `human_summary`,
   not a generic WorkItem completion message. KBA route/output should show
   `rss_context_agent`, `RssContextResult`, read-only status, and no side
   effects.

3. Preprints context rendering probe:

   `@KNI preprints context agent: diagnostic case diag_preprints_context_20260620_live_render_001 Read-only preprints context test for adolescent depression, digital phenotyping, and wearable-sensor monitoring. Use preprints context tools only. Return a concise Answer, Detailed Summary, and Useful references if bounded item evidence is available. State clearly if preliminary evidence is missing or not validated. Do not post elsewhere, refresh feeds, write files, create records, draft, send, publish, or schedule.`

   Expected proof: Slack displays the sectioned preprints answer from
   `human_summary`, not a generic WorkItem completion message. KBA route/output
   should show `preprints_context_agent`, `PreprintsContextResult`, read-only
   status, and no side effects.

4. Optional channel-routing probe after the above pass:

   Run one read-only named-agent ask from a non-`#ai-agents-workflow` channel
   such as `#docs`, using a short Chief of Staff advisory prompt with no live
   search. Expected proof: the response thread stays in the source channel and
   local compact payloads preserve the source channel id/name. Do not run this
   until the core `#ai-agents-workflow` probes are clean.

## Offline Eval Snapshot

Current focused local Promptfoo-provider snapshot remains a diagnostic map, not
a live signoff:

- `promptfoo/tests/slack_agent_expansion_15.yaml`: 36/36 passing, 0 provider errors.
- Latest focused rerun: 2026-06-21
  `.venv/bin/python scripts/run_slack_agent_expansion_gate.py --quiet`, 36
  successes, 0 failures, no live Slack/API/model/search calls. Route coverage:
  Airtable Context 2, Business Research 5, Chief of Staff 11, Clarification 1,
  Gmail Triage 4, Google Workspace Context 2, Opportunity Scout 4, Outreach
  Composer 4, Preprints Context 1, RSS Context 1, Zotero Context 1. These
  route minimums are enforced by the gate for the default 36-case pack.
- Latest KBA-side Slack bridge contract check: 2026-06-21
  `.venv/bin/python scripts/validate_slack_bridge_contract.py` passed, including
  exported contract freshness, result-rendering examples, and source-channel
  routing. `.venv/bin/python scripts/validate_slack_result_rendering_examples.py`
  also passed with 5 rendering examples.
- Previous promptfoo JSON rerun: `eval-znV-2026-06-20T23:16:20`, 36 successes,
  0 failures, 0 errors, 0 reported provider tokens.
- Readability is now an enforced Promptfoo invariant for the same 36 generated
  summaries: each Slack-facing `human_summary` must expose a visible `Answer:`
  section and avoid raw JSON, snake_case workflow fields, or known
  provider/workflow metadata terms. The invariant also fails summaries that put
  diagnostic headings such as `Agent`, `Search`, `Retrieval diagnostics`, or
  `Timing` before the reader-facing answer.
- Context agents: Airtable 2/2, Google Workspace 2/2, Zotero 1/1, RSS 1/1, Preprints 1/1.
- Operating/control routes: Business Research 5, Gmail Triage 4, Opportunity
  Scout 4, Outreach Composer 4, Chief of Staff 11, Clarification 1.
- This does not replace future bounded live Slack probes for renderer/channel behavior or billing/cost confirmation.
