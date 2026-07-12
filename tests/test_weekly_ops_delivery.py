from __future__ import annotations

import json

import pytest

from keystone_agents.weekly_ops_delivery import deliver_weekly_ops_packet
from scripts.deliver_weekly_chief_packet import main as delivery_cli_main


def _synthesis() -> dict[str, object]:
    return {
        "status": "success",
        "context_mode": "privacy_minimized_assertions",
        "proof_scope": "sanitized_context_proof",
        "plan": {"packet_title": "KNI Weekly Operations Packet — 2026-07-10"},
        "packet": {
            "summary": (
                "Executive focus areas\nOne focus.\nWorkstreams and decisions\nOne decision.\n"
                "Completed runs and outcomes\nOne result.\nCarry forward\nOne item.\n"
                "One-time Calendar focus\nOne event.\nRecurring Calendar cadence\nOne cadence.\n"
                "Next actions\nOne action.\nSource basis\nFour bounded sources.\n"
                "Operational health\nNo exception.\nPacket metadata\nOne request."
            ),
            "send_enabled": False,
        },
    }


def _folder_list(*_args, **_kwargs):
    return {
        "status": "success",
        "folder_id": "folder-1",
        "folder_path": "KNIOps",
        "items": [],
    }


def _write_doc(title, body, **_kwargs):
    return {
        "status": "success",
        "document_id": "doc-1",
        "title": title,
        "url": "https://docs.google.com/document/d/doc-1/edit",
        "body": body,
    }


def _read_doc(document_id, **_kwargs):
    packet = _synthesis()
    return {
        "status": "success",
        "document_id": document_id,
        "title": packet["plan"]["packet_title"],
        "text": packet["packet"]["summary"],
        "truncated": False,
    }


def test_delivery_plan_has_no_provider_writes() -> None:
    result = deliver_weekly_ops_packet(
        _synthesis(),
        live_workspace_write=False,
        live_slack_post=False,
    )

    assert result["status"] == "delivery_planned"
    assert result["provider_writes"] == 0
    assert "One focus" not in str(result)


def test_delivery_rejects_privacy_minimized_diagnostic_receipt() -> None:
    diagnostic = _synthesis()
    diagnostic["context_mode"] = "privacy_minimized_concept_signals"
    diagnostic["proof_scope"] = "sanitized_context_proof"

    with pytest.raises(ValueError, match="operationally specific"):
        deliver_weekly_ops_packet(
            diagnostic,
            live_workspace_write=False,
            live_slack_post=False,
        )


def test_delivery_accepts_assertion_backed_sanitized_receipt() -> None:
    assertion_backed = _synthesis()
    assertion_backed["context_mode"] = "privacy_minimized_assertions"
    assertion_backed["proof_scope"] = "sanitized_context_proof"

    result = deliver_weekly_ops_packet(
        assertion_backed,
        live_workspace_write=False,
        live_slack_post=False,
    )

    assert result["status"] == "delivery_planned"


def test_delivery_cli_reports_stale_receipt_as_structured_blocker(
    tmp_path, monkeypatch, capsys
) -> None:
    stale = _synthesis()
    stale.pop("context_mode")
    input_path = tmp_path / "stale.json"
    output_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "deliver_weekly_chief_packet.py",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ],
    )

    assert delivery_cli_main() == 1
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "blocked"
    assert "privacy-minimized assertion contract" in result["blocker"]
    assert result["provider_writes"] == 0
    assert json.loads(capsys.readouterr().out) == result


def test_doc_delivery_requires_exact_folder_and_readback() -> None:
    result = deliver_weekly_ops_packet(
        _synthesis(),
        live_workspace_write=True,
        workspace_approval_reference="approval:doc",
        live_slack_post=False,
        list_folder=_folder_list,
        write_doc=_write_doc,
        read_doc=_read_doc,
    )

    assert result["status"] == "doc_verified_slack_pending"
    assert result["doc_receipt"]["passed"] is True
    assert result["doc_receipt"]["folder_match_count"] == 1
    assert result["provider_writes"] == 1


