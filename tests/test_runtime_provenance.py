from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import keystone_agents.runtime.provenance as provenance
from keystone_agents.runtime.provenance import (
    RUNTIME_FINGERPRINT_SCHEMA,
    build_runtime_fingerprint,
    current_runtime_fingerprint,
)


def test_runtime_fingerprint_is_stable_and_changes_with_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / "src" / "keystone_agents" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    started = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)

    first = build_runtime_fingerprint(
        repo_root=root,
        env={"KEYSTONE_GMAIL_TRIAGE_MODEL": "gpt-test"},
        process_started_at_utc=started,
        source_paths=[source],
    )
    repeated = build_runtime_fingerprint(
        repo_root=root,
        env={"KEYSTONE_GMAIL_TRIAGE_MODEL": "gpt-test"},
        process_started_at_utc=started,
        source_paths=[source],
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")
    changed = build_runtime_fingerprint(
        repo_root=root,
        env={"KEYSTONE_GMAIL_TRIAGE_MODEL": "gpt-test"},
        process_started_at_utc=started,
        source_paths=[source],
    )

    assert first == repeated
    assert first["schema"] == RUNTIME_FINGERPRINT_SCHEMA
    assert first["source_file_count"] == 1
    assert first["source_read_error_count"] == 0
    assert first["source_sha256"] != changed["source_sha256"]
    assert first["runtime_sha256"] != changed["runtime_sha256"]


def test_runtime_fingerprint_hashes_safe_config_and_excludes_secrets(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    secret = "do-not-record-this-secret"
    env = {
        "KEYSTONE_OPENAI_API_KEY": secret,
        "KEYSTONE_GMAIL_CLIENT_SECRET": secret,
        "KEYSTONE_GMAIL_TRIAGE_MODEL": "gpt-a",
        "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
    }
    first = build_runtime_fingerprint(
        repo_root=tmp_path,
        env=env,
        source_paths=[source],
    )
    second = build_runtime_fingerprint(
        repo_root=tmp_path,
        env={**env, "KEYSTONE_OPENAI_API_KEY": "different-secret"},
        source_paths=[source],
    )
    changed_model = build_runtime_fingerprint(
        repo_root=tmp_path,
        env={**env, "KEYSTONE_GMAIL_TRIAGE_MODEL": "gpt-b"},
        source_paths=[source],
    )

    assert first["config_key_count"] == 2
    assert first["config_sha256"] == second["config_sha256"]
    assert first["runtime_sha256"] == second["runtime_sha256"]
    assert first["config_sha256"] != changed_model["config_sha256"]
    assert secret not in str(first)
    assert "KEYSTONE_OPENAI_API_KEY" not in str(first)


def test_current_runtime_fingerprint_is_a_copy() -> None:
    first = current_runtime_fingerprint()
    first["runtime_sha256"] = "mutated"
    first["package_versions"]["openai"] = "mutated"

    second = current_runtime_fingerprint()

    assert second["runtime_sha256"] != "mutated"
    assert second["schema"] == RUNTIME_FINGERPRINT_SCHEMA
    assert second["package_versions"]["openai"] != "mutated"


def test_runtime_fingerprint_detects_installed_version_change_without_manifest_edit(
    tmp_path: Path, monkeypatch,
) -> None:
    installed = {"openai-agents": "0.19.1", "openai": "2.50.0"}
    monkeypatch.setattr(provenance, "version", lambda name: installed.get(name, "1.0"))
    first = build_runtime_fingerprint(repo_root=tmp_path, env={})
    installed["openai"] = "3.12.0"
    changed = build_runtime_fingerprint(repo_root=tmp_path, env={})

    assert first["dependency_sha256"] == changed["dependency_sha256"]
    assert first["runtime_sha256"] != changed["runtime_sha256"]
    assert changed["package_versions"]["openai"] == "3.12.0"
    assert changed["config_key_count"] == 0


def test_runtime_fingerprint_includes_runtime_skills_and_python_patch(tmp_path: Path) -> None:
    skill = tmp_path / "src" / "keystone_agents" / "skills" / "sample" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Read the selected source.\n", encoding="utf-8")
    first = build_runtime_fingerprint(repo_root=tmp_path, env={})
    skill.write_text("Read and verify the selected source.\n", encoding="utf-8")
    changed = build_runtime_fingerprint(repo_root=tmp_path, env={})

    assert first["source_file_count"] == 1
    assert first["source_sha256"] != changed["source_sha256"]
    assert first["runtime_sha256"] != changed["runtime_sha256"]
    assert len(first["python_runtime"].split(".")) == 3
