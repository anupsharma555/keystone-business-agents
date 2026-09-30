---
name: kba-openweight-model-integration
description: Research, install, evaluate, and integrate open-weight models for the keystone-business-agents workspace using external-SSD storage, Ollama, and optionally KBA. Use for model onboarding, upgrades, local runtime compatibility, or connecting a validated local model to KBA; research alone does not authorize downloads or deployment.
---

# KBA Openweight Model Integration

This is a Codex implementation skill, invoked as
`$kba-openweight-model-integration`. It helps Codex onboard different model
families. It is not a prompt pack loaded by Ollama or a KBA runtime agent.

## Scope and source of truth

Work in the selected `keystone-business-agents` checkout or its authorized
worktree. If invoked elsewhere, locate the selected project before editing.
Read its `AGENTS.md`, README, relevant runtime code, and current local settings.
Keep this skill in `codex-skills/`. A Codex discovery link may point to this folder from the user's skills
directory. Do not make Codex skill discovery depend on mounting the model SSD.

### Model storage policy

Download and import model weights directly to the selected external SSD, never
to the computer's internal drive unless the user clearly requests that exception.
This includes original checkpoints, imported model blobs, adapters, auxiliary
model files, download caches, and temporary weight/conversion staging. Do not
download internally first and move the files afterward. Verify the downloader,
runtime server, desktop app, and any conversion tool use the intended storage
before starting a transfer. If a tool cannot honor it, stop and explain the blocker.

An absent or unwritable SSD does not authorize internal storage. Record any
explicit internal-storage exception and its scope; otherwise retain the SSD
default. Local execution still uses the computer's CPU/GPU and memory. Runtime
applications, this Codex skill, and small configuration/report files may remain
in their normal local locations; this policy governs model files and their staging.

Keep skill readiness, model validation, and deployed integration as separate
outcomes, each supported by its own evidence.

## Choose the requested path

1. **Research:** compare official model cards, exact weight licenses, supported
   runtimes/formats, resource needs, and task fit. Read
   [candidate selection and download sources](references/model-selection.md).
   Return a recommendation and
   bounded installation plan. Do not download from a research-only request.
2. **Install or upgrade:** read [SSD and Ollama setup](references/ssd-ollama.md).
   Use an explicit model/revision and storage target; retain restoration data.
3. **Evaluate or add task profiles:** read [evaluation](references/evaluation.md).
   Establish regular-model behavior before testing added instructions.
4. **Connect to KBA:** read [KBA integration](references/kba-integration.md) only
   when requested. Standalone setup does not change production providers, Slack
   routing, automatic fallback, or cloud configuration.

Continue already authorized steps without repeated confirmation. Ask only for
materially missing choices, such as an ambiguous drive, model, or deployment
target. Installing this skill itself does not authorize a model download.

## Intake checklist

- Identify tasks, interfaces, offline requirements, model choice, and acceptable
  responsiveness; do not substitute a different model silently.
- Compare the candidate against the actual installed models and relevant existing
  providers. Name the gap it could fill and a testable advantage: quality, latency,
  memory, offline availability, cost, modality, language, or a specialist task.
  Separate a plausible benefit before download from a measured benefit afterward.
  A different name or larger parameter count is not itself complementary value.
- Verify that the exact weights are downloadable, access/license conditions are
  satisfied, and the artifact can run on the intended computer/runtime. An API demo,
  cloud tag, or repository containing only code is insufficient. For local use,
  report download availability, estimated hardware fit, and observed runtime fit
  separately; do not promise operation from a disk-space estimate.
- Verify hardware, available memory, disk capacity, active workloads, runtime
  version, and actual SSD mount. Storage capacity is not inference memory.
- Link the publisher's model card and license for the exact variant; distinguish
  open weights, open-source tooling, quantized redistributions, and hosted terms.
- Check architecture, quantization, tokenizer/chat template, runtime version,
  context/output limits, thinking controls, and tool/schema support. Do not infer
  compatibility from a family name or active MoE parameter count.
- Inspect installed models and digests before downloads/upgrades. A mutable tag
  or update banner is not proof that weights changed.
- Review task-relevant benchmarks and firsthand user experiences, including
  failures. Record versions, test conditions, source dates and hardware matches;
  distinguish publisher claims, independent measurements, and anecdotes. Use
  these to design local checks, not to claim unmeasured local performance.
- Use synthetic or explicitly authorized local test material. Never upload private
  documents to a hosted service as an incidental part of onboarding.

## Execution and evidence

Start with a short plan naming the selected model, target, bounded tests, and
restoration path. Run one inference request at a time on a task-owned runtime;
do not unload another task's models or stop its server for a clean measurement.
No silent cloud fallback is acceptable. Apply the model storage policy above
to every download, import, and upgrade.

Use existing repository wrappers where they support the selected model. The
Nemotron pilot is a worked example, not a generic adapter: inspect its allowlists
before reusing it for another family. Generalize only the required configuration
and tests; do not copy model-specific defaults into a universal workflow.

Validate resolved storage paths, installed digest, actual runtime settings,
complete input, finished output, and task quality. A model's printed skill header
is a self-report, not a host execution receipt. API success does not prove GUI
behavior, offline operation, or KBA compatibility.

## Handoff checklist

- Report exact model/revision/digest, runtime, quantization, verified storage,
  effective settings, and primary source links.
- State what the candidate adds over the baseline, measured tradeoffs, and whether
  to retain it, keep it experimental, or prefer an existing model for the task.
- Separate API, desktop, offline, and KBA results. Mark each requested task
  **supported in this pilot**, **experimental**, or **unsupported**, with evidence.
- Give cold and warm latency separately, quality failures, and unobserved acceptance
  items. Local inference avoids hosted inference charges but uses local compute,
  memory, storage, and electricity.
- Provide concise launch/select/use, upgrade/restore, and safe-eject directions.
  Keep private paths, raw inputs, and configuration snapshots in ignored `.local/`
  reports; use synthetic values in tracked documentation and fixtures.
- For skill updates, edit this canonical folder, validate metadata/references,
  and perform a scoped behavioral review. Preserve any existing discovery link;
  restoring prior skill files must not alter installed model weights.
