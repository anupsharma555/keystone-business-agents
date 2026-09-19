# KBA V2 isolated experiments

This harness explores seven V2 hypotheses without changing production routes,
registering permanent business agents, adding provider access, or manufacturing
a quality improvement from fixtures. It uses bounded synthetic development and
held-out source packets. Case content and planted-error labels are separate:
planted labels never enter model prompts.

## Experiments and controls

| Experiment | Model variants | Mechanical proof | Main decision |
|---|---|---|---|
| `same_agent_stages` | baseline, self_review | Reopened SQLite checkpoint resumes a failed decision without repeating evidence acquisition | Do same-agent phases improve recovery? |
| `independent_critic` | baseline, self_review, independent_critic | Unknown claim/source IDs and attempted authority grants fail | Does independent criticism outperform self-review? |
| `complementary_research` | baseline, complementary_investigators | Concurrent fixture partitions merge without losing or inflating source identities | Do complementary evidence roles improve coverage? |
| `targeted_repair` | baseline, targeted_repair | A source-bound review can request one revision; missing evidence stops | Does targeted repair recover the actual deficiency? |
| `context_memory` | baseline, compact_context | Duplicate packets shrink while the complete authoritative packet remains identical | Can less context preserve decisions? |
| `checkpoint_replay` | baseline, checkpoint_replay | Production inspection/fork API preserves the source execution and rejects capability expansion | Does diagnostic replay improve debugging? |
| `layout_evidence` | text_only, text_plus_page_images | The same document, text extraction, and verified page PNG retain exact source/page provenance | Does a selected page image recover a decisive missing table cell? |

The complete hypotheses, prerequisites, faults, acceptance/rejection criteria,
later live questions, and existing Linear owners are in
`evals/static/v2_experiments.json`. These are experiment definitions, not claims
that every fault has already been resolved in production.

Four general cases apply to the first six experiments. Two document-layout
question variants apply only to `layout_evidence`, making 26 default control
observations. The latter share one source page; the held-out variant tests new
wording, not generalization to an unseen document.

`baseline` is one tool-free author under the isolated experiment contract. It
is **not** a full current KBA production benchmark. Self-review retains the
author's complete instructions plus a short second-pass addendum, and explicitly
replays the exact prior author input and structured answer. It uses the same
Research owner without inheriting unrelated SDK sessions. Independent criticism
instead starts with review-only instructions and a fresh minimal packet containing
only the unchanged request, sources, requested obligations, and candidate answer.
It does not receive the author's instruction profile or prior context wrappers.
These are real prompt/context differences, not merely different owner labels.

Each case declares bounded requested-obligation IDs. A criticism must target
exactly one existing claim or supplied obligation. Claim criticisms require
supplied source IDs; a missing deliverable can reference its requested obligation
without inventing an existing claim or an irrelevant citation. Unknown obligation
IDs, dual targets, invented sources, and authority grants fail validation. The
mechanical omission control deliberately removes candidate claims and proves
that requested work remains reviewable. This does not prove that a live critic
will correctly identify every omission.

Complementary investigators receive disjoint subsets of the same source universe;
the final author sees both outputs and the original evidence. Agreement is never
counted as corroboration. Live investigator calls remain serial, so parallel
live latency benefits remain unmeasured.

## Run without live calls

Use the repository virtual environment:

```bash
.venv/bin/python scripts/run_v2_experiments.py --list
.venv/bin/python scripts/run_v2_experiments.py --experiment all
.venv/bin/python scripts/run_v2_experiments.py --experiment context_memory --split held_out
.venv/bin/python scripts/run_v2_experiments.py --experiment layout_evidence --case document-layout-development
```

The default run performs scripted positive/negative controls only, with zero
model/provider calls. Phase and replay controls create isolated temporary SQLite
stores and require the optional orchestration dependencies. Missing prerequisites
produce failed checks and skip model execution. They cannot become passing
quality evidence.

The CLI writes `report.json`, `report.md`, and `blinded_outputs.json` under a new
`artifacts/v2-experiments/<run_id>/` directory. These are local run artifacts,
not tracked golden results. Existing reports are not overwritten.

## Explicit live comparison

Live runs require an exact experiment, case, and hard total model-request cap.
The shared SDK budget counts actual calls including retries, and an existing
parent budget cannot be enlarged. Only the project-specific KBA model credential
is used through the normal SDK provider boundary. There are no provider tools,
external writes, inherited conversation sessions, or automatic rollout.

After the operator has authorized a bounded model test, for example:

