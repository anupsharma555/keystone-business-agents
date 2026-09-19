from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from keystone_agents.models import GmailPriorityGroupingSDKInput, GmailTriageSDKInput
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.tools import gmail_query_tools
from keystone_agents.tools.gmail_tool import (
    GmailTool,
    gmail_message_envelope_from_api,
    gmail_message_envelope_from_dict,
)


def _part(mime_type: str, text: str) -> dict:
    return {
        "mimeType": mime_type,
        "body": {
            "data": base64.urlsafe_b64encode(text.encode()).decode(),
        },
    }


def _raw_message(
    payload: dict,
    *,
    message_id: str = "msg-source",
    thread_id: str = "thread-source",
    sender: str = "Publisher <publisher@example.test>",
    received_at_ms: str = "1788278400000",
) -> dict:
    payload = dict(payload)
    payload["headers"] = [
        *(payload.get("headers") or []),
        {"name": "Subject", "value": "Synthetic source evidence"},
        {"name": "From", "value": sender},
        {"name": "To", "value": "Operator <operator@example.test>"},
    ]
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": received_at_ms,
        "snippet": "Synthetic source evidence",
        "labelIds": ["INBOX"],
        "payload": payload,
    }


def _tool_message(payload: dict, **kwargs: str) -> dict:
    raw = _raw_message(payload, **kwargs)
    with patch.object(GmailTool, "_request", return_value=raw):
        return GmailTool(
            live=True,
            access_token="synthetic-unused-token",
        ).get_message(raw["id"])


@pytest.mark.parametrize(
    ("payload", "expected_role"),
    [
        (
            _part(
                "text/html",
                "<p>Quarterly pilot update.</p><blockquote>"
                "The pilot is NOT approved; budget is $0.</blockquote>",
            ),
            "editorial_source",
        ),
        (
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    _part("text/plain", "View this message in an HTML-capable reader."),
                    _part(
                        "text/html",
                        "<h2>Pilot details</h2><p>The pilot is NOT approved; "
                        "budget is $0.</p>",
                    ),
                ],
            },
            "selected_triage_view",
        ),
    ],
)
def test_actual_get_message_preserves_both_original_missing_fact_cases(
    payload: dict,
    expected_role: str,
) -> None:
    result = _tool_message(payload)
    serialized = json.dumps(result)

    assert "NOT approved" in result["body"]
    assert "$0" in result["normalized_body"]
    assert "NOT approved" in result["envelope"]["body_evidence"][0]["source_text"] or (
        "NOT approved" in result["envelope"]["body_evidence"][1]["source_text"]
    )
    assert "$0" in serialized
    assert result["id"] == "msg-source"
    assert result["threadId"] == "thread-source"
    assert any(
        expected_role == quote.get("quote_kind") or expected_role == item.get("role")
        for item in result["body_evidence"]
        for quote in item.get("quotations") or [{}]
    )


def test_equivalent_alternatives_remain_complete_without_concatenation() -> None:
    result = _tool_message(
        {
            "mimeType": "multipart/alternative",
            "parts": [
                _part("text/plain", "Status is conditional; budget is $0."),
                _part("text/html", "<p>Status is conditional; budget is $0.</p>"),
            ],
        }
    )

    assert result["body"] == "Status is conditional; budget is $0."
    assert result["body_content_status"] == "complete"
    assert result["body_content_complete"] is True
    assert [item["role"] for item in result["body_evidence"]] == [
        "selected_triage_view",
        "alternate_representation",
    ]


def test_conflicting_alternatives_are_retained_without_reconciliation() -> None:
    result = _tool_message(
        {
            "mimeType": "multipart/alternative",
            "parts": [
                _part("text/plain", "Pilot approved only after review; budget is $0."),
                _part("text/html", "<p>Pilot approved; budget is $10.</p>"),
            ],
        }
    )

    assert result["body"] == "Pilot approved only after review; budget is $0."
    assert result["body_content_status"] == "conflicting"
    assert result["body_content_complete"] is False
    assert "$0" in result["body_evidence"][0]["source_text"]
    assert "$10" in result["body_evidence"][1]["source_text"]
    assert any("without reconciliation" in item for item in result["triage_limitations"])


