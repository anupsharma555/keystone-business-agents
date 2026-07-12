# ANU-61 Controlled Pilot Contract

Executable source: `src/keystone_agents/controlled_pilot.py`

The controlled pilot begins only after L174-14, L174-16, L174-19, and L174-20
each have a trusted-runtime PASS receipt. Existing provider primitives and the
two completed Slack entrypoint proofs should be reused rather than rerun.

The four pilot workflows are:

1. source-backed research to reviewed Google Doc-ready content and an exact
   write plan, without creating the Doc;
2. selected Gmail thread to a current-state KNI next action or justified draft;
3. a direct current opportunity assessment;
4. a broad weekly or project brief.

The opportunity assessment stays on the direct specialist path. The other
three use LangGraph because they require dependent stages or manager-owned
cross-source synthesis. Chief of Staff is not a default wrapper around the
direct specialist case.

The direct Opportunity reasoning layer now has a compact supplied-packet mode.
It returns one `OpportunityAssessmentBrief` with confirmed source-linked facts,
interpretation, KNI fit, timing, geography uncertainty, missing evidence, one
next safe action, and one retained source list. A live exact-ask run reduced
output from 6,234 to 998 tokens and the maintained estimate from `$0.0309867`
to `$0.02411625`. A second prompt-efficiency pass kept the same acceptance
contract while reducing the composed instruction payload from 119,733 to 31,503
characters; the live result used 8,029 input and 1,123 output tokens at a
maintained `$0.01107525`, with every quality/safety check passing. The matched
generic baseline also passed at `$0.005883`, so this one-turn case does not
support a KBA differentiation claim. It remains reusable direct-specialist
evidence; the case still needs Slack-entry/permalink evidence before it is a
full pilot PASS.

The research-to-Doc reasoning layer also passes outside Slack. One live
official-page run used one request at a maintained `$0.027426`, preserved ten
extracted claims and the exact source URL, and returned a reviewed `KNIOps`
Doc plan without entering Workspace writes. Reuse this model/plan proof; do not
repeat it when collecting the remaining Slack-entry evidence.

The no-tool Business Research prompt was then reduced from 141,342 to 31,530
characters while retaining the full prompt for tool-enabled discovery runs.
The matched live rerun preserved the same ten claims, exact official source,
645-token focused brief, and no-write plan while reducing input from 32,764 to
9,279 tokens and the maintained estimate from `$0.027426` to `$0.00986175`.
This optimized receipt supersedes the earlier cost/usage baseline.

The initial pilot is read-only at the provider boundary. All four cases require
`provider_writes=0`; provider mutation capability is reused from ANU-174 and
ANU-223 rather than repeated inside Slack. A Doc-ready content packet or write
plan is not permission to create the Doc. Any later write-enabled pilot must be
a separately scoped issue or phase with its own explicit approval contract.

Gmail selection must preserve the operator's requested object. "Latest email"
means resolve one newest matching message by internal provider identity and
then read every message in its containing thread so chronology and current
state are available. "Latest thread" means select the newest matching
conversation directly and read that complete thread. These are separate asks;
neither may be silently substituted for the other.

The Gmail graph-pair preflight resolves accumulated synthetic validation
history newest-first and keeps provider identities internal. The current
provider-only proof selected one three-message thread from two marked
candidates, wrote a sanitized hashed source bundle, and made zero OpenAI
requests or Gmail writes. Its natural operator ask names the exact marked draft
lifecycle and therefore supplies approval for that scope; it does not require a
duplicate approval round trip or a long list of prohibited actions. Send and
unrelated-write boundaries remain authoritative system gates.

That older reversible-draft graph pair is not the first ANU-61 pilot case
because it performs provider writes. The first live pilot ask instead resolves
the latest thread from the configured exact test sender, reads the original
inquiry and all messages, uses only the complete thread plus approved KNI
context, and returns a recommendation or useful reply text in Slack without
creating a Gmail draft.

Each saved observation must reference the catalog's exact unchanged natural ask
by SHA-256; the scorer rejects a different well-formed hash rather than treating
arbitrary 64-character evidence as the case. Observations also record the Slack
permalink, answer-first state, human usefulness/copy review,
final-response count, route,
actual backend, WorkItem continuity, visible sources, provider-ID re-entry,
approval round trips, exact provider writes/read-back/cleanup, unintended
writes, duplicate artifacts, developer intervention, OpenAI requests, cost,
latency, and a trace or run reference.

A case fails if it needs a manual provider ID, a duplicate approval, any
provider write, duplicate artifacts, developer repair,
more than one final response, the wrong backend, or missing trace/cost/source
evidence. A passing KBA observation can then be paired with a matched clean
Codex/ChatGPT-style observation through the ANU-175 comparison contract; the
pilot must not infer a comparative win from missing baseline data.

The selected-Gmail-thread case is capped at two OpenAI requests: one compact
manual plan and one compact Outreach recommendation. Recommendation-only
sanitation is deterministic and must not trigger a second Outreach repair call.
The compact synthesis agent omits unused tool/recovery/lifecycle prompt surfaces
and receives only approved claims, minimal style settings, complete chronology,
and a compact Orchestrator memo.

Build the combined ANU-61/ANU-125 scorecard from sanitized saved observations:

```bash
npm run score:controlled-pilot -- \
  --input artifacts/test-pack/controlled-pilot-observations.json \
  --output artifacts/test-pack/controlled-pilot-scorecard.json
```

The input object has `trusted_runtime_rows`, `observations`, and optional
`baseline_observations`. Duplicate observations are rejected rather than
cherry-picked. Missing trusted rows, pilot cases, or baselines stay visibly
pending. A full pilot PASS requires all four trusted rows plus all four passing
KBA observations. Comparative claims are counted separately and only after a
matched baseline observation passes the ANU-175 comparison contract.

The current no-live readiness artifacts are:

- `artifacts/test-pack/controlled-pilot-prelive-input.json`
- `artifacts/test-pack/controlled-pilot-prelive-scorecard.json`

They intentionally report `pilot_status=pending`, all four trusted-runtime rows
passing, zero pilot observations, zero observed usage, and zero planned provider
writes. The trusted-runtime prerequisite is satisfied, but this is not a live
pilot pass. Aggregate request and cost values are per-case ceilings across the
full four-case catalog, not authorization to run them as a batch. Start with one
case, inspect its receipt, and patch before proceeding.
