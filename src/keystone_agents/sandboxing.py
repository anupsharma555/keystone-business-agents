"""Import-guarded scaffolding and explicit execution for SandboxAgent workflows.

Sandbox runs are never implicit. This module builds SDK objects when the
installed `openai-agents` package exposes sandbox classes, and only executes
them when a caller provides an explicit local test runner or sets `live=True`.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from keystone_agents.model_provider import (
    DEFAULT_PROVIDER,
    TraceMetadata,
    get_model_config,
    get_trace_config,
)
from keystone_agents.sdk import (
    Dir,
    File,
    LocalDir,
    Manifest,
    RunConfig,
    Runner,
    SandboxAgent,
    SandboxRunConfig,
    UnixLocalSandboxClient,
    WebSearchTool,
    build_live_run_config,
    model_request_budget_hooks,
    validate_sandbox_sdk_available,
    validate_web_search_sdk_available,
)

DEFAULT_SANDBOX_AGENT_NAME = "keystone_sandbox_workspace_reviewer"
DEFAULT_SANDBOX_WORKFLOW_NAME = "Keystone sandbox workspace review"
DEFAULT_SEARCH_REVIEW_WORKFLOW_NAME = "Keystone sandbox search review"
SAFE_SANDBOX_HOST_ENVIRONMENT = frozenset(
    {
        "PATH", "LANG", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_MESSAGES",
        "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "TZ", "TERM", "TMPDIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "UV_PYTHON", "NO_COLOR", "FORCE_COLOR", "CI",
    }
)
DEFAULT_TASK_INSTRUCTIONS = (
    "Review only the files mounted for this Keystone run. Keep approvals, live "
    "integration decisions, and artifact release in the host harness. Write any "
    "draft reports or review notes under artifacts/. Do not put secrets in prompts, "
    "manifests, logs, or generated artifacts."
)
DEFAULT_SEARCH_REVIEW_PROMPT = (
    "Review the staged SearXNG/hosted web-search materials against TASK.md as the "
    "third stage after retrieval and retrieval quality gates. Assess source "
    "quality, contradictions, stale evidence, and missing evidence. Write draft "
    "notes only under artifacts/."
)
DEFAULT_SANDBOX_WEB_SEARCH_USER_LOCATION = {
    "country": "US",
    "timezone": "America/New_York",
}
DEFAULT_SEARCH_REVIEW_QUESTIONS = (
    "Which claims rely on weak, indirect, or discovery-only sources?",
    "Which source claims contradict each other or need reconciliation?",
    "Which sources appear stale for the decision being made?",
    "What important evidence is still missing before downstream use?",
)

_SAFE_ENTRY_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{10,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
)
_FORBIDDEN_MOUNT_PARTS = frozenset(
    {
        ".aws",
        ".config",
        ".env",
        ".gnupg",
        ".ssh",
        "credentials.json",
        "token.json",
    }
)
_FORBIDDEN_MOUNT_NAME_PATTERNS = (
    re.compile(r"client_secret.*\.json\Z", re.IGNORECASE),
    re.compile(r"authorized_user.*\.json\Z", re.IGNORECASE),
    re.compile(r".*credentials?.*\.json\Z", re.IGNORECASE),
    re.compile(r".*token.*\.json\Z", re.IGNORECASE),
)


class SandboxAgentsUnavailable(RuntimeError):
    """Raised when sandbox scaffolding is requested without SDK sandbox classes."""


@dataclass(frozen=True)
class SandboxWorkspaceSpec:
    """Host folders and task text for a fresh sandbox workspace."""

    research_dirs: tuple[Path, ...] = field(default_factory=tuple)
    research_files: tuple[Path, ...] = field(default_factory=tuple)
    generated_reports_dir: Path | None = None
    document_batches_dir: Path | None = None
    review_workspace_dir: Path | None = None
    task_instructions: str = DEFAULT_TASK_INSTRUCTIONS
    require_existing_paths: bool = True

    @classmethod
    def from_paths(
        cls,
        *,
        research_dirs: Sequence[str | Path] = (),
        research_files: Sequence[str | Path] = (),
        generated_reports_dir: str | Path | None = None,
        document_batches_dir: str | Path | None = None,
        review_workspace_dir: str | Path | None = None,
        task_instructions: str = DEFAULT_TASK_INSTRUCTIONS,
        require_existing_paths: bool = True,
    ) -> SandboxWorkspaceSpec:
        """Normalize caller-supplied paths without importing sandbox SDK classes."""

        return cls(
            research_dirs=tuple(Path(path) for path in research_dirs),
            research_files=tuple(Path(path) for path in research_files),
            generated_reports_dir=(
                Path(generated_reports_dir) if generated_reports_dir is not None else None
            ),
            document_batches_dir=(
                Path(document_batches_dir) if document_batches_dir is not None else None
            ),
            review_workspace_dir=(
                Path(review_workspace_dir) if review_workspace_dir is not None else None
            ),
            task_instructions=task_instructions,
            require_existing_paths=require_existing_paths,
        )


@dataclass(frozen=True)
class SandboxSearchReviewSpec:
    """Scoped files and folders for a second-pass sandbox review of staged search artifacts."""

    packet_dirs: tuple[Path, ...] = field(default_factory=tuple)
    packet_files: tuple[Path, ...] = field(default_factory=tuple)
    prior_reports_dir: Path | None = None
    review_workspace_dir: Path | None = None
    review_questions: tuple[str, ...] = DEFAULT_SEARCH_REVIEW_QUESTIONS
    additional_instructions: str | None = None
    require_existing_paths: bool = True

    @classmethod
    def from_paths(
        cls,
        *,
        packet_dirs: Sequence[str | Path] = (),
        packet_files: Sequence[str | Path] = (),
        prior_reports_dir: str | Path | None = None,
        review_workspace_dir: str | Path | None = None,
        review_questions: Sequence[str] = DEFAULT_SEARCH_REVIEW_QUESTIONS,
        additional_instructions: str | None = None,
        require_existing_paths: bool = True,
    ) -> SandboxSearchReviewSpec:
        return cls(
            packet_dirs=tuple(Path(path) for path in packet_dirs),
            packet_files=tuple(Path(path) for path in packet_files),
            prior_reports_dir=Path(prior_reports_dir) if prior_reports_dir is not None else None,
            review_workspace_dir=(
                Path(review_workspace_dir) if review_workspace_dir is not None else None
            ),
            review_questions=tuple(str(question) for question in review_questions),
            additional_instructions=additional_instructions,
            require_existing_paths=require_existing_paths,
        )

    def to_workspace_spec(
        self,
        *,
        allow_hosted_web_search: bool = False,
    ) -> SandboxWorkspaceSpec:
        return SandboxWorkspaceSpec.from_paths(
            research_dirs=self.packet_dirs,
            research_files=self.packet_files,
            generated_reports_dir=self.prior_reports_dir,
            review_workspace_dir=self.review_workspace_dir,
            task_instructions=build_search_review_task_instructions(
                self,
                allow_hosted_web_search=allow_hosted_web_search,
            ),
            require_existing_paths=self.require_existing_paths,
        )


@dataclass(frozen=True)
class SandboxHostedWebSearchConfig:
    """Bounded hosted web-search settings for sandbox second-pass review."""

    external_web_access: bool = True
    search_context_size: Literal["low", "medium", "high"] = "medium"
    user_location: dict[str, str] | None = field(
        default_factory=lambda: dict(DEFAULT_SANDBOX_WEB_SEARCH_USER_LOCATION)
    )


@dataclass(frozen=True)
class SandboxAgentSetup:
    """Bundle for a future or explicit `Runner.run(..., run_config=...)` call."""

    agent: Any
    manifest: Any
    run_config: Any
    mount_summary: dict[str, str]


@dataclass(frozen=True)
class SandboxWorkspaceReviewResult:
    """Dry-run-safe envelope for optional sandbox workspace review execution."""

    setup: SandboxAgentSetup
    prompt: str
    executed: bool = False
    live: bool = False
    final_output: Any | None = None
    approval_required: bool = True
    artifact_review_required: bool = True
    send_enabled: bool = False
    can_send_email: bool = False
    audit_notes: tuple[str, ...] = field(default_factory=tuple)


SandboxRunner = Callable[[Any, str, Any], Any]


def sandbox_agents_available() -> bool:
    """Return whether the installed Agents SDK exposes sandbox classes."""

    try:
        _require_sandbox_sdk()
    except SandboxAgentsUnavailable:
        return False
    return True


def _required_sandbox_classes() -> dict[str, Any]:
    return {
        "Dir": Dir,
        "File": File,
        "LocalDir": LocalDir,
        "Manifest": Manifest,
        "RunConfig": RunConfig,
        "SandboxAgent": SandboxAgent,
        "SandboxRunConfig": SandboxRunConfig,
        "UnixLocalSandboxClient": UnixLocalSandboxClient,
    }


def _require_sandbox_sdk() -> None:
    try:
        validate_sandbox_sdk_available()
    except RuntimeError as exc:
        raise SandboxAgentsUnavailable(str(exc)) from exc

    missing = sorted(name for name, value in _required_sandbox_classes().items() if value is None)
    if missing:
        raise SandboxAgentsUnavailable(
            "OpenAI Agents SDK sandbox scaffolding is unavailable. Missing classes: "
            + ", ".join(missing)
        )


def _reject_secret_text(label: str, text: str) -> None:
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"{label} must not contain secret-like values.")


def _safe_entry_name(prefix: str, path: Path, index: int) -> str:
    base_name = _SAFE_ENTRY_NAME_RE.sub("_", path.name).strip("._")
    if not base_name:
        base_name = f"folder-{index}"
    entry = PurePosixPath(prefix) / f"{index:02d}-{base_name}"
    _validate_manifest_entry(str(entry))
    return str(entry)


def _validate_manifest_entry(entry: str) -> None:
    path = PurePosixPath(entry)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Manifest entry must stay workspace-relative: {entry!r}")


def _resolve_scoped_dir(path: Path, *, require_existing: bool) -> Path:
    expanded = path.expanduser()
    resolved = expanded.resolve(strict=require_existing)
    if require_existing and not resolved.is_dir():
        raise NotADirectoryError(f"Sandbox mount must be a directory: {resolved}")
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise ValueError("Sandbox mounts must be scoped directories, not root or home.")
    forbidden_name = any(
        pattern.fullmatch(part)
        for part in resolved.parts
        for pattern in _FORBIDDEN_MOUNT_NAME_PATTERNS
    )
    if _FORBIDDEN_MOUNT_PARTS.intersection(resolved.parts) or forbidden_name:
        raise ValueError(f"Sandbox mount may expose credential material: {resolved}")
    _reject_secret_text("Sandbox mount path", str(resolved))
    return resolved


def _resolve_scoped_file(path: Path, *, require_existing: bool) -> Path:
    expanded = path.expanduser()
    resolved = expanded.resolve(strict=require_existing)
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"Sandbox staged file must be a file: {resolved}")
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise ValueError("Sandbox staged files must be scoped paths, not root or home.")
    forbidden_name = any(
        pattern.fullmatch(part)
        for part in resolved.parts
        for pattern in _FORBIDDEN_MOUNT_NAME_PATTERNS
    )
    if _FORBIDDEN_MOUNT_PARTS.intersection(resolved.parts) or forbidden_name:
        raise ValueError(f"Sandbox staged file may expose credential material: {resolved}")
    _reject_secret_text("Sandbox staged file path", str(resolved))
    return resolved


def _add_local_dir_entries(
    entries: dict[str, Any],
    summary: dict[str, str],
    *,
    prefix: str,
    paths: Sequence[Path],
    require_existing: bool,
) -> None:
    for index, path in enumerate(paths, start=1):
        resolved = _resolve_scoped_dir(path, require_existing=require_existing)
        entry_name = _safe_entry_name(prefix, resolved, index)
        entries[entry_name] = LocalDir(src=resolved)
        summary[entry_name] = str(resolved)


def _add_staged_file_entries(
    entries: dict[str, Any],
    summary: dict[str, str],
    *,
    prefix: str,
    paths: Sequence[Path],
    require_existing: bool,
) -> None:
    for index, path in enumerate(paths, start=1):
        resolved = _resolve_scoped_file(path, require_existing=require_existing)
        entry_name = _safe_entry_name(prefix, resolved, index)
        content = resolved.read_bytes()
        _reject_secret_text("Sandbox staged file content", content.decode("utf-8", errors="ignore"))
        entries[entry_name] = File(content=content)
        summary[entry_name] = str(resolved)


def build_search_review_task_instructions(
    spec: SandboxSearchReviewSpec | None = None,
    *,
    allow_hosted_web_search: bool = False,
) -> str:
    """Render task text for sandbox review of staged search or research artifacts."""

    selected_spec = spec or SandboxSearchReviewSpec()
    questions = (
        tuple(question.strip() for question in selected_spec.review_questions if question.strip())
        or DEFAULT_SEARCH_REVIEW_QUESTIONS
    )
    lines = [
        "You are a second-pass reviewer for already retrieved SearXNG/hosted web-search artifacts.",
        "This sandbox review is stage three, after retrieval and retrieval quality gates.",
        (
            "Do not replace live search, retrieval, or quality-gate logic. "
            "Use the staged workspace first."
        ),
        "Review only the staged workspace files.",
        "Prioritize source quality, contradictions, stale evidence, and missing evidence.",
        "Flag unsupported or discovery-only claims before downstream use.",
        "Write draft review notes and draft artifacts only under artifacts/.",
        "Keep approvals, outbound actions, and artifact release in the host harness.",
        "",
        "Review checklist:",
    ]
    if allow_hosted_web_search:
        lines.insert(
            2,
            (
                "You may use the hosted web search tool only to corroborate a staged claim, "
                "reconcile a contradiction, or fill a clearly identified evidence gap."
            ),
        )
        lines.insert(
            3,
            (
                "Keep follow-up search narrow and source-driven. Do not restart broad discovery "
                "from scratch, and cite any new sources in draft artifacts."
            ),
        )
    else:
        lines.insert(2, "Do not fetch new sources.")
    lines.extend(f"- {question}" for question in questions)
    if selected_spec.additional_instructions:
        _reject_secret_text(
            "Sandbox search review additional instructions",
            selected_spec.additional_instructions,
        )
        lines.extend(
            ["", "Additional reviewer instructions:", selected_spec.additional_instructions]
        )
    return "\n".join(lines)


def build_sandbox_hosted_web_search_tool(
    config: SandboxHostedWebSearchConfig | None = None,
) -> Any:
    """Build the OpenAI hosted web-search tool for bounded sandbox follow-up search."""

    validate_web_search_sdk_available()
    selected = config or SandboxHostedWebSearchConfig()
    return WebSearchTool(
        user_location=selected.user_location,
        search_context_size=selected.search_context_size,
        external_web_access=selected.external_web_access,
    )


def _build_manifest_entries(
    workspace: SandboxWorkspaceSpec,
) -> tuple[dict[str, Any], dict[str, str]]:
    _reject_secret_text("Sandbox task instructions", workspace.task_instructions)
    entries: dict[str, Any] = {
        "TASK.md": File(content=workspace.task_instructions.encode("utf-8")),
        "artifacts": Dir(
            children={
                "README.md": File(
                    content=(
                        b"Draft reports and review notes go here. The host harness "
                        b"reviews artifacts before moving them out of the sandbox.\n"
                    )
                )
            }
        ),
    }
    summary: dict[str, str] = {}

    _add_local_dir_entries(
        entries,
        summary,
        prefix="research",
        paths=workspace.research_dirs,
        require_existing=workspace.require_existing_paths,
    )
    _add_staged_file_entries(
        entries,
        summary,
        prefix="research_packets",
        paths=workspace.research_files,
        require_existing=workspace.require_existing_paths,
    )

    optional_dirs = (
        ("generated_reports", workspace.generated_reports_dir),
        ("document_batches", workspace.document_batches_dir),
        ("review_workspace", workspace.review_workspace_dir),
    )
    for prefix, path in optional_dirs:
        if path is not None:
            _add_local_dir_entries(
                entries,
                summary,
                prefix=prefix,
                paths=(path,),
                require_existing=workspace.require_existing_paths,
            )

    return entries, summary


def build_keystone_sandbox_manifest(
    spec: SandboxWorkspaceSpec | None = None,
) -> Any:
    """Build a fresh-workspace `Manifest` without starting a sandbox session."""

    _require_sandbox_sdk()
    workspace = spec or SandboxWorkspaceSpec()
    entries, summary = _build_manifest_entries(workspace)
    manifest = Manifest(entries=entries)
    try:
        manifest.keystone_mount_summary = summary
    except Exception:
        pass
    return manifest


def build_sandbox_workspace_review_agent(
    *,
    default_manifest: Any | None = None,
    instructions: str = DEFAULT_TASK_INSTRUCTIONS,
    model: str | None = None,
    name: str = DEFAULT_SANDBOX_AGENT_NAME,
    tools: Sequence[Any] = (),
) -> Any:
    """Build a non-running `SandboxAgent` for workspace review scaffolding."""

    _require_sandbox_sdk()
    _reject_secret_text("Sandbox agent instructions", instructions)
    selected_manifest = default_manifest or build_keystone_sandbox_manifest()
    selected_model = model or get_model_config().model
    return SandboxAgent(
        name=name,
        instructions=instructions,
        model=selected_model,
        default_manifest=selected_manifest,
        tools=list(tools),
    )


def build_unix_local_sandbox_run_config(
    *,
    manifest: Any | None = None,
    model: str | None = None,
    workflow_name: str = DEFAULT_SANDBOX_WORKFLOW_NAME,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool = True,
    trace_include_sensitive_data: bool = False,
    client: Any | None = None,
) -> Any:
    """Build a Unix-local `RunConfig` with `SandboxRunConfig` but do not run it."""

    _require_sandbox_sdk()
    trace_config = get_trace_config().with_overrides(
        workflow_name=workflow_name,
        group_id=group_id,
        trace_metadata=trace_metadata,
        tracing_disabled=tracing_disabled,
        trace_include_sensitive_data=trace_include_sensitive_data,
    )
    sandbox_config = SandboxRunConfig(
        client=client if client is not None else _build_scoped_unix_local_client(),
        manifest=manifest,
    )
    return RunConfig(
        model=model or get_model_config().model,
        sandbox=sandbox_config,
        workflow_name=trace_config.workflow_name,
        group_id=trace_config.group_id,
        trace_metadata=dict(trace_config.trace_metadata) or None,
        tracing_disabled=trace_config.tracing_disabled,
        trace_include_sensitive_data=trace_config.trace_include_sensitive_data,
    )


def _build_scoped_unix_local_client() -> Any:
    parameters = inspect.signature(UnixLocalSandboxClient).parameters
    if "inherit_host_environment" in parameters:
        return UnixLocalSandboxClient(
            inherit_host_environment=False,
            host_environment_allowlist=SAFE_SANDBOX_HOST_ENVIRONMENT,
        )
    # Old SDKs may still construct previews and use injected fake runners.
    # Real execution is rejected below until environment isolation is supported.
    return UnixLocalSandboxClient()


def _validate_unix_local_execution_environment(run_config: Any) -> None:
    client = getattr(getattr(run_config, "sandbox", None), "client", None)
    if client is None:
        raise SandboxAgentsUnavailable(
            "Real sandbox execution requires an explicit sandbox client; "
            "an implicit host-environment client is not permitted."
        )
    if UnixLocalSandboxClient is None or not isinstance(client, UnixLocalSandboxClient):
        return
    # The SDK currently exposes no public policy accessor. Its private allowlist
    # is None when inheriting the host environment, including on older releases.
    # Fail closed if this compatibility boundary changes rather than run unsafely.
    allowlist = getattr(client, "_host_environment_allowlist", None)
    if not isinstance(allowlist, set | frozenset) or not allowlist.issubset(
        SAFE_SANDBOX_HOST_ENVIRONMENT
    ):
        raise SandboxAgentsUnavailable(
            "Unix-local sandbox execution requires SDK environment isolation with "
            "a conservative host-variable allowlist. Preview and injected test runners "
            "remain available; host credential inheritance is not permitted."
        )


def build_unix_local_sandbox_setup(
    spec: SandboxWorkspaceSpec | None = None,
    *,
    model: str | None = None,
    workflow_name: str = DEFAULT_SANDBOX_WORKFLOW_NAME,
    client: Any | None = None,
    tools: Sequence[Any] = (),
) -> SandboxAgentSetup:
    """Build agent, manifest, and run config objects for local development."""

    workspace = spec or SandboxWorkspaceSpec()
    _require_sandbox_sdk()
    entries, mount_summary = _build_manifest_entries(workspace)
    manifest = Manifest(entries=entries)
    agent = build_sandbox_workspace_review_agent(
        default_manifest=manifest,
        model=model,
        instructions=workspace.task_instructions,
        tools=tools,
    )
    run_config = build_unix_local_sandbox_run_config(
        manifest=manifest,
        model=model,
        workflow_name=workflow_name,
        client=client,
    )
    return SandboxAgentSetup(
        agent=agent,
        manifest=manifest,
        run_config=run_config,
        mount_summary=mount_summary,
    )


def _run_with_sdk_runner(agent: Any, prompt: str, run_config: Any) -> Any:
    """Call the SDK runner with a sandbox run config."""

    _validate_unix_local_execution_environment(run_config)
    hooks = model_request_budget_hooks()
    return Runner.run_sync(agent, prompt, run_config=run_config, hooks=hooks)


def _sandbox_result_notes(
    *,
    executed: bool,
    live: bool,
    hosted_web_search: bool = False,
) -> tuple[str, ...]:
    notes = [
        "Sandbox review is workspace-scoped and does not grant outbound permissions.",
        "Host harness remains responsible for approvals, artifact release, and audit records.",
        "Generated sandbox artifacts must be reviewed and redacted before downstream use.",
        "No email, Slack, CRM, LinkedIn, scheduling, publishing, or send action is enabled.",
    ]
    if hosted_web_search:
        notes.append(
            "Hosted web search is attached for bounded corroboration, but staged files remain "
            "the primary review input."
        )
    if executed:
        notes.append(
            "Sandbox execution was explicitly requested with "
            + ("live SDK credentials." if live else "a caller-supplied local runner.")
        )
    else:
        notes.append("Sandbox setup was built without executing Runner.")
    return tuple(notes)


def _final_output(value: Any) -> Any:
    return getattr(value, "final_output", value)


def build_unix_local_search_review_setup(
    spec: SandboxSearchReviewSpec,
    *,
    model: str | None = None,
    workflow_name: str = DEFAULT_SEARCH_REVIEW_WORKFLOW_NAME,
    client: Any | None = None,
    hosted_web_search: bool = False,
    hosted_web_search_config: SandboxHostedWebSearchConfig | None = None,
) -> SandboxAgentSetup:
    """Build sandbox objects for stage-three review of staged search artifacts."""

    tools: list[Any] = []
    if hosted_web_search:
        tools.append(build_sandbox_hosted_web_search_tool(hosted_web_search_config))
    return build_unix_local_sandbox_setup(
        spec.to_workspace_spec(allow_hosted_web_search=hosted_web_search),
        model=model,
        workflow_name=workflow_name,
        client=client,
        tools=tools,
    )


def run_sandbox_workspace_review(
    spec: SandboxWorkspaceSpec | None = None,
    *,
    prompt: str,
    execute: bool = False,
    live: bool = False,
    model: str | None = None,
    workflow_name: str = DEFAULT_SANDBOX_WORKFLOW_NAME,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    client: Any | None = None,
    runner: SandboxRunner | None = None,
    run_config: Any | None = None,
    tools: Sequence[Any] = (),
    hosted_web_search: bool = False,
) -> SandboxWorkspaceReviewResult:
    """Build and optionally run a Unix-local sandbox workspace review.

    The default path is a preview: it constructs the agent, manifest, and
    run_config without invoking `Runner`. Execution requires either `live=True`
    for credential-gated SDK execution or a caller-supplied runner/run_config
    for local tests. The wrapper never enables outbound side effects.
    """

    _require_sandbox_sdk()
    _reject_secret_text("Sandbox review prompt", prompt)
    setup = build_unix_local_sandbox_setup(
        spec,
        model=model,
        workflow_name=workflow_name,
        client=client,
        tools=tools,
    )

    if not execute:
        return SandboxWorkspaceReviewResult(
            setup=setup,
            prompt=prompt,
            executed=False,
            live=False,
            audit_notes=_sandbox_result_notes(
                executed=False,
                live=False,
                hosted_web_search=hosted_web_search,
            ),
        )

    if run_config is None and runner is None and not live:
        raise RuntimeError(
            "Sandbox execution requires live=True for credential-gated SDK execution "
            "or a caller-supplied local runner/run_config for tests."
        )

    selected_run_config = run_config
    if selected_run_config is None:
        if live:
            model_config = get_model_config()
            if model is not None:
                model_config = replace(model_config, model=model)
            selected_run_config = build_live_run_config(
                model_config,
                workflow_name=workflow_name,
                group_id=group_id,
                trace_metadata=trace_metadata,
                tracing_disabled=True,
                trace_include_sensitive_data=False,
                sandbox=setup.run_config.sandbox,
            )
        else:
            selected_run_config = setup.run_config

    raw_result = (runner or _run_with_sdk_runner)(
        setup.agent,
        prompt,
        selected_run_config,
    )
    final_output = _final_output(raw_result)
    if final_output is not None:
        _reject_secret_text("Sandbox final output", str(final_output))

    return SandboxWorkspaceReviewResult(
        setup=setup,
        prompt=prompt,
        executed=True,
        live=live and runner is None,
        final_output=final_output,
        audit_notes=_sandbox_result_notes(
            executed=True,
            live=live and runner is None,
            hosted_web_search=hosted_web_search,
        ),
    )


def run_sandbox_search_review(
    spec: SandboxSearchReviewSpec,
    *,
    prompt: str = DEFAULT_SEARCH_REVIEW_PROMPT,
    execute: bool = False,
    live: bool = False,
    model: str | None = None,
    workflow_name: str = DEFAULT_SEARCH_REVIEW_WORKFLOW_NAME,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    client: Any | None = None,
    runner: SandboxRunner | None = None,
    run_config: Any | None = None,
    hosted_web_search: bool = False,
    hosted_web_search_config: SandboxHostedWebSearchConfig | None = None,
) -> SandboxWorkspaceReviewResult:
    """Build and optionally run a stage-three sandbox review over staged search artifacts."""

    tools: list[Any] = []
    if hosted_web_search:
        tools.append(build_sandbox_hosted_web_search_tool(hosted_web_search_config))
        if execute and live:
            model_config = get_model_config()
            if model is not None:
                model_config = replace(model_config, model=model)
            if model_config.provider != DEFAULT_PROVIDER:
                raise RuntimeError(
                    "Sandbox hosted web search requires MODEL_PROVIDER=openai because "
                    "OpenAI hosted web_search is only supported on the OpenAI Responses path."
                )

    return run_sandbox_workspace_review(
        spec.to_workspace_spec(allow_hosted_web_search=hosted_web_search),
        prompt=prompt,
        execute=execute,
        live=live,
        model=model,
        workflow_name=workflow_name,
        group_id=group_id,
        trace_metadata=trace_metadata,
        client=client,
        runner=runner,
        run_config=run_config,
        tools=tools,
        hosted_web_search=hosted_web_search,
    )