def test_nested_mime_html_preserves_entities_links_and_table_relationships() -> None:
    result = _tool_message(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        _part("text/plain", "Read the detailed version online."),
                        _part(
                            "text/html",
                            "<p>R&amp;D details at <a href='https://example.test/proof'>"
                            "source</a>.</p><table><tr><th>Status</th><th>Budget</th>"
                            "</tr><tr><td>NOT approved</td><td>$0</td></tr>"
                            "<tr><td>Only if reviewed</td><td>-$5</td></tr>"
                            "<tr><td>Blank value</td><td></td></tr></table>",
                        ),
                    ],
                }
            ],
        }
    )
    source_text = result["body_evidence"][1]["source_text"]

    assert "R&D details" in source_text
    assert "[[link: https://example.test/proof]]" in source_text
    assert "[[table 1 row 2]]" in source_text
    assert "[[table 1 cell row=2 cell_index=1" in source_text
    assert "NOT approved" in source_text
    assert "[[table 1 cell row=2 cell_index=2" in source_text
    assert "$0" in source_text
    assert "Only if reviewed" in source_text
    assert "-$5" in source_text
    assert "[[blank table cell]]" in source_text
    assert result["body_evidence"][1]["structure_annotations"] is True
    assert result["extracted_links"][0]["url"] == "https://example.test/proof"


def test_editorial_quote_and_reply_history_have_distinct_triage_behavior() -> None:
    editorial = _tool_message(
        _part("text/html", "<p>Update.</p><blockquote>Editorial source says $0.</blockquote>")
    )
    reply = _tool_message(
        _part(
            "text/html",
            "<p>Latest answer.</p><div class='gmail_quote'>On Monday, Alex wrote:"
            "<blockquote>Older message says $10.</blockquote></div>",
        )
    )

    assert "Editorial source says $0" in editorial["body"]
    assert editorial["body_evidence"][0]["quotations"][0]["quote_kind"] == (
        "editorial_source"
    )
    assert "Older message says $10" not in reply["body"]
    assert "Older message says $10" in reply["body_evidence"][0]["source_text"]
    assert any(
        quote["quote_kind"] == "reply_history"
        for quote in reply["body_evidence"][0]["quotations"]
    )
    assert any("stripped before triage" in item for item in reply["triage_limitations"])


def test_plain_reply_and_nested_malformed_html_remain_labeled_source_evidence() -> None:
    plain = _tool_message(
        _part(
            "text/plain",
            "Latest statement.\nOn Monday, Alex wrote:\n> Older statement was $10.",
        )
    )
    malformed = _tool_message(
        _part(
            "text/html",
            "<p>Current.</p><blockquote>Outer source <blockquote>Nested condition $0",
        )
    )

    assert plain["body"] == "Latest statement."
    assert "Older statement was $10" in plain["body_evidence"][0]["source_text"]
    assert plain["body_evidence"][0]["quotations"][0]["attribution_status"] == (
        "explicit"
    )
    assert "Nested condition $0" in malformed["body"]
    assert any(
        quote["quote_kind"] == "editorial_source"
        for quote in malformed["body_evidence"][0]["quotations"]
    )


def test_empty_image_only_and_large_content_report_explicit_limits() -> None:
    empty = _tool_message({"mimeType": "multipart/mixed", "parts": []})
    image = _tool_message(
        {
            "mimeType": "multipart/related",
            "parts": [{"mimeType": "image/png", "body": {"data": "aW1hZ2U="}}],
        }
    )
    newsletter_images = _tool_message(
        _part(
            "text/html",
            "<p>Newsletter: NOT approved.</p>" + "<img src='cid:image'>" * 12,
        )
    )
    large = _tool_message(_part("text/plain", "x" * 7000 + " END"))

    assert empty["body_content_status"] == "empty"
    assert empty["body_content_complete"] is False
    assert any("No readable text" in item for item in empty["triage_limitations"])
    assert image["body"] == ""
    assert image["body_content_status"] == "partial"
    assert image["body_content_complete"] is False
    assert "not read" in image["body_evidence"][0]["source_text"]
    assert "NOT approved" in newsletter_images["body"]
    assert newsletter_images["body_content_status"] == "partial"
    assert any(
        "12 inline visual" in item
        for item in newsletter_images["body_evidence"][0]["limitations"]
    )
    assert large["body_content_status"] == "partial"
    assert large["body_evidence"][0]["truncated"] is True
    assert len(large["body_evidence"][0]["source_text"]) <= 6000
    assert "[[truncated:" in large["body_evidence"][0]["source_text"]


