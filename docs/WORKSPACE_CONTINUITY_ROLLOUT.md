# Workspace-scoped Slack continuity rollout

This extends the compact continuity implementation from its original channel
pilot to channels in an explicitly enrolled Slack workspace. It changes where
the shared capability is admitted; it does not pool channel histories or turn a
successful pilot case into universal live acceptance. The operator authorized
the workspace rollout on November 15, 2026.

## Technical index

- [Shared behavior and local scope](#shared-behavior-and-local-scope)
- [Enrollment and compatibility](#enrollment-and-compatibility)
- [Context budgets](#context-budgets)
- [Verification and activation](#verification-and-activation)
- [Remaining limits](#remaining-limits)
- [Serial Slack acceptance](#serial-slack-acceptance)

## Shared behavior and local scope

| Capability | Reused across enrolled channels | Scope retained for each request |
| --- | --- | --- |
| Orchestrator summary and reply interpretation | Existing agent instructions, tools and continue/branch/new/clarify decisions | Current raw request and admitted thread/task; history cannot grant approval |
| WorkItem continuation and cached source reads | Common source binding, integrity checks, native graph and saved-task admission | Exact linked task and selected source; unrelated tasks and sources remain excluded |
| Seven-day historical lookup | Same bounded query/read tools | Only the active channel, with current request time and candidate identity checks |
| Recent thread context | Same bounded host capture and dated excerpts | Only the active thread; current/future messages excluded and incomplete reads marked |
| Latest failed attempt | Same host journal projection | Exact linked task, channel and thread; task status stays separate from attempt status |
| Approved memory | Same query/capture tools and eligibility checks | Exact workspace/channel/operator/project; no automatic migration or duplication of pilot preferences |
| Approved operator profile | Same existing profile lookup | Only configured operator subjects in the enrolled workspace, selected for relevance |
| Source reuse and prompt caching | Existing cache/snapshot contracts | Source identity and evidence limits remain checked; a source-cache read does not prove a provider prompt-cache hit |
| Slack presentation | Shared structured sections, links and relevant follow-up guidance | Current result and task, without unrelated action buttons |

The rollout does not fetch all channel history at startup, create memories,
replay old requests, join private channels, grant provider permissions, or
enable scheduled automation. A new root does not inherit the last task in its
channel. Existing scoped records keep their original scope.

## Enrollment and compatibility

The Slack host and KBA have separate explicit configuration boundaries:

```dotenv
# Slack host configuration
KNI_BUSINESS_AGENTS_LAZY_CONTEXT_TEAM_IDS=TEXAMPLE

# KBA configuration
KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED=true
KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS=TEXAMPLE
```

Use the exact authenticated workspace identity in private local configuration.
No real workspace, channel or operator identifier belongs in this document or
its synthetic tests. KBA may read its team enrollment from the existing
allowlisted linked Slack configuration, subject to existing environment
precedence.

A nonempty team setting selects workspace mode. Valid public or private channel
identities in that exact workspace are eligible; direct messages are outside
this channel rollout. A foreign or missing team and malformed configuration
fail closed. A matching legacy channel list cannot rescue a rejected workspace.
The host descriptor carries the current team so the consumer can reject a
mismatched scope before retrieval.

With no team enrollment, the prior explicit channel-list mode remains
available. Keep the existing pilot channel values for a bounded rollback;
clearing the team settings from both local configuration files, including any
linked copy of the KBA setting in the Slack file, restores that mode after a
guarded host reload.
Disabling KBA's feature flag restores its existing non-compact session policy.

Enrollment does not bypass Slack membership, history scopes, retention or
external sharing restrictions. Missing access is reported as unavailable
context, not silently substituted with another channel's messages.

## Context budgets

The rollout retains the accepted limits: recent linked-thread interpretation
has an 8,192-byte default ceiling; canonical task handles have a 2,048-byte
ceiling; combined optional task/memory context has a 6,144-byte ceiling. Up to
four relevant approved facts may be admitted. These are serialized-byte bounds,
not exact token counts. Article evidence uses separate bounded source readers.
Eligible requests omit raw SDK history rather than copying entire transcripts.

## Verification and activation

The integrated KBA gate passed **604 checks**. It covers the workspace policy,
24 synthetic public/private channel identities, memory and profile eligibility,
actual direct-child transport, source readers, public CLI/native graph cases,
session omission, rendering, registry and prompt contracts. The public graph
tests include workspace public/private exact-source continuations, short replies
and separate branches with scripted native SDK models. These establish backend
behavior, not live model judgment.

The final integrated Slack-host gate passed **408 tests and 79 subtests**
against the actual checkout. It covers lazy query/read bounds, current-thread
capture, root mentions, mentioned and unmentioned owned-thread replies, foreign
workspace task rejection, attempt storage and active-run restart guards. The
host's system Python lacks pytest, so its test gate used the existing KBA test
interpreter with the actual Slack package on `PYTHONPATH`. Temporary state and
an unchanged copy of the manifest provided the tests' expected working-directory
layout. No dependency was installed or runtime interpreter changed.

Scoped KBA/host Ruff, source compilation and whitespace checks passed. The
existing large Slack Socket Mode module retains unrelated baseline lint
findings; this rollout does not claim repository-wide host lint cleanliness.

Independent review exposed a concrete cross-channel forwarding defect despite
normal memory queries being scope-filtered: a prior fact projection could seed
another channel's capability and direct child input. The repair validates exact
record IDs against the current destination and current approval, safety, expiry
and supersession state, and compares complete projected facts before forwarding.
An unavailable store or changed record omits the fact; it does not promote old
text to approved memory. Valid facts remain deduplicated across model stages.

The parent reviewed and applied the Sol workers' source changes and independently
ran the integrated gates. Workspace configuration was then enabled while
preserving the old channel lists. The active-run guard found no queued/running
Business Agent requests, and a fresh connected worker hello was observed at
10:19 Eastern on November 15. A fresh process using the real Slack bridge's KBA
child environment admitted 24 synthetic public/private channels and rejected a
foreign workspace without provider calls. This confirms configuration alignment
and loaded host startup; it does not constitute a new live agent acceptance run.

An additional post-configuration KBA gate passed **153 affected checks** using
the enabled workspace configuration. These overlap with the 604-check gate;
they are not additional unique coverage. Logs are local-only:
`/private/tmp/kba-workspace-integration-gate.log`,
`/private/tmp/kba-workspace-config-alignment-gate.log`, and
`/private/tmp/ks-workspace-final-gate.log`.

### Older thread compatibility

The final review found that many historical run records predate workspace
provenance. Rejecting every such record would break valid existing task links.
The compatibility path does not migrate or assign those records a workspace
by assumption. It requires an enrolled current transport identity, agreement
between the saved projection and its exact journal attempt, a unique trustworthy
root, and a bounded current-channel Slack root read matching timestamp, author
and normalized original request text. Any explicit foreign-workspace record or
attempt blocks reuse before that read. Missing, changed, ambiguous or unreadable
roots also block it; disabled history performs no root read. A root may be
verified from an otherwise incomplete page, but the missing later thread context
remains visibly marked incomplete.

The parent applied the reviewed compatibility patch and reran the final host
gate above, including its mismatch, mixed-workspace, duplicate-root and disabled
history cases. A second Sol worker independently reviewed the patch and ran its
22 compatibility/dispatch cases before integration. Older attachment-materialized
requests whose stored text differs from the provider root can remain blocked;
they require a separately verified explicit task reference rather than inferred
linkage. No historical records were bulk relabeled.

The final compatibility source was loaded through the same active-run guard.
A fresh connected hello was observed at **10:27:35 Eastern on November 15**;
the health record was healthy, with an empty last error and no restart
recommendation. No live agent request was dispatched to establish this startup
result.

## Remaining limits

The original pilot's natural short graph continuation succeeded live. A separate
branch asked unnecessary clarification; its subsequent date/source-preservation
repair passed backend checks but has not been rerun live. That historical limit
remains even after workspace enrollment. New channel enrollment establishes
availability and scope, not natural-model consistency for every agent, source,
channel, or reply type. No new live agent-test allowance is implied by this
rollout.

Normal Socket Mode root mentions and owned-thread replies are covered by the
offline host tests. Unmentioned root messages are not new agent requests.
Transport paths without a trustworthy workspace identity cannot enroll by
guessing from the channel list. Existing Slack permissions and membership still
bound actual channel access.

Historical evidence remains in the [pilot validation](COMPACT_CONTINUITY_PILOT_VALIDATION.md),
[implementation plan](COMPACT_CONTINUITY_IMPLEMENTATION_PLAN.md),
[memory/state review](CROSS_SESSION_MEMORY_STATE_REVIEW.md), and
[channel review](CROSS_CHANNEL_CONTEXT_REVIEW.md).

## Serial Slack acceptance

On November 15 the operator authorized up to two serial Computer-driven tests
in an additional channel. The selected announcement feed supplied a dated,
numbered article reference; no URL was inserted into the test request. The
batch retained the configured model, eight-request ceiling, three manager steps,
three-minute timeout and local USD 0.25 run guard. Extraction fallbacks were
temporarily disabled for static-only selected-page reading; they must be
restored after the batch. Search, other sources and outbound work were excluded.

Before dispatch, the Sol review gate passed 152 KBA tests. The actual-host gate
then exposed a test-harness problem: a helper inherited the background-execution
setting and could enter asynchronous error delivery. The observed errors
occurred before KBA dispatch, and no successful post was observed. The test-only
repair pins background execution off, mocks update delivery and asserts that no
thread is started. Independent checks passed with the ambient setting both
unset and enabled; the integrated host gate passed 69 tests before test one.

The first Slack request was **blocked before any model started**. It asked to
create a saved review task for a numbered feed item. The generic external-write
classifier interpreted the source noun "item" as the object of "create", and
returned a draft-only approval question. The current host context and channel
capability had the correct workspace, channel, operator and timestamps; missing
identity in a compact reporting projection was not missing transport admission.
No task, article extraction, source cache or graph stage was created.

Offline reproduction isolated two general admission defects. Local task/brief
creation could be confused with external record writes, and a no-web-search
constraint could deny an explicitly requested exact article read. The fixes
retain affirmative send/provider-write gates, distinguish local output from
explicit provider destinations, and distinguish discovery permission from
selected-source reading. Preview-only, no-page-read, no-new-extraction and
cached-snapshot restrictions still deny fresh acquisition. Existing cached
continuations retain their source binder rather than being promoted into a
fresh-page plan.

The parent integrated gate passed **917 KBA tests** after these repairs, with
Ruff, compilation and whitespace checks clean. The integrated host gate passed
**71 tests and two subtests**, including short replies to blocked roots without
a WorkItem, for both scoped and legacy thread provenance. These results overlap
earlier counts. The second and final Computer request used a short natural
retry in the same thread. Slack delivered an answer, but request fulfillment
was **partial**: the exact article was selected and extracted, while no saved
WorkItem or graph was created and the requested findings were not summarized.
Company-oriented sections were irrelevant to the article-review request.

The second attempt recorded five model starts. A single Trafilatura extraction
returned 6,081 characters; it included results and limitations. No broad search,
fallback extraction or external write was recorded. The specialist's two model
requests had an estimated local cost of $0.00457, which excludes the other
stages. The final Admin Usage window recorded five requests, 95,204 input
tokens (49,863 cached) and 4,204 output tokens. This is an ungrouped platform
window, not an exact invoice or independently attributed project cost.
The temporary static-only extraction profile was restored and its original
fallback setting verified after these two tests.

### Attribution and repair boundaries

The host inferred its initial plan from the short current reply alone and set
`requires_durable_state=false`. The Orchestrator received the original root in
thread context and chose the correct article, but incorrectly described a saved
task as already existing. The selected-source plan retained the host's false
durability value and ran direct company-focused synthesis. This is a planning
contract defect plus an unsupported model state assumption, not missing Slack
thread transport. The request to save an unfinished review must be interpreted
from the admitted conversation; it must not require a literal task identifier
or repeated workflow wording in every reply.

Separately, the company-profile conversion reduced a bounded 4,000-character
preview to 900 characters and dropped its cached-source access handle. The
retained claim candidates all preceded the results section. The focused model
therefore lacked the results and a source-window reader, even though extraction
had succeeded. Its stated evidence limitation was consistent with what the host
actually supplied. Merely enlarging the model context window would not repair
that discarded evidence or missing tool binding.

The repair is shared across enrolled channels. Conversation interpretation
remains model-owned, task existence remains a host-verified fact, and cached
article access remains bound to exact source identity and current scope. The
two Slack requests are exhausted. The operator subsequently authorized up to
five serial backend agent tests, with no additional Slack posts; offline
acceptance precedes those calls. Subsequent backend results must be recorded
separately from these failed/partial live Slack outcomes.

### Shared offline repairs after the Slack batch

Orchestrator now declares whether the active conversation requests a saved
resumable task, separately from the host's verified task-existence fact. This
small model-owned requirement applies to supported WorkItem specialists, not
one reply phrase or article channel. Unsupported existing-task claims and
incompatible immediate-answer ownership receive one bounded model correction.
An authenticated existing task retains its typed intent and artifact rather
than being recast as a newly requested brief. Explicit branch and new-request
decisions remain separate.

Selected source normalization preserves its already bounded preview and exact
snapshot handle. The direct selected-URL synthesis path binds its scoped local
reader after retrieval, when the handle is known, and records the resulting tool
scope. The generic synthesis hook preserves agent/model identity; it does not
grant fresh search or provider access. Focused instructions follow the actual
article or company question and leave irrelevant company fields empty.

The final parent gate passed **1,263 tests**, with scoped Ruff, compilation and
whitespace checks clean. An independent affected gate passed 299 tests. These
overlap previous counts. Native scripted SDK tests cover a taskless retry into
a persisted ResearchBrief graph, reading results beyond a truncated preview,
an unrelated same-thread reply, non-article saved work, invalid direct-answer
ownership, actual selected-URL script reader binding, and preservation of an
existing task's artifact. They prove mechanics with scripted decisions; live
model judgment is recorded separately below.

### Backend validation and in-run correction

The first authorized backend replay used the actual captured short-reply
context, normal tools and `gpt-6-luna`, with an isolated local task store and no
Slack posting. It recovered the original objective and restrictions, created a
saved WorkItem and entered the ResearchBrief graph. The specialist read a later
cached source window. It then blocked on `supplied_research_citation_mismatch`;
the exact bad citation field was not retained in this historical run.

The original correction was prevented by
`decision_repair_verified_candidate_universe_unavailable`: it expected a fresh
provider-result candidate record even though the catalog was already supplied
and the only model tool was a retained-source reader. This is distinct from the
initial model citation mismatch. The replay used six model requests and had an
estimated total model-token cost of $0.01412. The saved task alone does not prove
that the requested brief completed.

The correction contract now permits one model repair using the original,
verified supplied catalog and exact completed snapshot window. Tools are
disabled during that correction; no source is silently replaced and no
provider read is repeated. Citation validation remains authoritative. Invalid
catalog/source bindings, unavailable snapshots and an unsuccessful correction
remain blockers. Privacy-safe diagnostics record citation field paths,
mismatch categories, counts and fingerprints. The original validation outcome
is retained separately from a later replay failure. The diagnosis collector
surfaces both reasons and the recorded repair/evidence state without copying
full feedback, prompts or article text.

An expanded integrated offline gate passed **1,381 tests**, including native
source-window correction and privacy-safe diagnosis reporting. These overlap
the previous gates. The first backend replay remains a partial result; the
remaining authorized backend requests are used progressively after checks.

Backend replay two correctly selected continuation of that exact saved task,
using one Orchestrator request (estimated $0.00303), but failed before specialist
execution. The saved task, source and artifact count remained unchanged. Its
Orchestrator preflight was valid; the host continuation adapter and retry check
still admitted only legacy `source_summary` tasks and excluded the newly saved
`research_brief`. The generic terminal `schema_or_parse_error` incorrectly
suggested malformed model output. The source-binding repair preserves either
supported research artifact and the existing intent instead of converting source
identity into company-research intent. Exact task/source/permission checks remain
required; this is not permission to resume arbitrary blocked work.

Backend replay three **passed** after the repairs. It used the same isolated
WorkItem and exact snapshot from replay one, resolved the old blocker and saved
one ResearchBrief. One local source-window call read the remaining 4,881
characters through the end of the 6,081-character snapshot. No new extraction,
web search, provider request or external write was recorded. Independent review
confirmed that the answer covered the study, findings and limitations, cited
only the selected article and omitted irrelevant company sections. Its
underlying cited paper was not opened or independently verified.

This successful attempt used three model requests and an estimated $0.00705.
Its first specialist output passed citation validation, so in-run correction
was not triggered live; the deliberate mismatch-and-correction behavior is
verified by the native scripted SDK regression. The batch stopped after three
of five approved backend requests: ten total model requests, 283,160 input
tokens (131,299 cached), 7,798 output tokens and estimated total cost $0.02419.
The final Admin Usage snapshot had reported the first seven requests and had
not yet included replay three. Local estimates are not an invoice.

The expanded parent gate passed **1,952 tests**, followed by focused continuation,
private-logging and Gmail-scope regressions. A shared classifier regression found
by the wider gate was repaired offline: edits inside a single supported Gmail
draft retain only that draft operation, while send and additional provider
objects remain gated. No live Gmail operation was part of this work. Structured
host-admission diagnostics now remain in the private execution record; the
public failure reason is plain language.

These results prove this saved research retry in the backend, using captured
context from a second channel. They do not establish new Slack delivery,
all-channel live acceptance, branching, or recovery for every other agent and
failure state. Those pending checks remain open in the private issue tracker. The earlier failed/partial Slack and backend attempts
remain recorded separately. No additional Slack message was posted after the
two-test allowance was exhausted.