def test_slack_delivery_occurs_only_after_verified_doc() -> None:
    captured = {}

    def fake_post(channel, message):
        captured["channel"] = channel
        captured["message"] = message
        return {"status": "posted", "channel": channel, "ts": "123.456"}

    result = deliver_weekly_ops_packet(
        _synthesis(),
        live_workspace_write=True,
        workspace_approval_reference="approval:doc",
        live_slack_post=True,
        slack_channel_id="COPS",
        slack_approval_reference="approval:slack",
        list_folder=_folder_list,
        write_doc=_write_doc,
        read_doc=_read_doc,
        post_slack=fake_post,
    )

    assert result["status"] == "passed"
    assert result["slack_receipt"]["passed"] is True
    assert result["slack_receipt"]["channel_name"] == "ops-finance"
    assert "Verified Google Doc:" in captured["message"]
    assert result["provider_writes"] == 2


def test_slack_delivery_requires_scoped_approvals_and_live_doc() -> None:
    with pytest.raises(ValueError, match="verified live Doc"):
        deliver_weekly_ops_packet(
            _synthesis(),
            live_workspace_write=False,
            live_slack_post=True,
            slack_channel_id="COPS",
            slack_approval_reference="approval:slack",
        )


def test_slack_retry_reuses_verified_doc_without_second_workspace_write() -> None:
    prior = deliver_weekly_ops_packet(
        _synthesis(),
        live_workspace_write=True,
        workspace_approval_reference="approval:doc",
        live_slack_post=False,
        list_folder=_folder_list,
        write_doc=_write_doc,
        read_doc=_read_doc,
    )
    writes = []

    result = deliver_weekly_ops_packet(
        _synthesis(),
        live_workspace_write=False,
        live_slack_post=True,
        slack_channel_id="COPS",
        slack_approval_reference="approval:slack-retry",
        prior_delivery_receipt=prior,
        write_doc=lambda *_args, **_kwargs: writes.append("unexpected") or {},
        post_slack=lambda channel, _message: {
            "status": "posted",
            "channel": channel,
            "ts": "456.789",
        },
    )

    assert result["status"] == "passed"
    assert result["provider_writes"] == 1
    assert writes == []
    assert result["doc_receipt"] == prior["doc_receipt"]
    with pytest.raises(ValueError, match="approval reference"):
        deliver_weekly_ops_packet(
            _synthesis(),
            live_workspace_write=True,
            live_slack_post=False,
        )


def test_slack_retry_rejects_doc_receipt_for_different_packet() -> None:
    prior = deliver_weekly_ops_packet(
        _synthesis(),
        live_workspace_write=True,
        workspace_approval_reference="approval:doc",
        live_slack_post=False,
        list_folder=_folder_list,
        write_doc=_write_doc,
        read_doc=_read_doc,
    )
    different = _synthesis()
    different["packet"]["summary"] += "\nChanged after the Doc was verified."

    with pytest.raises(ValueError, match="verified live Doc"):
        deliver_weekly_ops_packet(
            different,
            live_workspace_write=False,
            live_slack_post=True,
            slack_channel_id="COPS",
            slack_approval_reference="approval:slack-retry",
            prior_delivery_receipt=prior,
        )


def test_delivery_blocks_ambiguous_knio_ps_folder() -> None:
    with pytest.raises(RuntimeError, match="exactly one KNIOps"):
        deliver_weekly_ops_packet(
            _synthesis(),
            live_workspace_write=True,
            workspace_approval_reference="approval:doc",
            live_slack_post=False,
            list_folder=lambda *_args, **_kwargs: {"status": "missing"},
            write_doc=_write_doc,
            read_doc=_read_doc,
        )


def test_delivery_blocks_mismatched_doc_readback() -> None:
    with pytest.raises(RuntimeError, match="read-back verification"):
        deliver_weekly_ops_packet(
            _synthesis(),
            live_workspace_write=True,
            workspace_approval_reference="approval:doc",
            live_slack_post=False,
            list_folder=_folder_list,
            write_doc=_write_doc,
            read_doc=lambda *_args, **_kwargs: {
                "status": "success",
                "document_id": "doc-1",
                "title": "Wrong",
                "text": "Wrong",
                "truncated": False,
            },
        )
