"""Approval-gated Google Doc and Slack delivery for a synthesized weekly packet."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from keystone_agents.tools.internal_data_tools import (
    google_doc_read_impl,
    google_doc_write_impl,
    google_drive_search_files_impl,
)
from keystone_agents.tools.slack_tool import SlackTool

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
PACKET_FOLDER = "KNIOps"
PACKET_CHANNEL_NAME = "ops-finance"


def deliver_weekly_ops_packet(
    synthesis_result: dict[str, Any],
    *,
    live_workspace_write: bool,
    workspace_approval_reference: str = "",
    live_slack_post: bool,
    slack_channel_id: str = "",
    slack_approval_reference: str = "",
    prior_delivery_receipt: dict[str, Any] | None = None,
    search_files: Callable[..., dict[str, Any]] = google_drive_search_files_impl,
    write_doc: Callable[..., dict[str, Any]] = google_doc_write_impl,
    read_doc: Callable[..., dict[str, Any]] = google_doc_read_impl,
    post_slack: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write, verify, then optionally post one concise verified-link summary."""

    title, body = _packet_artifact(synthesis_result)
    resumed_doc = _verified_prior_doc(
        prior_delivery_receipt,
        expected_title=title,
        expected_body=body,
    )
    if live_slack_post and not live_workspace_write and resumed_doc is None:
        raise ValueError("Weekly packet Slack delivery requires a verified live Doc first.")
    if live_workspace_write and not workspace_approval_reference.strip():
        raise ValueError("Weekly packet Doc creation requires an approval reference.")
    if live_slack_post and (
        not slack_channel_id.strip() or not slack_approval_reference.strip()
    ):
        raise ValueError("Weekly packet Slack delivery requires channel ID and approval reference.")
    if not live_workspace_write and not live_slack_post:
        return {
            "status": "delivery_planned",
            "packet_title": title,
            "packet_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "folder_name": PACKET_FOLDER,
            "slack_channel_name": PACKET_CHANNEL_NAME,
            "provider_writes": 0,
            "send_enabled": False,
        }

    if live_workspace_write:
        folder_search = search_files(PACKET_FOLDER, folder_path="", max_items=10, live=True)
        folder_matches = [
            item
            for item in folder_search.get("items") or []
            if str(item.get("name") or "").strip().casefold() == PACKET_FOLDER.casefold()
            and str(item.get("mime_type") or item.get("mimeType") or "")
            == GOOGLE_FOLDER_MIME
        ]
        if len(folder_matches) != 1:
            raise RuntimeError("Weekly packet delivery requires exactly one KNIOps folder.")

        created = write_doc(
            title,
            body,
            folder_path=PACKET_FOLDER,
            approval_reference=workspace_approval_reference,
            live=True,
        )
        document_id = str(created.get("document_id") or "")
        document_url = str(created.get("url") or "")
        read_back = read_doc(
            document_id,
            folder_path=PACKET_FOLDER,
            max_chars=12_000,
            live=True,
        )
        doc_verified = bool(
            document_id
            and document_url
            and created.get("status") == "success"
            and read_back.get("status") == "success"
            and read_back.get("document_id") == document_id
            and read_back.get("title") == title
            and str(read_back.get("text") or "").strip() == body
            and read_back.get("truncated") is False
        )
        if not doc_verified:
            raise RuntimeError("Weekly packet Google Doc read-back verification failed.")

        doc_receipt = {
            "passed": True,
            "folder_match_count": 1,
            "document_id_hash": _hash(document_id),
            "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "char_count": len(body),
            "truncated": False,
            "approval_reference": workspace_approval_reference,
        }
    else:
        assert resumed_doc is not None
        document_url = resumed_doc["document_url"]
        doc_receipt = resumed_doc["doc_receipt"]
    if not live_slack_post:
        return {
            "status": "doc_verified_slack_pending",
            "document_url": document_url,
            "doc_receipt": doc_receipt,
            "slack_channel_name": PACKET_CHANNEL_NAME,
            "provider_writes": 1 if live_workspace_write else 0,
            "send_enabled": False,
        }

    message = _slack_delivery_message(title=title, body=body, document_url=document_url)
    post = post_slack or SlackTool(live=True).post_message
    posted = post(slack_channel_id.strip(), message)
    if posted.get("status") != "posted" or not str(posted.get("ts") or ""):
        return {
            "status": "doc_verified_slack_failed",
            "document_url": document_url,
            "doc_receipt": doc_receipt,
            "slack_receipt": {
                "passed": False,
                "channel_name": PACKET_CHANNEL_NAME,
                "channel_id_hash": _hash(slack_channel_id),
                "approval_reference": slack_approval_reference,
            },
            "provider_writes": 1 if live_workspace_write else 0,
            "send_enabled": False,
        }
    return {
        "status": "passed",
        "document_url": document_url,
        "doc_receipt": doc_receipt,
        "slack_receipt": {
            "passed": True,
            "channel_name": PACKET_CHANNEL_NAME,
            "channel_id_hash": _hash(slack_channel_id),
            "message_ts_hash": _hash(str(posted.get("ts") or "")),
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "approval_reference": slack_approval_reference,
        },
        "provider_writes": 2 if live_workspace_write else 1,
        "send_enabled": False,
    }


def _packet_artifact(synthesis_result: dict[str, Any]) -> tuple[str, str]:
    if synthesis_result.get("status") != "success":
        raise ValueError("Weekly packet delivery requires a successful synthesis receipt.")
    plan = synthesis_result.get("plan") or {}
    packet = synthesis_result.get("packet") or {}
    title = str(plan.get("packet_title") or "").strip()
    body = str(packet.get("synthesis") or packet.get("summary") or "").strip()
    if not title.startswith("KNI Weekly Operations Packet — ") or not body:
        raise ValueError("Weekly packet synthesis receipt lacks the titled artifact body.")
    if packet.get("send_enabled") is not False:
        raise ValueError("Weekly packet synthesis receipt violated the no-send boundary.")
    return title, body


def _verified_prior_doc(
    receipt: dict[str, Any] | None,
    *,
    expected_title: str,
    expected_body: str,
) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    if receipt.get("status") not in {
        "doc_verified_slack_pending",
        "doc_verified_slack_failed",
    }:
        return None
    document_url = str(receipt.get("document_url") or "").strip()
    doc_receipt = receipt.get("doc_receipt")
    expected_title_hash = hashlib.sha256(expected_title.encode()).hexdigest()
    expected_body_hash = hashlib.sha256(expected_body.encode()).hexdigest()
    if not (
        document_url.startswith("https://docs.google.com/document/d/")
        and isinstance(doc_receipt, dict)
        and doc_receipt.get("passed") is True
        and doc_receipt.get("title_sha256") == expected_title_hash
        and doc_receipt.get("body_sha256") == expected_body_hash
    ):
        return None
    return {"document_url": document_url, "doc_receipt": doc_receipt}


def _slack_delivery_message(*, title: str, body: str, document_url: str) -> str:
    summary_lines = [line.strip() for line in body.splitlines() if line.strip()]
    summary = " ".join(summary_lines[1:4])[:600] if len(summary_lines) > 1 else body[:600]
    return f"{title}\n{summary}\nVerified Google Doc: {document_url}"


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:12]
