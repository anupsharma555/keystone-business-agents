# Sandbox Agents Setup Plan

OpenAI Sandbox Agents are a future Keystone capability for workspace-centric review. They should not replace the existing dry-run-first specialist agents. Use them when the agent needs a live filesystem workspace, artifact inspection, or resumable file review. Keep ordinary routing, approvals, live-integration flags, and audit decisions in the Keystone harness.

## Current Status

Sandbox execution is not required for tests. The repository contains import-guarded scaffolding and an explicit execution wrapper in `src/keystone_agents/sandboxing.py`; it builds SDK objects only when the installed `openai-agents` package exposes sandbox classes. If those classes are unavailable, imports still succeed and callers get a clear `SandboxAgentsUnavailable` error only when they request sandbox objects.

## Architecture Boundary

Keep the harness and control plane outside sandbox compute.

- Harness responsibilities: user intent, approval gates, live-integration flags, model configuration, tracing policy, audit records, source review, and artifact release.
- Sandbox responsibilities: file-local inspection, command execution once explicitly enabled by a future capability, draft report generation, document batch review, and workspace-local edits.
- Do not let sandbox output automatically trigger email, Slack, CRM, LinkedIn, scheduling, publication, or file release.
- Treat sandbox artifacts as drafts until the host harness reviews them and records an approval decision.

This follows the OpenAI Agents SDK split where `SandboxAgent` defines the agent and default workspace contract, `Manifest` defines fresh workspace contents, `SandboxRunConfig` chooses the sandbox session source, and `RunConfig` carries the sandbox config into `Runner`.

## When Keystone Should Use Sandbox Agents

Use a sandbox agent when a workflow is naturally file-backed:

- Search/research artifact review: after retrieval and retrieval quality gates, stage SearXNG/hosted web-search packets, result bundles, or normalized source files into a scoped workspace for a second-pass reviewer. This is a third-stage review path that improves search quality; it does not replace live search or source acquisition.
- Mounted research folders: review source bundles, user-curated research folders, or local analysis packets without copying their contents into prompts.
- Generated reports: inspect draft reports, reconcile structured outputs with source files, and produce review notes.
- Document batches: process batches of non-PHI documents where filesystem traversal, chunked review, and artifact output are useful.
- Resumable workspace review: continue a workspace inspection across runs using explicit sandbox session state or a saved snapshot once that lifecycle is implemented.

Do not use sandbox agents for simple Gmail triage, company fixture lookup, opportunity scoring from structured inputs, or outreach drafting that does not need filesystem state.

## Unix-Local Development Shape

The current scaffold supports the local object shape without running a sandbox:

```python
from pathlib import Path

from keystone_agents.sandboxing import (
    SandboxWorkspaceSpec,
    build_keystone_sandbox_manifest,
    build_sandbox_workspace_review_agent,
    build_unix_local_sandbox_run_config,
)

spec = SandboxWorkspaceSpec.from_paths(
    research_dirs=[Path("local_research/company_packet")],
    generated_reports_dir=Path("reports/drafts"),
    document_batches_dir=Path("documents/batch_001"),
    review_workspace_dir=Path("workspaces/review_001"),
)

manifest = build_keystone_sandbox_manifest(spec)
agent = build_sandbox_workspace_review_agent(default_manifest=manifest)
run_config = build_unix_local_sandbox_run_config(manifest=manifest)
```

The resulting objects follow the SDK shape:

- `Manifest(entries={...})` declares fresh workspace inputs.
- `File(content=...)` creates a synthetic `TASK.md` and an `artifacts/README.md`.
- `LocalDir(src=...)` materializes scoped host directories into workspace-relative paths such as `research/01-company_packet`.
- `SandboxAgent(default_manifest=manifest, ...)` defines the future reviewer agent.
- `SandboxRunConfig(client=UnixLocalSandboxClient(), manifest=manifest)` selects Unix-local development.
- `RunConfig(sandbox=SandboxRunConfig(...))` is ready for a future `Runner.run(...)` call.

Do not require real `Runner.run(...)` sandbox execution in tests for this scaffold. Tests should construct or import these objects, use injected fake/local runners for wrapper behavior, and skip object construction when sandbox classes are unavailable.

## Search Review Helper

The scaffold now includes a search-review-specific wrapper for second-pass review
of staged SearXNG/hosted web-search artifacts after retrieval quality gates:

