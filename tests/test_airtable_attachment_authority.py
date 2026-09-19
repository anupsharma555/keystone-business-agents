from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from keystone_agents import cli
from keystone_agents.slack_actions import (
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    build_selected_message_context,
)

REQUEST = "add this attached receipt as exactly one personal expense in Airtable"


def _context(path: Path, *, bot: bool = False) -> dict:
    content = path.read_bytes()
    payload = build_selected_message_context(
        {
            "type": "message_action",
            "callback_id": RUN_AGENT_MESSAGE_CALLBACK_ID,
            "team": {"id": "T_SYNTHETIC"},
            "channel": {"id": "C_SYNTHETIC"},
            "message": {
                "ts": "1789060000.000001",
                "user": "U_SYNTHETIC",
                "text": "Receipt for review.",
                **({"bot_id": "B_SYNTHETIC"} if bot else {}),
                "files": [
                    {
                        "id": "F_SYNTHETIC",
                        "name": path.name,
                        "mimetype": "application/pdf",
                        "size": len(content),
                        "local_path": str(path),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            },
        }
    ).model_dump(mode="json", by_alias=True)
    attachment_context = cli._slack_attachment_manifest_context(payload)
    return {
        "schema": "keystone.direct_specialist_context.v1",
        "slack_scope": dict(attachment_context["scope"]),
        "slack_attachment_manifest": attachment_context,
    }


def test_verified_prior_human_file_is_resolved_without_transcript_path(tmp_path) -> None:
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4\nsynthetic receipt")
    context = _context(receipt)

    text = cli._direct_airtable_receipt_provider_context(
        "airtable_context_agent",
        REQUEST,
        execution_context=context,
    )
    bundle = cli._direct_context_attachment_bundle(REQUEST, context, include_prior=True)

    assert str(receipt) in text
    assert "airtable_target_table" in text
    assert [attachment.path for attachment in bundle.attachments] == [receipt.resolve()]
    assert bundle.attachments[0].checksum_sha256 == hashlib.sha256(receipt.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "context_key", ["thread_transcript_tail", "thread_root_request", "provider_context"]
)
def test_unverified_context_path_is_not_a_receipt_or_model_attachment(
    tmp_path, context_key
) -> None:
    receipt = tmp_path / "not-authorized.pdf"
    receipt.write_bytes(b"%PDF-1.4\nuntrusted context")
    context = {"schema": "keystone.direct_specialist_context.v1", context_key: str(receipt)}

    assert cli._finance_receipt_context_input(REQUEST, context) == REQUEST
    assert (
        cli._direct_airtable_receipt_provider_context(
            "airtable_context_agent",
            REQUEST,
            execution_context=context,
        )
        == ""
    )
    assert not cli._direct_context_attachment_bundle(
        REQUEST, context, include_prior=True
    ).has_inputs
    reason, _feedback = cli._direct_attachment_write_issue(
        "airtable_create_expense_from_receipt",
        {"local_file_path": str(receipt)},
        {},
    )
    assert reason == "attachment_authority_or_bytes_mismatch"


@pytest.mark.parametrize(
    "corruption", ["bot", "channel", "thread", "source", "checksum", "size", "mime", "ref"]
)
def test_prior_slack_file_requires_human_scope_and_unchanged_bytes(tmp_path, corruption) -> None:
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4\nsynthetic receipt")
    context = _context(receipt, bot=corruption == "bot")
    manifest = context["slack_attachment_manifest"]["payload_manifest"]
    entry = next(item for item in manifest["entries"] if item["kind"] == "attachment")
    if corruption == "channel":
        context["slack_scope"]["channel_id"] = "C_OTHER"
    elif corruption == "thread":
        context["slack_scope"]["thread_ts"] = "1789060000.000099"
    elif corruption == "source":
        entry["source_message_ts"] = "1789060000.000099"
    elif corruption == "checksum":
        receipt.write_bytes(b"%PDF-1.4\nchanged receipt")
    elif corruption == "size":
        entry["byte_size"] += 1
    elif corruption == "mime":
        entry["media_type"] = "image/png"
    elif corruption == "ref":
        entry["ref"] = "provider:unverified:attachment"

    assert not cli._verified_slack_attachment_bundle(context).has_inputs


def test_current_operator_path_takes_precedence_over_old_attachment(tmp_path) -> None:
    old = tmp_path / "old.pdf"
    old.write_bytes(b"%PDF-1.4\nold")
    selected = tmp_path / "selected.pdf"
    selected.write_bytes(b"%PDF-1.4\nselected")
    request = f"Add a business expense from this receipt in Airtable: {selected}"

    bundle = cli._direct_context_attachment_bundle(request, _context(old), include_prior=True)

    assert [attachment.path for attachment in bundle.attachments] == [selected.resolve()]
    allowed = {
        str(attachment.path): attachment.checksum_sha256 for attachment in bundle.attachments
    }
    assert cli._direct_attachment_write_issue(
        "airtable_create_expense_from_receipt",
        {"local_file_path": str(selected)},
        allowed,
    ) == ("", "")
    selected.write_bytes(b"%PDF-1.4\nchanged after model input")
    assert (
        cli._direct_attachment_write_issue(
            "airtable_create_expense_from_receipt",
            {"local_file_path": str(selected)},
            allowed,
        )[0]
        == "attachment_authority_or_bytes_mismatch"
    )


def test_multiple_prior_files_are_not_silently_resolved_to_one_receipt(tmp_path) -> None:
    receipt = tmp_path / "one.pdf"
    receipt.write_bytes(b"%PDF-1.4\none")
    context = _context(receipt)
    other = tmp_path / "two.pdf"
    other.write_bytes(b"%PDF-1.4\ntwo")
    entry = copy.deepcopy(
        next(
            item
            for item in context["slack_attachment_manifest"]["payload_manifest"]["entries"]
            if item["kind"] == "attachment"
        )
    )
    entry.update(
        {
            "file_id": "F_SECOND",
            "name": other.name,
            "materialized_path": str(other),
            "ref": entry["ref"].replace("F_SYNTHETIC", "F_SECOND"),
            "checksum_sha256": hashlib.sha256(other.read_bytes()).hexdigest(),
            "byte_size": other.stat().st_size,
        }
    )
    context["slack_attachment_manifest"]["payload_manifest"]["entries"].append(entry)

    assert len(cli._verified_slack_attachment_bundle(context).attachments) == 2
    assert cli._finance_receipt_context_input(REQUEST, context) == REQUEST


def test_history_manifest_survives_cli_projection_without_path_text(tmp_path) -> None:
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4\nsynthetic receipt")
    context = _context(receipt)
    captured = context["slack_attachment_manifest"]
    payload = {
        "schema": "keystone.slack.history_context.v1",
        **captured["scope"],
        "request_ts": "1789060000.000002",
        "payload_manifest": captured["payload_manifest"],
        "thread_messages": [
            {
                "ts": "1789060000.000001",
                "user": "U_SYNTHETIC",
                "role": "operator",
                "text": "Receipt for review.",
            }
        ],
    }
    path = tmp_path / "context.json"
    path.write_text(json.dumps(payload))

    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path),
        database_url=f"sqlite:///{tmp_path / 'business.db'}",
    )
    projected = cli._direct_specialist_execution_context(REQUEST, workflow_state=state)

    assert cli._verified_slack_attachment_bundle(projected).has_inputs
