from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import scripts.run_keystone_automation as automation
from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.storage.sqlite_store import SQLiteStore


class _Completed:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _db_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _base_args(tmp_path: Path) -> list[str]:
    return [
        "--no-health-preflight",
        "--lock-file",
        str(tmp_path / "automation.lock"),
        "--json",
    ]


def _child_payload() -> str:
    return json.dumps(
        {
            "storage": {
                "agent_run": {"id": 42},
                "items": [{"approval_queue_item": {"id": "approval_1"}}],
            }
        }
    )


def test_weekly_dry_run_uses_save_without_live_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> _Completed:
        calls.append({"command": command, "env": kwargs["env"]})
        return _Completed(stdout=_child_payload())

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert (
        automation.main(
            [
                *_base_args(tmp_path),
                "weekly-opportunity",
                "--database-url",
                _db_url(tmp_path / "weekly.db"),
            ]
        )
        == 0
    )

    command = calls[0]["command"]
    env = calls[0]["env"]
    output = json.loads(capsys.readouterr().out)

    assert "--save" in command
    assert "--dry-run" in command
    assert "--live-search" not in command
    assert "--live-sdk" not in command
    assert "--live-slack" not in command
    assert env["KEYSTONE_DRY_RUN"] == "true"
    assert env["KEYSTONE_ENABLE_LIVE_RESEARCH"] == "false"
    assert output["automation"]["gmail_writes_enabled"] is False
    assert output["automation"]["external_writes_enabled"] is False
    assert output["automation"]["agent_run_id"] == 42


def test_weekly_live_research_requires_operator_confirmation(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--confirm-live"):
        automation.main(
            [
                *_base_args(tmp_path),
                "weekly-opportunity",
                "--stage",
                "live-research",
                "--database-url",
                _db_url(tmp_path / "weekly.db"),
            ]
        )


def test_weekly_blocks_pending_approval_backlog_before_child_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = _db_url(tmp_path / "weekly.db")
    SQLiteStore(db_url).save_approval_item(
        ApprovalQueueItem(
            object_type="opportunity",
            object_id="opp-1",
            title="Pending opportunity",
            summary="Needs review",
            source_agent="test",
        )
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("child command must not run with pending approvals")
        ),
    )

    with pytest.raises(SystemExit, match="Pending approval backlog"):
        automation.main(
            [
                *_base_args(tmp_path),
                "weekly-opportunity",
                "--database-url",
                db_url,
            ]
        )


def test_gmail_label_preview_is_live_read_with_preview_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> _Completed:
        calls.append({"command": command, "env": kwargs["env"]})
        return _Completed(stdout=json.dumps({"mode": "live-gmail", "messages": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert (
        automation.main(
            [
                *_base_args(tmp_path),
                "gmail-triage",
                "--stage",
                "label-preview",
                "--confirm-live",
                "--label-filter",
                "Keystone/Triage",
                "--gmail-query",
                "newer_than:1d",
            ]
        )
        == 0
    )

    command = calls[0]["command"]
    env = calls[0]["env"]
    output = json.loads(capsys.readouterr().out)

    assert "--live-gmail" in command
    assert "--no-dry-run" in command
    assert "--preview-labels" in command
    assert "--apply-labels" not in command
    assert "--create-draft" not in command
    assert "--allow-inbox" not in command
    assert env["KEYSTONE_ENABLE_LIVE_GMAIL"] == "true"
    assert output["automation"]["gmail_writes_enabled"] is False


def test_gmail_label_apply_requires_repeated_reviewed_previews(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="reviewed successful previews"):
        automation.main(
            [
                *_base_args(tmp_path),
                "gmail-triage",
                "--stage",
                "label-apply",
                "--confirm-live",
                "--apply-labels-approved",
                "--successful-preview-count",
                "1",
                "--label-filter",
                "Keystone/Triage",
            ]
        )


def test_gmail_draft_create_requires_target_query(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="target --gmail-query"):
        automation.main(
            [
                *_base_args(tmp_path),
                "gmail-triage",
                "--stage",
                "draft-create",
                "--confirm-live",
                "--draft-create-approved",
                "--label-filter",
                "Keystone/Triage",
            ]
        )


def test_existing_lock_blocks_automation_before_child_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_file = tmp_path / "automation.lock"
    lock_file.write_text("pid=123\n", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("child command must not run while locked")
        ),
    )

    with pytest.raises(SystemExit, match="Automation lock already exists"):
        automation.main(
            [
                "--no-health-preflight",
                "--lock-file",
                str(lock_file),
                "weekly-opportunity",
                "--database-url",
                _db_url(tmp_path / "weekly.db"),
            ]
        )


def test_child_failure_is_redacted_and_returns_child_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(
            stderr="failed with token=sk-" + ("x" * 24),
            returncode=7,
        ),
    )

    code = automation.main(
        [
            *_base_args(tmp_path),
            "weekly-opportunity",
            "--database-url",
            _db_url(tmp_path / "weekly.db"),
        ]
    )

    captured = capsys.readouterr()

    assert code == 7
    assert "sk-" + ("x" * 24) not in captured.err
    assert "[REDACTED]" in captured.err
