from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from keystone_agents import sandboxing

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FakeSandboxRunResult:
    final_output: str


def _sandbox_or_skip() -> None:
    if not sandboxing.sandbox_agents_available():
        pytest.skip("OpenAI Agents SDK sandbox classes are unavailable.")


def _workspace_spec(tmp_path: Path) -> sandboxing.SandboxWorkspaceSpec:
    research = tmp_path / "research"
    reports = tmp_path / "reports"
    documents = tmp_path / "documents"
    review = tmp_path / "review"
    for path in (research, reports, documents, review):
        path.mkdir()

    return sandboxing.SandboxWorkspaceSpec.from_paths(
        research_dirs=[research],
        generated_reports_dir=reports,
        document_batches_dir=documents,
        review_workspace_dir=review,
    )


def _search_review_spec(tmp_path: Path) -> sandboxing.SandboxSearchReviewSpec:
    packets = tmp_path / "search_packets"
    reports = tmp_path / "reports"
    review = tmp_path / "review"
    for path in (packets, reports, review):
        path.mkdir()

    packet_file = packets / "company_packet.json"
    packet_file.write_text(
        '{"company":"Example Health","claims":["Series B announced in 2021"],'
        '"sources":[{"url":"https://example.com/news"}]}',
        encoding="utf-8",
    )

    return sandboxing.SandboxSearchReviewSpec.from_paths(
        packet_dirs=[packets],
        packet_files=[packet_file],
        prior_reports_dir=reports,
        review_workspace_dir=review,
        additional_instructions="Focus on whether funding and partnership claims remain current.",
    )


def test_sandbox_docs_cover_keystone_use_cases_and_security() -> None:
    text = (PROJECT_ROOT / "docs" / "SANDBOX_AGENTS.md").read_text(encoding="utf-8")

    required_phrases = [
        "harness and control plane outside sandbox compute",
        "Mounted research folders",
        "Search/research artifact review",
        "after retrieval and retrieval quality gates",
        "SearXNG/hosted web-search",
        "Generated reports",
        "Document batches",
        "Resumable workspace review",
        "second-pass reviewer",
        "Do not replace live search, retrieval, or quality-gate logic",
        "Hosted `web_search` is enabled by default when the sandbox search-review step activates",
        "source quality, contradictions, stale evidence, and missing evidence",
        "research_packets/01-company_packet.json",
        "run_sandbox_search_review",
        "No secrets in prompts",
        "Mount only scoped directories",
        "Review and redact artifacts before moving them out",
        "Manifest(entries={...})",
        "LocalDir(src=...)",
        "File(content=...)",
        "SandboxAgent(default_manifest=manifest",
        "SandboxRunConfig(client=UnixLocalSandboxClient()",
        "RunConfig(sandbox=SandboxRunConfig",
        "run_sandbox_workspace_review",
        "execute=True",
        "live=True",
    ]
    for phrase in required_phrases:
        assert phrase in text


def test_openai_agents_dependency_range_is_0_19() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "openai-agents>=0.19.1,<0.20" in config["project"]["dependencies"]


def test_sandbox_scaffold_is_import_guarded_when_classes_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandboxing, "SandboxAgent", None)

    assert sandboxing.sandbox_agents_available() is False
    with pytest.raises(sandboxing.SandboxAgentsUnavailable):
        sandboxing.build_sandbox_workspace_review_agent()


def test_unix_local_sandbox_setup_builds_sdk_objects_without_running(
    tmp_path: Path,
) -> None:
    _sandbox_or_skip()

    setup = sandboxing.build_unix_local_sandbox_setup(_workspace_spec(tmp_path))

    assert type(setup.agent).__name__ == "SandboxAgent"
    assert type(setup.manifest).__name__ == "Manifest"
    assert type(setup.run_config).__name__ == "RunConfig"
    assert setup.agent.default_manifest is setup.manifest
    assert setup.run_config.sandbox.manifest is setup.manifest
    assert type(setup.run_config.sandbox.client).__name__ == "UnixLocalSandboxClient"


def test_sandbox_execution_wrapper_defaults_to_preview_without_running(
    tmp_path: Path,
) -> None:
    _sandbox_or_skip()

    result = sandboxing.run_sandbox_workspace_review(
        _workspace_spec(tmp_path),
        prompt="Review TASK.md and summarize mounted files.",
    )

    assert result.executed is False
    assert result.live is False
    assert result.final_output is None
    assert result.send_enabled is False
    assert result.can_send_email is False
    assert result.approval_required is True
    assert result.artifact_review_required is True
    assert result.setup.mount_summary
    assert any("without executing Runner" in note for note in result.audit_notes)