```bash
.venv/bin/python scripts/run_v2_experiments.py --experiment independent_critic --case validation-held-out --variant independent_critic --live --max-model-requests 3
```

For the input-document comparison, use one text-only request and one request
with the selected page image under the same two-request ceiling:

```bash
.venv/bin/python scripts/run_v2_experiments.py --experiment layout_evidence --case document-layout-development --live --max-model-requests 2
```

The fixture's prospective-clinical-outcomes row says **No / Not measured**.
The deliberately incomplete text extraction drops those two cells. Text-only
answers should acknowledge unavailable values; image-assisted answers can read
the exact cells. Neither variant should confuse retrospective AUROC with
prospective clinical benefit. Gold cell values are never included in text-only
prompt metadata. Both variants cite `layout-page-1` and share the same document,
page, gold-table, and extracted-text fingerprints.

The image variant passes only the exact integrity-checked PNG as a trusted
attachment through the shared SDK wrapper and records the page-image hash.
Model inputs remain structured mappings: nested provider text or serialized JSON
cannot acquire local files implicitly. Native media currently requires the
shared OpenAI input path; unsupported provider configurations fail rather than
silently dropping the image. Fake-SDK tests decode the actual native image and
compare its bytes with the verified fixture; they remain labeled `fake_model`
and never count as live quality evidence.

A baseline uses one initial request. Self-review/independent-critic/supervisor
variants use an initial answer plus review, and at most one conditional revision.
Complementary investigators use two investigations plus a synthesis. Structured
output repairs also consume the shared cap. Budget exhaustion produces a visible
blocked observation; no additional call is made beyond that cap.

Each observation preserves its unchanged task/source fingerprint, effective
variant, input size, owner/phase, usage/cost when available, call count, timing,
typed answer/review, and structural checks. Citation binding is not semantic
truth. Every fake/scripted run and every unreviewed live run reports model quality
as **UNMEASURED**. Do not interpret a passing mechanics row as a quality lift.

## Blinded human assessment

Give reviewers only `blinded_outputs.json`, the original case request/sources,
and the rubric. Variant/phase identity remains in the separate report. Both plain
and sectioned versions are rendered from exactly the same answer, preventing a
layout comparison from quietly changing the underlying facts.

Ratings JSON maps each opaque presentation ID to three integers from 1 to 5:
`correctness`, `usefulness`, and `evidence_visibility`. Unknown IDs, missing rubric
fields, non-integers, and ratings of fake/scripted runs are rejected.

```bash
.venv/bin/python scripts/run_v2_experiments.py --review-report artifacts/v2-experiments/EXACT_RUN/report.json --ratings /tmp/ratings.json --output-dir artifacts/v2-experiments/EXACT_REVIEW
```

Importing ratings runs no models. It records raw judgments without declaring a
winner from a small sample. Additional held-out comparisons, safety parity,
operator benefit, and cost evidence are required before a production decision.

The paired plain/sectioned output views are an ancillary readability control;
they do not replace Experiment 7's **input** text-versus-page-image comparison.
For that experiment, judge factual fidelity using the modality actually supplied:
acknowledging a missing text cell is correct uncertainty, not a failed guess.

## Synthetic document fixture provenance

`evals/fixtures/v2_document_layout.json` contains the full synthetic gold table,
document/page identity, controlled cell omission, generator version, PNG hash,
and extracted-text hash. The companion `.txt` is the deliberately defective
extraction; `.png` is the complete page. The PNG embeds its document ID, page
number, and gold-table hash, all verified before attachment. No real OCR provider
produced this extraction, and this single table does not establish general
document-vision superiority.

Regenerate only in a development environment with Pillow 12.3.0:

```bash
.venv/bin/python scripts/generate_v2_document_layout.py
```

Pillow is required only by this regeneration helper. Runtime input checks and
experiments use the checked-in fixture plus the shared safe file reader; they do
not require Pillow. Review both image appearance and changed hashes after any
intentional fixture revision. The initial page was visually inspected at
1500×660 and its decisive row is legible.

## Related implemented context fixes

The WorkItem Slack session default now matches direct Slack asks at six recent
items when no explicit limit is supplied. Explicit limits remain unchanged;
non-Slack WorkItems retain the existing default. Items may widen to preserve
reasoning/tool-call/output dependency units; this is not a token limit.

Chief WorkItem inputs carry complete Slack context once at the top level rather
than also copying it through target metadata and the Orchestrator packet.
Persisted WorkItem state remains intact. Tests capture actual pre-model input and
compare direct/WorkItem session specifications, rather than inferring fidelity
from a smaller serialized blob.
