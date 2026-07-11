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
   review-only Slack draft. Hard ceiling: 8 OpenAI requests and $0.50.

Combined hard ceiling: 11 OpenAI requests and $0.60. Run serially in
`#ai-agents-workflow`; stop after the first failed render, retry, missing usage
receipt, raw Gmail body persistence, duplicate final response, or unexpected
side effect.

## Approval boundary

Approval must explicitly cover the two Slack posts and the combined request/cost
ceiling. It does not approve Gmail mutation, Gmail draft creation, email send,
live search for the graph probe, schedules, files, Airtable/Workspace/Zotero
writes, or posting outside the exact workflow channel.

## Required evidence

- Slack permalink and local run/WorkItem ID for each probe.
- Direct route versus LangGraph node path.
- Answer-first visible body with no provider/model/timing metadata before it.
- Visible source URLs or explicit source limitations for the direct research ask.
- Selected Gmail identity without persisted raw message body for the graph ask.
- One final response per probe; no duplicate preview/completion/final posts.
- Usage, trace, request count, retry count, and cost receipt.
- Explicit no-unintended-side-effect result.

ANU-60 and the operationalization goal remain incomplete until the two live
permalinks and receipts exist. Fixture and no-live readiness evidence cannot be
substituted for visible Slack acceptance.

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

The two completed model requests cost an estimated $0.0713 from the checked-in
pricing table, within the direct probe's three-request and $0.10 ceiling. This is
a useful partial proof, not a passing `SLACK-DIRECT-01` acceptance row; rerun only
after a separately approved budget confirms the fixed visible output.