```python
from pathlib import Path

from keystone_agents.sandboxing import (
    SandboxSearchReviewSpec,
    build_search_review_task_instructions,
    build_unix_local_search_review_setup,
    run_sandbox_search_review,
)

spec = SandboxSearchReviewSpec.from_paths(
    packet_dirs=[Path("local_research/search_packets")],
    packet_files=[Path("local_research/search_packets/company_packet.json")],
    prior_reports_dir=Path("reports/drafts"),
    review_workspace_dir=Path("workspaces/search_review_001"),
    additional_instructions=(
        "Focus on source quality, contradictions, stale evidence, and missing evidence."
    ),
)

task_text = build_search_review_task_instructions(spec)
setup = build_unix_local_search_review_setup(spec)
preview = run_sandbox_search_review(spec)
preview_with_web = run_sandbox_search_review(
    spec,
    hosted_web_search=True,
)
```

This helper is intentionally narrow:

- `SandboxSearchReviewSpec` is a convenience wrapper over `SandboxWorkspaceSpec`.
- It is meant for stage three only: retrieval runs first, retrieval quality gates run second, and the sandbox reviewer runs third over the staged artifacts.
- Packet directories are mounted under `research/`.
- Individual staged packet files are copied into manifest entries such as
  `research_packets/01-company_packet.json`.
- `TASK.md` tells the sandbox reviewer it is a second-pass reviewer and says:
  `Do not replace live search, retrieval, or quality-gate logic`.
- Hosted `web_search` is enabled by default when the sandbox search-review step activates, unless the caller explicitly disables it. This is still a stage-three path: the sandbox must use staged artifacts first and only run bounded follow-up search.
- The review checklist explicitly asks for source quality, contradictions, stale
  evidence, and missing evidence.
- Preview mode still defaults to `execute=False`; nothing runs unless execution
  is explicitly requested.

## Explicit Execution Wrapper

`run_sandbox_workspace_review(...)` is the narrow execution wrapper. It defaults
to preview mode and builds the `SandboxAgent`, `Manifest`, and `RunConfig`
without invoking `Runner`.

```python
from pathlib import Path

from keystone_agents.sandboxing import SandboxWorkspaceSpec, run_sandbox_workspace_review

spec = SandboxWorkspaceSpec.from_paths(
    research_dirs=[Path("local_research/company_packet")],
)

preview = run_sandbox_workspace_review(
    spec,
    prompt="Review TASK.md and summarize mounted files.",
)

result = run_sandbox_workspace_review(
    spec,
    prompt="Review TASK.md and write draft notes under artifacts/.",
    execute=True,
    live=True,
)
```

Execution rules:

- `execute=False` is the default and must not call the SDK runner.
- `execute=True` requires either `live=True` for credential-gated SDK execution
  or a caller-supplied fake/local runner or run config for tests.
- Tracing sensitive data remains disabled for sandbox execution.
- The wrapper returns a `SandboxWorkspaceReviewResult` with no-send fields,
  approval-required state, artifact-review-required state, mount summary, and
  audit notes.
- Final output is treated as a draft review artifact. The host harness must
  review and redact it before downstream use.

## Security Rules

- No secrets in prompts, task instructions, manifests, logs, fixture snapshots, or generated artifacts.
- Search review inputs should be sanitized before staging. Do not place raw auth headers, cookies, tokens, or credential dumps into retrieved search packets.
- Mount only scoped directories needed for the task. Do not mount home, root, credential folders, token files, `.ssh`, `.aws`, `.gnupg`, `.config`, or broad unrelated workspaces.
- Keep manifest paths workspace-relative. Do not use absolute workspace entries or `..`.
- Treat generated artifacts as untrusted drafts until the host harness reviews them.
- Review and redact artifacts before moving them out of the sandbox or attaching them to any outbound workflow.
- Do not process PHI or patient-specific information in sandbox workflows.
- Keep live integrations outside the sandbox unless a future implementation adds explicit flags, audit logging, and human approval.

## Future Implementation Notes

The next implementation step should persist the sandbox session state or snapshot identifier in Keystone storage only after redacting sensitive metadata.

Before enabling execution, add tests for:

- missing sandbox SDK classes
- path scope validation
- secret-like task text rejection
- manifest entries for research folders, reports, document batches, and review workspaces
- wrapper preview mode and fake/local runner execution
- search review task generation and staged packet-file manifest entries
- artifact review gates before any file is moved out

## References

- OpenAI Agents SDK sandbox concepts: https://openai.github.io/openai-agents-python/sandbox/guide/
- Manifest reference: https://openai.github.io/openai-agents-python/ref/sandbox/manifest/
- Workspace entries reference: https://openai.github.io/openai-agents-python/ref/sandbox/entries/
- RunConfig and SandboxRunConfig reference: https://openai.github.io/openai-agents-python/ref/run_config/