@pytest.mark.parametrize("mime_type", ["text/plain", "text/html"])
def test_long_sanitized_message_windows_recover_late_and_boundary_conditions(
    monkeypatch: pytest.MonkeyPatch,
    mime_type: str,
) -> None:
    qualification = (
        "FINAL QUALIFICATION: the proposal is NOT approved unless independent review "
        "is complete."
    )
    source = "Background source detail. " * 1_000 + qualification
    body = source if mime_type == "text/plain" else f"<article><p>{source}</p></article>"
    raw = _raw_message(_part(mime_type, body))
    raw["historyId"] = "history-long-1"
    tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(tool, "current_account_email", lambda: "reader@example.test")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        tool.get_message_context_projection,
    )

    page = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-source",
        live=True,
    )
    chunks: list[str] = []
    starts: list[int] = []
    snapshots: set[str] = set()
    while True:
        assert page.status == "read"
        assert page.thread_id == "thread-source"
        assert page.provider_history_id == "history-long-1"
        snapshots.add(page.source_snapshot_sha256)
        evidence = page.body_evidence[0]
        assert evidence.coverage is not None
        chunks.append(evidence.source_text)
        starts.append(evidence.coverage.start_char)
        next_request = evidence.next_request
        if next_request is None:
            assert evidence.coverage.complete is True
            break
        page = gmail_query_tools.read_gmail_context_impl(
            **next_request.model_dump(),
            live=True,
        )

    reconstructed = "".join(chunks)
    assert qualification in reconstructed
    assert starts == list(range(0, len(reconstructed), 3_000))
    assert len(snapshots) == 1
    assert page.body_content_complete is True


def test_gmail_source_change_blocks_window_mixing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = [
        "Background. " * 700 + "LATE CONDITION: NOT approved.",
        "Changed background. " * 700 + "CHANGED CONDITION.",
    ]
    state = {"index": 0}

    def raw_message() -> dict:
        value = _raw_message(_part("text/plain", bodies[state["index"]]))
        value["historyId"] = f"history-{state['index']}"
        return value

    tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: raw_message())
    monkeypatch.setattr(tool, "current_account_email", lambda: "reader@example.test")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        tool.get_message_context_projection,
    )

    first = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-source",
        live=True,
    )
    next_request = first.body_evidence[0].next_request
    assert next_request is not None
    state["index"] = 1
    changed = gmail_query_tools.read_gmail_context_impl(
        **next_request.model_dump(),
        live=True,
    )

    assert changed.status == "source_changed"
    assert changed.source_restart_required is True
    assert changed.body_evidence == []
    assert "do not combine source versions" in " ".join(changed.triage_limitations)


def test_exact_cap_empty_and_attachment_backed_sources_do_not_invent_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(tool, "current_account_email", lambda: "reader@example.test")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        tool.get_message_context_projection,
    )

    exact = _raw_message(_part("text/plain", "x" * 3_000))
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: exact)
    exact_result = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-source",
        live=True,
    )
    exact_evidence = exact_result.body_evidence[0]
    assert exact_evidence.coverage is not None
    assert exact_evidence.coverage.full_char_count == 3_000
    assert exact_evidence.coverage.complete is True
    assert exact_evidence.next_request is None

    empty = _raw_message({"mimeType": "text/plain", "body": {"data": ""}})
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: empty)
    empty_result = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-source",
        live=True,
    )
    assert empty_result.body_evidence == []
    assert empty_result.body_content_status == "empty"

    attachment = _raw_message(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "attached.txt",
                    "body": {"attachmentId": "private-provider-id", "size": 20},
                }
            ],
        }
    )
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: attachment)
    initial = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-source",
        live=True,
    )
    inaccessible = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-source",
        body_part_path="0.1",
        max_body_chars=3_000,
        expected_thread_id="thread-source",
        expected_account_identity_sha256=initial.account_identity_sha256,
        expected_source_snapshot_sha256=initial.source_snapshot_sha256,
        live=True,
    )
    assert inaccessible.status == "source_inaccessible"
    assert inaccessible.body_evidence == []
    assert "attachment bodies" in " ".join(inaccessible.triage_limitations)


