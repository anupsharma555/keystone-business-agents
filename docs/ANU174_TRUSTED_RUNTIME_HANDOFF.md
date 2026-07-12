# ANU-174 Trusted Runtime Handoff

This is the remaining serial execution queue after repository-side validation.
Do not run it as a batch, do not run Promptfoo, and do not start without a fresh
explicit OpenAI allowance and billing baseline.

## Next live ceiling

- The prior four-request allowance is exhausted. L174-14, L174-16, and L174-19
  are reusable passes; do not rerun them.
- Request fresh approval for exactly one L174-20 `gpt-5.4-mini` request with a
  `$0.05` hard ceiling. Doc and Slack delivery use no OpenAI request.
- Stop after the first unexpected retry, missing usage/cost evidence, structural
  validation failure, provider side effect outside the named scope, or shared
  model-quality defect.
- Patch and rerun only the failed row. Reuse all passing receipts.

## 1. L174-14 local KNI capability summary

Materialize and inspect the exact outbound packet locally first:

```bash
npm run run:local-kni-capability-summary -- \
  --preview-privacy-minimized-context \
  --output artifacts/test-pack/local-kni-capability-summary-privacy-minimized-preview.json
```

The preview must report `local_source_mapping_verified=true`, zero OpenAI
requests and provider writes, and a transmission contract with every private or
reconstructable data class set to `false`.

```bash
npm run run:local-kni-capability-summary -- \
  --live-sdk \
  --approve-privacy-minimized-context \
  --output artifacts/test-pack/local-kni-capability-summary-live.json
```

Pass requires one request, cost at or below `$0.05`, substantive service-area
synthesis cited to sanitized source IDs, uncertainty/human review, verified
local hash-to-source mapping, and no raw excerpts, paths, provider IDs, tools,
search, writes, sends, posts, or persistent response storage. This proves the
privacy-minimized synthesis lane; it does not claim that raw private excerpts
were transmitted.

## 2. L174-16 latest research note to disposable Doc — PASS

Read the selected Doc and inspect the identity-free model packet without a model
or Workspace write first:

```bash
npm run run:workspace-research-brief-lifecycle -- \
  --live-google-workspace-reads \
  --preview-privacy-minimized-context \
  --output artifacts/test-pack/workspace-research-brief-privacy-minimized-preview.json
```

The saved assertion-backed preview and live receipt are reusable. The preview exposes five safe
domain signals plus source-provenance, next-action, and evidence-density
relationships without transmitting the note body or provider identity.

```bash
GOOGLE_WORKSPACE_WRITES_ENABLED=true \
KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true \
npm run run:workspace-research-brief-lifecycle -- \
  --live-google-workspace-reads \
  --live-sdk \
  --approve-privacy-minimized-context \
  --live-google-workspace-writes \
  --approval-reference anu-174-l174-16-assertion-run \
  --output artifacts/test-pack/workspace-research-brief-lifecycle-live.json
```

The passing receipt has one locally mapped sanitized source identity, one useful cited
`ResearchBrief` over the typed assertions, one model request at or below
`$0.05`, and exact marked Doc create/read/update/read/trash verification with no
residual test Doc. The model request must use `store=false`, in-memory caching,
disabled tracing, and no tools/search.

## 3. L174-19 finance-aware BD priority

Refresh the read-only aggregate and inspect the exact privacy-minimized decision
packet before authorizing the model:

```bash
npm run run:finance-bd-priority-validation -- \
  --live-airtable-reads \
  --preview-privacy-minimized-context \
  --output artifacts/test-pack/finance-bd-priority-privacy-minimized-preview.json
```

The current packet includes deterministic negative-margin, high-expense-load,
and uncategorized-gap assertions and has already passed live validation. Reuse
the saved receipt unless the aggregate or derivation contract changes.

```bash
npm run run:finance-bd-priority-validation -- \
  --live-airtable-reads \
  --live-sdk \
  --approve-privacy-minimized-context \
  --output artifacts/test-pack/finance-bd-priority-validation-live.json
```

The command refreshes the provider aggregate once in-process because the exact
private totals are intentionally neither persisted nor transmitted. Pass
requires one model request at or below `$0.05`, visible
comparison of both options, exactly one selected priority, finance-aware
rationale, an Opportunity Scout handoff receipt, and no search or write.

## 4. L174-20 weekly packet synthesis and delivery

The earlier concept-only result must not be delivered. The new packet adds typed
non-identifying workstream, status, owner-role, action-state, and source-family
count assertions. Preview it first, then run one fresh assertion-backed turn:

The existing `artifacts/test-pack/weekly-chief-packet-live.json` is the rejected
concept-only diagnostic and is not a delivery input. A fresh run must overwrite
it with `context_mode=privacy_minimized_assertions`; the delivery command rejects
missing, concept-only, or otherwise stale context modes.

```bash
npm run run:chief-prior-week-packet -- \
  --input artifacts/test-pack/weekly-chief-packet-2026-07-10-assembly-input.json \
  --preview-privacy-minimized-context \
  --output artifacts/test-pack/weekly-chief-packet-privacy-minimized-preview.json
```

```bash
npm run run:chief-prior-week-packet -- \
  --input artifacts/test-pack/weekly-chief-packet-2026-07-10-assembly-input.json \
  --live-sdk \
  --approve-privacy-minimized-context \
  --output artifacts/test-pack/weekly-chief-packet-live.json
```

Pass requires one request at or below `$0.05`, every required packet section,
the supplied assertion counts for Slack, Gmail, completed runs, and Calendar,
`Operational health` followed by `Packet metadata`, no tools/writes, and a
`sanitized_context_proof` receipt. Raw operational summaries, provider IDs,
email addresses, personal names, URLs, and exact timestamps remain local.

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

ANU-174 is 19/20. L174-14, L174-16, and L174-19 have reusable minimized-context
live receipts. L174-20 alone needs one new, separately approved assertion-backed
request followed by its scoped Doc and Slack delivery. When that row passes,
run only focused status checks, mark ANU-174 Done, and unblock ANU-61. ANU-61
already blocks ANU-203 in Linear. Do not fall back to raw trusted-private
transmission. Legacy Promptfoo remains deferred.
