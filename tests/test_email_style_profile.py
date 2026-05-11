from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from keystone_agents.agents.gmail_triage import run_gmail_triage_fixture
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.email_style_tool import (
    SentEmailStyleSample,
    build_email_style_profile_from_samples,
    load_email_style_profile_from_storage,
    load_sent_email_style_samples_fixture,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'style.db'}"


def test_sent_email_style_profile_builder_redacts_and_requires_approval() -> None:
    samples = load_sent_email_style_samples_fixture("sample_sent_email_style_messages")
    result = build_email_style_profile_from_samples(samples, profile_id="fixture-sent-style")
    encoded = json.dumps(result.model_dump(mode="json"), sort_keys=True)

    assert result.profile.profile_id == "fixture-sent-style"
    assert result.profile.approval_state == ApprovalState.PENDING
    assert result.sample_count == 3
    assert result.usable_sample_count == 3
    assert result.excluded_sample_count == 0
    assert result.approval_required is True
    assert result.approved_for_use is False
    assert result.raw_sent_email_bodies_included is False
    assert result.send_enabled is False
    assert result.sent is False
    assert result.profile.source_url.startswith("fixture://")
    assert result.sample_summaries[0].source_url.startswith("fixture://")
    assert result.sample_summaries[0].body_hash
    assert "taylor@example.com" not in encoded
    assert "[REDACTED_EMAIL]" in encoded
    assert "Thanks for reaching out about the clinical workflow question" not in encoded


def test_sent_email_style_profile_excludes_sensitive_samples_and_subjects() -> None:
    result = build_email_style_profile_from_samples(
        [
            SentEmailStyleSample(
                source_id="fixture:safe",
                source_url="fixture://style#safe",
                subject="Re: Research operations",
                body=(
                    "Hi Jordan,\n\nHappy to compare notes if useful. "
                    "Please send non-sensitive context.\n\nBest,\nAnup"
                ),
            ),
            SentEmailStyleSample(
                source_id="fixture:patient",
                source_url="fixture://style#patient",
                subject="Re: Patient Alice depression treatment",
                body=(
                    "Patient Alice was diagnosed with depression and the treatment plan "
                    "changed this week."
                ),
            ),
            SentEmailStyleSample(
                source_id="fixture:secret",
                source_url="fixture://style#secret",
                subject="Re: Token note",
                body="token=localplaceholder123 should never be stored.",
            ),
        ],
        profile_id="redacted-sent-style",
        notes="Patient Alice token=localplaceholder123",
    )
    encoded = json.dumps(result.model_dump(mode="json"), sort_keys=True)

    assert result.sample_count == 3
    assert result.usable_sample_count == 1
    assert result.excluded_sample_count == 2
    assert result.sample_summaries[1].used_for_profile is False
    assert "possible_phi" in result.sample_summaries[1].sensitive_flags
    assert result.sample_summaries[1].subject_summary == "[REDACTED_SUBJECT]"
    assert result.sample_summaries[2].used_for_profile is False
    assert "secret" in result.sample_summaries[2].sensitive_flags
    assert (
        result.profile.notes == "Profile notes redacted because they contained sensitive content."
    )
    assert "Patient Alice" not in encoded
    assert "depression treatment" not in encoded
    assert "localplaceholder123" not in encoded
    assert "token=" not in encoded