def test_complete_source_is_filtered_before_window_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "token=" + "s" * 32
    source = "x" * 2_994 + " " + secret + " safe tail"
    raw = _raw_message(_part("text/plain", source))
    tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(tool, "current_account_email", lambda: "reader@example.test")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        tool.get_message_context_projection,
    )

    first = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-source",
        live=True,
    )
    chunks = [first.body_evidence[0].source_text]
    next_request = first.body_evidence[0].next_request
    while next_request is not None:
        page = gmail_query_tools.read_gmail_context_impl(
            **next_request.model_dump(),
            live=True,
        )
        chunks.append(page.body_evidence[0].source_text)
        next_request = page.body_evidence[0].next_request
    reconstructed = "".join(chunks)

    assert secret not in reconstructed
    assert "[redacted secret-like value]" in reconstructed
    assert "safe tail" in reconstructed

    phi_raw = _raw_message(
        _part(
            "text/plain",
            "x" * 2_995
            + " Patient Jane Doe was diagnosed with depression and needs follow-up.",
        )
    )
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: phi_raw)
    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        gmail_query_tools.read_gmail_context_impl(
            resource_type="message",
            resource_id="msg-source",
            live=True,
        )


def test_multipart_mixed_sections_coexist_and_declared_charset_is_preserved() -> None:
    mixed = _tool_message(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                _part("text/plain", "Project Alpha is approved."),
                _part("text/html", "<p>Project Beta is NOT approved.</p>"),
            ],
        }
    )
    encoded = base64.urlsafe_b64encode("Budget: £0; unit: µg.".encode("cp1252")).decode()
    charset = _tool_message(
        {
            "mimeType": "text/plain",
            "body": {"data": encoded},
            "headers": [
                {
                    "name": "Content-Type",
                    "value": "text/plain; charset=windows-1252",
                }
            ],
        }
    )
    unsupported_charset = _tool_message(
        {
            "mimeType": "text/plain",
            "body": {
                "data": base64.urlsafe_b64encode(b"Budget is $0.").decode()
            },
            "headers": [
                {
                    "name": "Content-Type",
                    "value": "text/plain; charset=x-unknown-synthetic",
                }
            ],
        }
    )

    assert "Project Alpha is approved." in mixed["body"]
    assert "Project Beta is NOT approved." in mixed["body"]
    assert mixed["body_content_status"] == "complete"
    assert {item["role"] for item in mixed["body_evidence"]} == {
        "coexisting_section"
    }
    assert all(not item["alternative_group"] for item in mixed["body_evidence"])
    assert charset["body"] == "Budget: £0; unit: µg."
    assert charset["body_content_complete"] is True
    assert unsupported_charset["body"] == "Budget is $0."
    assert unsupported_charset["body_content_status"] == "partial"
    assert unsupported_charset["body_content_complete"] is False
    assert any(
        "unsupported" in item
        for item in unsupported_charset["body_evidence"][0]["limitations"]
    )