def test_sandbox_execution_wrapper_requires_explicit_execution_path(
    tmp_path: Path,
) -> None:
    _sandbox_or_skip()

    with pytest.raises(RuntimeError, match="requires live=True"):
        sandboxing.run_sandbox_workspace_review(
            _workspace_spec(tmp_path),
            prompt="Review TASK.md.",
            execute=True,
        )


def test_sandbox_execution_wrapper_uses_injected_local_runner(
    tmp_path: Path,
) -> None:
    _sandbox_or_skip()
    calls: list[dict[str, object]] = []

    def fake_runner(agent: object, prompt: str, run_config: object) -> FakeSandboxRunResult:
        calls.append({"agent": agent, "prompt": prompt, "run_config": run_config})
        return FakeSandboxRunResult(final_output="Sandbox review complete.")

    result = sandboxing.run_sandbox_workspace_review(
        _workspace_spec(tmp_path),
        prompt="Review TASK.md and write only draft notes under artifacts/.",
        execute=True,
        runner=fake_runner,
    )

    assert calls
    assert result.executed is True
    assert result.live is False
    assert result.final_output == "Sandbox review complete."
    assert result.send_enabled is False
    assert result.artifact_review_required is True
    assert calls[0]["agent"] is result.setup.agent
    assert calls[0]["run_config"] is result.setup.run_config


def test_sandbox_execution_wrapper_rejects_secret_like_prompt(tmp_path: Path) -> None:
    _sandbox_or_skip()

    with pytest.raises(ValueError, match="secret-like"):
        sandboxing.run_sandbox_workspace_review(
            _workspace_spec(tmp_path),
            prompt="Review this workspace with sk-" + "a" * 24,
        )


def test_sandbox_manifest_uses_file_and_scoped_local_dir_entries(tmp_path: Path) -> None:
    _sandbox_or_skip()

    setup = sandboxing.build_unix_local_sandbox_setup(_workspace_spec(tmp_path))
    entries = setup.manifest.entries

    assert type(entries["TASK.md"]).__name__ == "File"
    assert type(entries["artifacts"]).__name__ == "Dir"
    for prefix in ("research", "generated_reports", "document_batches", "review_workspace"):
        keys = [key for key in entries if str(key).startswith(f"{prefix}/")]
        assert keys, f"missing {prefix} entry"
        assert type(entries[keys[0]]).__name__ == "LocalDir"
        assert keys[0] in setup.mount_summary


def test_sandbox_manifest_rejects_secret_like_task_text(tmp_path: Path) -> None:
    _sandbox_or_skip()

    spec = _workspace_spec(tmp_path)
    unsafe = sandboxing.SandboxWorkspaceSpec.from_paths(
        research_dirs=spec.research_dirs,
        task_instructions="Inspect docs with sk-" + "a" * 24,
    )

    with pytest.raises(ValueError, match="secret-like"):
        sandboxing.build_keystone_sandbox_manifest(unsafe)


def test_sandbox_manifest_rejects_unscoped_credential_mount(tmp_path: Path) -> None:
    _sandbox_or_skip()

    credential_dir = tmp_path / ".ssh"
    credential_dir.mkdir()
    spec = sandboxing.SandboxWorkspaceSpec.from_paths(research_dirs=[credential_dir])

    with pytest.raises(ValueError, match="credential material"):
        sandboxing.build_keystone_sandbox_manifest(spec)


@pytest.mark.parametrize(
    "mount_name",
    [
        ".env",
        ".aws",
        ".config",
        ".gnupg",
        ".ssh",
        "credentials.json",
        "token.json",
        "client_secret_123.apps.googleusercontent.com.json",
        "authorized_user_oauth.json",
        "google_credentials_fixture.json",
        "oauth_token_fixture.json",
    ],
)
def test_sandbox_manifest_rejects_credential_like_mount_names(
    tmp_path: Path,
    mount_name: str,
) -> None:
    _sandbox_or_skip()

    credential_dir = tmp_path / mount_name
    credential_dir.mkdir()
    spec = sandboxing.SandboxWorkspaceSpec.from_paths(research_dirs=[credential_dir])

    with pytest.raises(ValueError, match="credential material"):
        sandboxing.build_keystone_sandbox_manifest(spec)


def test_sandbox_manifest_rejects_home_and_root_mounts() -> None:
    _sandbox_or_skip()

    for broad_path in (Path("/"), Path.home()):
        spec = sandboxing.SandboxWorkspaceSpec.from_paths(research_dirs=[broad_path])
        with pytest.raises(ValueError, match="root or home"):
            sandboxing.build_keystone_sandbox_manifest(spec)