def test_pending_style_profile_is_not_loaded_until_approved(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    pending = build_email_style_profile_from_samples(
        load_sent_email_style_samples_fixture("sample_sent_email_style_messages"),
        profile_id="sent-default",
    ).profile
    approved = pending.model_copy(update={"approval_state": ApprovalState.APPROVED_FOR_DRAFTING})

    store.save_email_style_profile(pending)
    assert load_email_style_profile_from_storage("sent-default", database_url=database_url) is None

    store.save_email_style_profile(approved)
    loaded = load_email_style_profile_from_storage("sent-default", database_url=database_url)
    assert loaded is not None
    result = run_gmail_triage_fixture(
        FIXTURES / "sample_email_consulting.txt",
        sender_name="Alex",
        email_style_profile=loaded,
    )
    assert result.style_profile_used is True
    assert result.style_profile_id == "sent-default"


def test_build_email_style_profile_cli_fixture_save_has_no_raw_bodies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import scripts.build_email_style_profile as cli

    database_url = _database_url(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_email_style_profile.py",
            "--fixture",
            "sample_sent_email_style_messages",
            "--profile-id",
            "sent-default",
            "--save",
            "--database-url",
            database_url,
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    encoded = json.dumps(payload, sort_keys=True)
    store = SQLiteStore(database_url)

    assert payload["profile"]["approval_state"] == "pending"
    assert payload["sample_count"] == 3
    assert payload["usable_sample_count"] == 3
    assert payload["excluded_sample_count"] == 0
    assert payload["approval_required"] is True
    assert payload["storage"]["email_style_profile"]["table"] == "email_style_profiles"
    assert store.count("email_style_profiles") == 1
    assert store.count("agent_runs") == 1
    assert store.count("approvals") == 1
    assert "taylor@example.com" not in encoded
    assert "Thanks for reaching out about the clinical workflow question" not in encoded


def test_live_sent_style_profile_requires_explicit_live_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.build_email_style_profile as cli

    monkeypatch.setattr(
        cli,
        "GmailTool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GmailTool must not be constructed while dry-run is true")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["build_email_style_profile.py", "--live-gmail"])

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_live_sent_style_profile_samples_only_sent_mail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.build_email_style_profile as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(self, label: str | None, max_results: int) -> list[dict[str, str]]:
            assert label == "SENT"
            assert max_results == 2
            return [{"id": "sent-1"}, {"id": "sent-2"}]

        def get_message(self, message_id: str) -> dict[str, object]:
            body = (
                "Hi Jordan,\n\nHappy to compare notes if useful. "
                "Please send any non-sensitive context.\n\nBest,\nAnup"
            )
            return {
                "id": message_id,
                "envelope": {
                    "message_id": message_id,
                    "thread_id": f"thread-{message_id}",
                    "subject": "Re: Sent sample",
                    "sender_name": "Anup",
                    "sender_email": "anup@example.com",
                    "normalized_body": body,
                },
            }

        def apply_labels(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("style profiling must not modify labels")

        def create_draft_reply(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("style profiling must not create drafts")

        def send_email(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("style profiling must not send email")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_email_style_profile.py",
            "--live-gmail",
            "--no-dry-run",
            "--max-messages",
            "2",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "live-gmail-sent"
    assert payload["profile"]["source"] == "live_gmail_sent"
    assert payload["sample_count"] == 2
    assert payload["raw_sent_email_bodies_included"] is False
    assert payload["send_enabled"] is False
    assert payload["sent"] is False


def test_approved_stored_style_profile_integrates_with_gmail_and_outreach(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    approved = build_email_style_profile_from_samples(
        [
            SentEmailStyleSample(
                source_id="fixture:style",
                source_url="fixture://style",
                subject="Re: Fit",
                body=(
                    "Hi Jordan,\n\nHappy to compare notes if useful. "
                    "Please send any non-sensitive context.\n\nBest,\nAnup"
                ),
            )
        ],
        profile_id="sent-approved",
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    ).profile
    SQLiteStore(database_url).save_email_style_profile(approved)

    import scripts.run_gmail_triage as gmail_cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--fixture",
            str(FIXTURES / "sample_email_consulting.txt"),
            "--sender-name",
            "Alex",
            "--email-style-profile-id",
            "sent-approved",
            "--database-url",
            database_url,
            "--json",
        ],
    )

    assert gmail_cli.main() == 0
    gmail_payload = json.loads(capsys.readouterr().out)

    assert gmail_payload["style_profile_used"] is True
    assert gmail_payload["style_profile_id"] == "sent-approved"
    assert gmail_payload["approval_required"] is True

    import scripts.run_outreach_draft as outreach_cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--fixture",
            "sample_company_curebase",
            "--opportunity-fixture",
            "sample_lead_curebase",
            "--email-style-profile-id",
            "sent-approved",
            "--database-url",
            database_url,
            "--json",
        ],
    )

    assert outreach_cli.main() == 0
    outreach_payload = json.loads(capsys.readouterr().out)

    assert outreach_payload["draft"]["style_profile_used"] is True
    assert outreach_payload["draft"]["style_profile_id"] == "sent-approved"
    assert outreach_payload["send_enabled"] is False
    assert outreach_payload["draft"]["sent"] is False