def test_message_and_thread_paths_preserve_source_identity_speaker_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_first = _raw_message(
        _part("text/html", "<p>First value is $0.</p>"),
        message_id="msg-first",
        sender="Alex <alex@example.test>",
        received_at_ms="1788278400000",
    )
    raw_second = _raw_message(
        _part("text/html", "<p>Second value is NOT approved.</p>"),
        message_id="msg-second",
        sender="Blair <blair@example.test>",
        received_at_ms="1788364800000",
    )
    tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: raw_first)
    monkeypatch.setattr(tool, "current_account_email", lambda: "reader@example.test")
    projection = tool.get_message_context_projection("msg-first")

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        lambda _identity: projection,
    )
    message_context = gmail_query_tools.read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-first",
        live=True,
    )

    thread_tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(
        thread_tool,
        "_request",
        lambda *_args, **_kwargs: {
            "id": "thread-source",
            "messages": [raw_first, raw_second],
        },
    )
    thread = thread_tool.get_thread("thread-source")
    rebuilt = gmail_message_envelope_from_dict(thread["messages"][0])
    thread["source_url"] = "https://mail.google.com/mail/#all/thread-source"
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        lambda _identity: thread,
    )
    thread_context = gmail_query_tools.read_gmail_context_impl(
        resource_type="thread",
        resource_id="thread-source",
        live=True,
    )

    assert message_context.resource_id == "msg-first"
    assert rebuilt.message_id == "msg-first"
    assert "$0" in rebuilt.body_evidence[0].source_text
    assert message_context.body_evidence[0].message_id == "msg-first"
    assert message_context.body_evidence[0].thread_id == "thread-source"
    assert message_context.body_evidence[0].sender_name == "Alex"
    assert "$0" in message_context.body_evidence[0].source_text
    assert {item.message_id for item in thread_context.body_evidence} == {
        "msg-first",
        "msg-second",
    }
    assert {item.sender_name for item in thread_context.body_evidence} == {"Alex", "Blair"}
    assert all(item.received_at for item in thread_context.body_evidence)
    assert "NOT approved" in thread_context.model_dump_json()


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_thread_chronology_is_invariant_to_provider_message_order(
    monkeypatch: pytest.MonkeyPatch,
    order: tuple[int, int, int],
) -> None:
    bodies = [
        "INITIAL: Review has not started.",
        "MIDDLE: We are checking the details.",
        "LATEST: Review is complete; approval is still conditional.",
    ]
    messages = [
        _raw_message(
            _part("text/plain", body),
            message_id=f"msg-{index}",
            received_at_ms=str(1_700_000_000_000 + index * 60_000),
        )
        for index, body in enumerate(bodies)
    ]
    tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(
        tool,
        "_request",
        lambda *_args, **_kwargs: {
            "id": "thread-source",
            "messages": [messages[index] for index in order],
        },
    )

    result = tool.get_thread("thread-source")

    assert [item["id"] for item in result["messages"]] == [
        "msg-0",
        "msg-1",
        "msg-2",
    ]
    assert result["latest_received_at"] == gmail_message_envelope_from_api(
        messages[2]
    ).received_at
    assert result["summary"].startswith(f"Latest status: {bodies[2]}")
    assert f"Initial context: {bodies[0]}" in result["summary"]


def test_thread_with_unusable_date_does_not_guess_latest_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _raw_message(
        _part("text/plain", "Known dated source."),
        message_id="msg-valid",
    )
    unknown = _raw_message(
        _part("text/plain", "Undated source must not be called latest."),
        message_id="msg-undated",
        received_at_ms="",
    )
    tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(
        tool,
        "_request",
        lambda *_args, **_kwargs: {
            "id": "thread-source",
            "messages": [unknown, valid],
        },
    )

    result = tool.get_thread("thread-source")

    assert result["latest_received_at"] == ""
    assert "Thread chronology unavailable" in result["summary"]
    assert "Latest status:" not in result["summary"]
    assert any(
        "chronology" in limitation and "unavailable" in limitation
        for limitation in result["triage_limitations"]
    )


def test_direct_gmail_sdk_prompt_receives_provenance_and_conflict_before_decision() -> None:
    envelope = gmail_message_envelope_from_api(
        _raw_message(
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    _part("text/plain", "Budget is $0 only if reviewed."),
                    _part("text/html", "<p>Budget is $10.</p>"),
                ],
            }
        )
    )

    prompt = GmailTriageSDKInput.from_envelope(envelope).to_prompt()
    grouping_prompt = GmailPriorityGroupingSDKInput.from_envelopes(
        [envelope],
        email_style_profile="Short professional paragraphs.",
    ).to_prompt()

    assert "Message ID: msg-source" in prompt
    assert "Thread ID: thread-source" in prompt
    assert "status=conflicting; content_complete=false" in prompt
    assert "Budget is $0 only if reviewed." in prompt
    assert "Budget is $10." in prompt
    assert "Do not silently concatenate conflicts" in prompt
    assert "Bounded source evidence: status=conflicting" in grouping_prompt
    assert "Budget is $0 only if reviewed." in grouping_prompt
    assert "Budget is $10." in grouping_prompt