def test_search_review_task_instructions_cover_second_pass_review() -> None:
    spec = sandboxing.SandboxSearchReviewSpec.from_paths(
        review_questions=[
            "Check source quality.",
            "Check stale evidence.",
        ],
        additional_instructions="Escalate contradictions that would block outreach use.",
        require_existing_paths=False,
    )

    instructions = sandboxing.build_search_review_task_instructions(spec)

    assert "second-pass reviewer" in instructions
    assert "SearXNG/hosted web-search" in instructions
    assert "after retrieval and retrieval quality gates" in instructions
    assert "Do not replace live search, retrieval, or quality-gate logic" in instructions
    assert "source quality" in instructions
    assert "stale evidence" in instructions
    assert "Additional reviewer instructions" in instructions
    assert "block outreach use" in instructions
    assert "Do not fetch new sources." in instructions


def test_search_review_task_instructions_can_allow_bounded_hosted_web_search() -> None:
    instructions = sandboxing.build_search_review_task_instructions(allow_hosted_web_search=True)

    assert "hosted web search tool" in instructions
    assert "Use the staged workspace first." in instructions
    assert "Do not fetch new sources." not in instructions
    assert "Do not restart broad discovery from scratch" in instructions


def test_search_review_spec_converts_to_workspace_spec(tmp_path: Path) -> None:
    spec = _search_review_spec(tmp_path)

    workspace = spec.to_workspace_spec()

    assert workspace.research_dirs == spec.packet_dirs
    assert workspace.research_files == spec.packet_files
    assert workspace.generated_reports_dir == spec.prior_reports_dir
    assert workspace.review_workspace_dir == spec.review_workspace_dir
    assert "second-pass reviewer" in workspace.task_instructions
    assert "after retrieval and retrieval quality gates" in workspace.task_instructions


def test_search_review_spec_can_render_hosted_web_search_task_text(tmp_path: Path) -> None:
    spec = _search_review_spec(tmp_path)

    workspace = spec.to_workspace_spec(allow_hosted_web_search=True)

    assert "hosted web search tool" in workspace.task_instructions


def test_search_review_setup_stages_packet_files_and_task(tmp_path: Path) -> None:
    _sandbox_or_skip()

    setup = sandboxing.build_unix_local_search_review_setup(_search_review_spec(tmp_path))
    entries = setup.manifest.entries

    assert type(entries["TASK.md"]).__name__ == "File"
    assert "research_packets/01-company_packet.json" in entries
    assert type(entries["research_packets/01-company_packet.json"]).__name__ == "File"
    assert "research/01-search_packets" in entries
    assert setup.mount_summary["research_packets/01-company_packet.json"].endswith(
        "company_packet.json"
    )


def test_search_review_setup_can_attach_hosted_web_search_tool(tmp_path: Path) -> None:
    _sandbox_or_skip()

    setup = sandboxing.build_unix_local_search_review_setup(
        _search_review_spec(tmp_path),
        hosted_web_search=True,
    )

    assert len(setup.agent.tools) == 1
    tool = setup.agent.tools[0]
    assert type(tool).__name__ == "WebSearchTool"
    assert tool.external_web_access is True
    assert tool.search_context_size == "medium"
    assert tool.user_location["country"] == "US"


def test_search_review_wrapper_defaults_to_preview_without_running(
    tmp_path: Path,
) -> None:
    _sandbox_or_skip()

    result = sandboxing.run_sandbox_search_review(_search_review_spec(tmp_path))

    assert result.executed is False
    assert result.live is False
    assert result.final_output is None
    assert result.send_enabled is False
    assert "research_packets/01-company_packet.json" in result.setup.mount_summary
    assert any("without executing Runner" in note for note in result.audit_notes)


def test_search_review_wrapper_preview_can_include_hosted_web_search(tmp_path: Path) -> None:
    _sandbox_or_skip()

    result = sandboxing.run_sandbox_search_review(
        _search_review_spec(tmp_path),
        hosted_web_search=True,
    )

    assert len(result.setup.agent.tools) == 1
    assert any("Hosted web search is attached" in note for note in result.audit_notes)


def test_search_review_wrapper_rejects_live_hosted_web_search_on_non_openai_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sandbox_or_skip()

    monkeypatch.setattr(
        sandboxing,
        "get_model_config",
        lambda: type("Config", (), {"provider": "gemini", "model": "gemini-2.5-flash"})(),
    )

    with pytest.raises(RuntimeError, match="MODEL_PROVIDER=openai"):
        sandboxing.run_sandbox_search_review(
            _search_review_spec(tmp_path),
            execute=True,
            live=True,
            hosted_web_search=True,
        )
