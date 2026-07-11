# ANU-174 Trusted Runtime Handoff

This is the remaining serial execution queue after repository-side validation.
Do not run it as a batch, do not run Promptfoo, and do not start without a fresh
explicit OpenAI allowance and billing baseline.

## Proposed live ceiling

- Four serial `gpt-5.4-mini` requests total: one each for L174-14, L174-16,
  L174-19, and L174-20.
- `$0.05` per request and `$0.20` aggregate hard ceiling.
- Stop after the first unexpected retry, missing usage/cost evidence, structural
  validation failure, provider side effect outside the named scope, or shared
  model-quality defect.
- Patch and rerun only the failed row. Reuse all passing receipts.

## 1. L174-14 local KNI capability summary

```bash
npm run run:local-kni-capability-summary -- \
  --live-sdk \
  --approve-private-context \
  --output artifacts/test-pack/local-kni-capability-summary-live.json
```

Pass requires one request, cost at or below `$0.05`, the selected guarded source
path, substantive service-area synthesis, uncertainty/human review, and no
tools, search, writes, sends, posts, or persistent response storage.

## 2. L174-16 latest research note to disposable Doc

```bash
GOOGLE_WORKSPACE_WRITES_ENABLED=true \
KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true \
npm run run:workspace-research-brief-lifecycle -- \
  --live-google-workspace-reads \
  --live-sdk \
  --approve-private-context \
  --live-google-workspace-writes \
  --approval-reference anu-174-l174-16-trusted-run \
  --output artifacts/test-pack/workspace-research-brief-lifecycle-live.json
```

Pass requires one selected source identity, one cited `ResearchBrief`, one model
request at or below `$0.05`, and exact marked Doc create/read/update/read/trash
verification with no residual test Doc.

## 3. L174-19 finance-aware BD priority

```bash
npm run run:finance-bd-priority-validation -- \
  --live-airtable-reads \
  --live-sdk \
  --approve-private-context \
  --output artifacts/test-pack/finance-bd-priority-validation-live.json
```

The command refreshes the provider aggregate once in-process because the
private totals are intentionally not persisted in the prior readiness receipt.
Pass requires one model request at or below `$0.05`, visible comparison of both
option identities, exactly one selected priority, finance-aware rationale, an
Opportunity Scout handoff receipt, and no search or write.

## 4. L174-20 weekly packet synthesis and delivery

First synthesize from the already assembled bounded source file:

```bash
npm run run:chief-prior-week-packet -- \
  --input artifacts/test-pack/weekly-chief-packet-2026-07-10-assembly-input.json \
  --live-sdk \
  --approve-external-business-synthesis \
  --redactions-file artifacts/test-pack/weekly-chief-packet-personal-redactions.json \
  --output artifacts/test-pack/weekly-chief-packet-live.json
```

Pass requires one request at or below `$0.05`, every required packet section,
`Operational health` followed by `Packet metadata`, no tools/writes, and the
bounded private-context data-handling receipt.

Then create and verify the durable Doc without repeating synthesis:

```bash
GOOGLE_WORKSPACE_WRITES_ENABLED=true \
npm run deliver:chief-prior-week-packet -- \
  --input artifacts/test-pack/weekly-chief-packet-live.json \
  --live-google-workspace-write \
  --workspace-approval-reference anu-174-l174-20-doc \
  --output artifacts/test-pack/weekly-chief-packet-delivery.json
```

Only after `doc_verified_slack_pending`, post the verified link using the exact
`#ops-finance` channel ID and a separate approval reference. Use the prior Doc
receipt so the post cannot create another Doc:

```bash
npm run deliver:chief-prior-week-packet -- \
  --input artifacts/test-pack/weekly-chief-packet-live.json \
  --live-slack-post \
  --slack-channel-id <ops-finance-channel-id> \
  --slack-approval-reference anu-174-l174-20-slack \
  --resume-delivery-receipt artifacts/test-pack/weekly-chief-packet-delivery.json \
  --output artifacts/test-pack/weekly-chief-packet-delivery-final.json
```

Full L174-20 pass requires verified Doc and Slack receipts. A Slack failure
must retain the Doc receipt and retry only the post.

## Tracker transition

After each row passes, update its ANU-174 scorecard row and retain the local
receipt. When all four pass, run only the focused ANU-174 status tests, mark the
issue Done, and unblock ANU-61 and ANU-203. The controlled Slack pilot remains
the next milestone; legacy Promptfoo remains deferred.
