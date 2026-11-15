# Local and remote synchronization review

The publication scope is the shared Slack context and continuity implementation,
operating-model defaults, source/repair diagnostics, and README refresh. Local
model experiments and separate comparison/vocabulary work are preserved outside
this publication. No deployment, Slack message or live provider test is part of
this Git synchronization.

## Technical index

- [Remote baseline](#remote-baseline)
- [Publication scope](#publication-scope)
- [Preserved local work](#preserved-local-work)
- [Validation](#validation)
- [Parity verification](#parity-verification)

## Remote baseline

The verified remote base is `main`. Before publication, its tip was `00ea5cb8`.
The source checkout was at `56bb668f`, with no outgoing commits and three incoming
commits. The incoming commits contain the open-weight integration skill and
diagram updates; they do not contain the November context implementation.

| Remote change | Commit date | Treatment |
| --- | --- | --- |
| Last core integration | September 18, 2026 | Starting local baseline |
| Open-weight integration skill | September 29, 2026 | Retain existing remote files unchanged |
| Repository diagram updates | October 1 and November 1, 2026 | Retain upstream image changes |

Commit dates describe the repository history, not runtime deployment dates.

## Publication scope

The focused branch is based on the refreshed remote base, with explicit path
staging and shared-file projections. The registry and its tests retain
production continuity changes while omitting experimental agent cards. The
skills architecture document includes only the shared Orchestrator update.
Experimental prompt-contract tests are omitted alongside their excluded skills.

Included behavior covers channel-bound historical retrieval, relevant approved
operator context, scoped memory capture/retrieval, conversational task linkage,
exact cached-source continuation, bounded source-window reads, one supported
citation correction, readable Slack sections, and failure/usage diagnostics.
Operating agents and the manual planner default to `gpt-6-luna`; the explicit
canary benchmark keeps its separate model contract.

The README adds a live GitHub CI badge, static Python/SDK/state badges, direct
navigation, a continuity overview and the current setup/model policy. Its local
links are checked against repository files. No license, coverage percentage or
universal live-acceptance claim is invented.

## Preserved local work

The source checkout keeps the local-model adapters, optional dependency, GUI and
skill tooling, experimental agents/prompts, synthetic evaluation packets,
comparison outputs and experimental documentation. Experimental registry,
architecture and test hunks remain local even where production changes share a
file. Merely retaining the already-published open-weight onboarding skill does
not publish the experiments.

Two additional nonexperimental workstreams are outside the approved scope:

- Matched comparison protocol: `docs/COMPARISON_PROTOCOL.md`, its executable
  schema/helper, synthetic fixture and tests, plus the differentiation-map link.
- Workflow vocabulary: the field/authority documentation, merged-tag validation
  and corresponding tests.

These omissions are intentional, not evidence of remote parity for those
workstreams. They need their own scoped publication. Environment files, local
state, credentials, databases, logs and caches remain excluded by existing
ignore rules; no ignore-rule change is needed.

## Validation

The publication gate uses the repository's pinned environment, staged-file
preflight, staged-content inspection, secret scanner, Ruff and the full offline
pytest suite. New files enter the Git index before the tracked-file scanner
runs, so that scan includes the intended new source, tests and documentation.

Three broad-gate regressions were reconciled: the Gmail pilot assertion now
follows the new operating model, formal-opportunity validation is checked in
its retained blocker, and named artifact descriptions do not become accidental
provider-write instructions. Focused tests preserve affirmative write gates.
The architecture generators were rerun with the existing date label; their
semantic output was unchanged, so rendering/date-only differences are omitted.

The final local suite passed **8,370 tests**, with **75 skipped**; Ruff, dependency
consistency, staged-file checks and secret scanning also passed. The focused
reconciliation gate passed 167 tests with five skipped. These counts overlap.

Passing offline checks is distinct from live acceptance. The workspace rollout
record retains failed and successful attempts plus pending other-channel,
other-agent and failure-recovery checks. No additional live run is claimed here.

## Parity verification

At handoff, verify the PR's changed-file list against the explicit publication
manifest, fetch the merged remote base, prove the reported merge commit is an
ancestor of that base, and compare every tracked remote blob against the source
checkout. Classify every mismatch using the preserved-work list above; do not
silently ignore unexplained differences.

The README and focused regression fixes are also applied to the original
checkout after checking for concurrent edits. The publication worktree grants
no authority to overwrite experimental content or clean the original checkout.
Private local receipts retain the complete path-level comparison and baseline
hash checks without publishing local account identities or runtime data.
