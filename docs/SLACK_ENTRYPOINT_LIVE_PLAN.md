# KBA Direct and Graph Slack Entrypoint Plan

This is the narrow live acceptance packet for the medium-term operationalization
goal. It complements the broader ANU-60 proof plan; it does not replace the
remaining issue-level acceptance rows.

## Exact probes

1. `SLACK-DIRECT-01`: one direct named Business Research ask for Suki AI with
   visible sources. Hard ceiling: 3 OpenAI requests and $0.10 estimated cost.
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
