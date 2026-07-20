# KBA Direct and Graph Slack Entrypoint Plan

This is the narrow live acceptance packet for the medium-term operationalization
goal. It complements the broader ANU-60 proof plan; it does not replace the
remaining issue-level acceptance rows.

## Exact probes

1. `SLACK-DIRECT-01`: one direct named Business Research ask for Suki AI with a
   formatted research brief and visible sources. Hard ceiling: 3 OpenAI requests
   and $0.10 estimated cost. Express the desired brief sections instead of
   repeating generic no-send/no-write policy already enforced by the agent.
2. `SLACK-GRAPH-01`: one connector-backed Gmail → Business Research → Outreach
   graph ask using one configured exact safe sender, no live search, and a
   formatted Slack review summary containing the thread's main point,
   organization context, suggested reply, evidence, and approval state. The
   natural ask does not repeat generic no-send/no-write boilerplate; policy and
   deterministic gates enforce those boundaries. Hard ceiling: 8 OpenAI
   requests and $0.50.
3. `SLACK-DIRECT-ZOTERO-01`: one direct Zotero authenticated provider read that
   selects the most recently added journal article and projects only title,
   authors, and publication title without web search, full text, or mutation.
   Hard ceiling: 3 OpenAI requests and $0.05. Passing evidence already exists:
   run `5692` used one request at an estimated `$0.01259775`.
4. `SLACK-DIRECT-CONSTRAINT-01`: the exact direct Business Research regression,
   `@KNI BA, who is Abridge and summarize the company in 20 words.` The visible
   answer must contain exactly 20 words, retain a source URL outside the counted
   answer, omit generic detailed-summary expansion, and produce one final thread
   reply. Hard ceiling: 4 OpenAI requests and $0.15.

Registry hard ceiling: 18 OpenAI requests and $0.80 across all four probes.
All four probes have passing evidence. The exact constraint proof reused KBA run
`5851`: one request at an estimated `$0.01005225`, an exact 20-word visible
answer, one final Slack reply, and deterministic Orchestrator review pass. No
additional allowance or rerun is needed.

## Approval boundary

The recorded approvals covered only the exact probes and their ceilings. They
did not approve Gmail mutation, Gmail draft creation, email send, schedules,
files, Airtable/Workspace/Zotero writes, or posting outside the exact workflow
channel.

## Required evidence

- Slack permalink and local run/WorkItem ID for each probe.
- Direct route versus LangGraph node path.
- Answer-first visible body with no provider/model/timing metadata before it.
- Visible source URLs or explicit source limitations for the direct research ask.
- Selected Gmail identity without persisted raw message body for the graph ask.
- One final response per probe; no duplicate preview/completion/final posts.
- Usage, trace, request count, retry count, and cost receipt.
- Explicit no-unintended-side-effect result.

All four entrypoint probes across Business Research, the connector-backed Gmail
graph, direct Zotero, and exact constraint handling have live evidence. Fixture
and no-live readiness evidence were not substituted for visible Slack
acceptance.
Fixture and no-live readiness evidence cannot be substituted for future live
acceptance claims either.

## 2026-07-11 direct-probe checkpoint

The real KNI app entrypoint routed the natural Suki request to Business Research
and returned an answer-first focused brief with visible first-party source URLs.
The run remained read-only and the structured receipt recorded one model request.

The probe also exposed two acceptance defects, so the sequence stopped before
Airtable:

- Editing an earlier literal `@KNI` message into an app mention emitted a run,
  then the corrected fresh mention emitted a second run. Do not use message edit
  to repair an entrypoint probe; remove the non-triggering message and post one
  resolved app mention instead.
- The compact answer renderer cut a sentence at its character limit and exposed
  internal "approved context" wording. Summary compaction is now sentence-aware,
  the focused-brief prompt requires direct operator language, and focused tests
  cover both regressions.

The initial two model requests cost an estimated $0.0713 from the checked-in
pricing table, within the direct probe's three-request and $0.10 ceiling. A
later recorded rerun supplied the passing `SLACK-DIRECT-01` permalink, answer-
first body, source visibility, run identity, cost receipt, and no-side-effect
evidence. Do not rerun it without a materially new acceptance question.
