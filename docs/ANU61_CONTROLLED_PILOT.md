# ANU-61 Controlled Pilot Contract

Executable source: `src/keystone_agents/controlled_pilot.py`

The controlled pilot begins only after L174-14, L174-16, L174-19, and L174-20
each have a trusted-runtime PASS receipt. Existing provider primitives and the
two completed Slack entrypoint proofs should be reused rather than rerun.

The four pilot workflows are:

1. source-backed research to one verified internal Google Doc;
2. selected Gmail thread to a current-state KNI next action or justified draft;
3. a direct current opportunity assessment;
4. a broad weekly or project brief.

The opportunity assessment stays on the direct specialist path. The other
three use LangGraph because they require dependent stages or manager-owned
cross-source synthesis. Chief of Staff is not a default wrapper around the
direct specialist case.

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

Each saved observation must reference the exact natural ask by SHA-256 and
record the Slack permalink, answer-first state, final-response count, route,
actual backend, WorkItem continuity, visible sources, provider-ID re-entry,
approval round trips, exact provider writes/read-back/cleanup, unintended
writes, duplicate artifacts, developer intervention, OpenAI requests, cost,
latency, and a trace or run reference.

A case fails if it needs a manual provider ID, a duplicate approval, an extra
write, an unverified allowed write, duplicate artifacts, developer repair,
more than one final response, the wrong backend, or missing trace/cost/source
evidence. A passing KBA observation can then be paired with a matched clean
Codex/ChatGPT-style observation through the ANU-175 comparison contract; the
pilot must not infer a comparative win from missing baseline data.

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
