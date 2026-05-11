from __future__ import annotations

import base64
import json
import sys
from typing import Any

import pytest

from keystone_agents.outreach_examples import (
    outreach_example_document_from_gmail_thread,
    sanitize_outreach_example_text,
)
from keystone_agents.schemas.outreach_examples import OutreachExampleDocument
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.gmail_tool import GmailTool


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("utf-8").rstrip("=")


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'outreach_examples.db'}"


def _thread() -> dict[str, Any]:
    return {
        "id": "gmail-thread-private-1",
        "messages": [
            {
                "id": "msg-1",
                "labelIds": ["SENT"],
                "from": "anup@keystone.example",
                "subject": "Clinical AI evaluation discussion",
                "body": (
                    "From: private.raw@example.com\n"
                    "Hello Alex, I noticed the clinical trial platform work. "
                    "Happy to compare notes if useful. Call me at 555-123-4567. "
                    "token=localplaceholder123"
                ),
            },
            {
                "id": "msg-2",
                "from": "alex.private@example.com",
                "subject": "Re: Clinical AI evaluation discussion",
                "body": (
                    "Thanks, this could be useful. Patient Jane diagnosis details "
                    "and MRN 123456 should not be stored. Let's discuss next week."
                ),
            },
        ],
    }


def _document(*, approved: bool = True) -> OutreachExampleDocument:
    return outreach_example_document_from_gmail_thread(
        _thread(),
        source_label="Keystone/Library/Success",
        company_type="clinical trial technology",
        opportunity_type="clinical AI evaluation",
        approved_for_drafting=approved,
    )


def test_sanitizer_removes_sensitive_private_content() -> None:
    sanitized, flags = sanitize_outreach_example_text(
        "Email alex.private@example.com, phone 555-123-4567, "
        "Patient Jane diagnosis details, MRN 123456, token=secret-value."
    )

    assert "alex.private@example.com" not in sanitized
    assert "555-123-4567" not in sanitized
    assert "Patient Jane" not in sanitized
    assert "123456" not in sanitized
    assert "secret-value" not in sanitized
    assert {"email", "phone", "possible_phi", "secret"} <= set(flags)


class FakeThreadResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeThreadSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeThreadResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if method != "GET":
            raise AssertionError(f"thread capture must be read-only, got {method} {url}")
        if url.endswith("/threads"):
            return FakeThreadResponse({"threads": [{"id": "thread-1"}]})
        if url.endswith("/threads/thread-1"):
            return FakeThreadResponse(
                {
                    "id": "thread-1",
                    "messages": [
                        {
                            "id": "msg-1",
                            "threadId": "thread-1",
                            "internalDate": "1713744000000",
                            "labelIds": ["SENT"],
                            "snippet": "clinical AI evaluation",
                            "payload": {
                                "headers": [
                                    {
                                        "name": "From",
                                        "value": "Anup <anup@keystone.example>",
                                    },
                                    {
                                        "name": "To",
                                        "value": "Jordan <jordan@example.com>",
                                    },
                                    {"name": "Subject", "value": "Clinical AI evaluation"},
                                ],
                                "mimeType": "text/plain",
                                "body": {
                                    "data": _b64(
                                        "Happy to compare notes if useful. "
                                        "token=localplaceholder123"
                                    )
                                },
                            },
                        },
                        {
                            "id": "msg-2",
                            "threadId": "thread-1",
                            "internalDate": "1713830400000",
                            "labelIds": ["INBOX"],
                            "snippet": "interested",
                            "payload": {
                                "headers": [
                                    {
                                        "name": "From",
                                        "value": "Jordan <jordan@example.com>",
                                    },
                                    {
                                        "name": "To",
                                        "value": "Anup <anup@keystone.example>",
                                    },
                                    {
                                        "name": "Subject",
                                        "value": "Re: Clinical AI evaluation",
                                    },
                                ],
                                "mimeType": "text/plain",
                                "body": {
                                    "data": _b64(
                                        "Thanks, this is useful. Patient Alice was "
                                        "diagnosed with depression. Call me at "
                                        "555-123-4567."
                                    )
                                },
                            },
                        },
                    ],
                }
            )
        raise AssertionError(f"Unexpected Gmail API call: {method} {url}")


def test_gmail_tool_thread_capture_uses_read_only_endpoints_and_metadata() -> None:
    session = FakeThreadSession()
    gmail = GmailTool(live=True, access_token="local-test-token", session=session)

    refs = gmail.list_threads_by_label_filter(
        label_filter="Keystone/Library/Success",
        max_results=1,
    )
    thread = gmail.get_thread("thread-1")

    assert refs == [{"id": "thread-1", "threadId": "thread-1"}]
    assert thread["message_count"] == 2
    assert thread["send_enabled"] is False
    assert thread["draft_created"] is False
    assert thread["labels_modified"] is False
    assert all(call["method"] == "GET" for call in session.calls)
    assert any(str(call["url"]).endswith("/threads") for call in session.calls)
    assert any(str(call["url"]).endswith("/threads/thread-1") for call in session.calls)
    assert not any("/modify" in str(call["url"]) for call in session.calls)
    assert not any("/drafts" in str(call["url"]) for call in session.calls)
    assert not any("/send" in str(call["url"]) for call in session.calls)
    first_message = thread["messages"][0]
    assert first_message["subject"] == "Clinical AI evaluation"
    assert first_message["received_at"] == "2024-04-22T00:00:00Z"
    assert "Happy to compare notes" in first_message["normalized_body"]
    assert first_message["labelIds"] == ["SENT"]


def test_raw_body_is_not_stored_in_sqlite_retrievable_documents(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    document = _document()

    store.save_outreach_example_document(document)
    encoded_rows = json.dumps(store.fetch_all("outreach_examples"), sort_keys=True)
    result = store.retrieve_outreach_examples(
        "clinical AI evaluation compare notes",
        company_type="clinical trial technology",
        opportunity_type="clinical AI evaluation",
    )
    encoded_result = json.dumps(result.model_dump(mode="json"), sort_keys=True)

    assert result.examples
    assert result.examples[0].raw_body_included is False
    assert "alex.private@example.com" not in encoded_rows
    assert "555-123-4567" not in encoded_rows
    assert "Patient Jane" not in encoded_rows
    assert "MRN 123456" not in encoded_rows
    assert "private.raw@example.com" not in encoded_rows
    assert "token=localplaceholder123" not in encoded_rows
    assert "Hello Alex" not in encoded_rows
    assert "Happy to compare notes if useful" not in encoded_rows
    assert "Let's discuss next week" not in encoded_rows
    assert "alex.private@example.com" not in encoded_result
    assert result.raw_body_included is False
    assert result.external_vector_db_used is False


def test_examples_require_approval_before_retrieval(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    pending = _document(approved=False)
    approved = _document(approved=True).model_copy(update={"example_id": "approved-example"})

    store.save_outreach_example_document(pending)
    store.save_outreach_example_document(approved)

    result = store.retrieve_outreach_examples("clinical AI evaluation compare notes")

    assert [example.example_id for example in result.examples] == ["approved-example"]
    with pytest.raises(ValueError, match="approved_only=True"):
        store.retrieve_outreach_examples("clinical AI", approved_only=False)


def test_capture_requires_live_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.capture_gmail_thread_examples as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_gmail_thread_examples.py",
            "--label-filter",
            "Keystone/Library/Success",
        ],
    )

    with pytest.raises(SystemExit, match="--live-gmail"):
        cli.main()


def test_capture_requires_no_dry_run_before_constructing_gmail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.capture_gmail_thread_examples as cli

    def fail_if_constructed(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("GmailTool must not be constructed without --no-dry-run")

    monkeypatch.setattr(cli, "GmailTool", fail_if_constructed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_gmail_thread_examples.py",
            "--live-gmail",
            "--label-filter",
            "Keystone/Library/Success",
        ],
    )

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_capture_is_read_only_and_never_calls_gmail_mutations(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.capture_gmail_thread_examples as cli

    class FakeGmail:
        def __init__(self, live: bool) -> None:
            self.live = live

        def list_threads_by_label_filter(
            self,
            *,
            label_filter: str,
            max_results: int,
        ) -> list[dict[str, str]]:
            assert self.live is True
            assert label_filter == "Keystone/Library/Success"
            assert max_results == 3
            return [{"threadId": "thread-1"}]

        def get_thread(self, thread_id: str) -> dict[str, Any]:
            assert thread_id == "thread-1"
            return _thread()

        def apply_labels(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("capture must not mutate labels")

        def create_draft_reply(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("capture must not create drafts")

        def send_email(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("capture must not send email")

    database_url = _database_url(tmp_path)
    monkeypatch.setattr(cli, "GmailTool", FakeGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_gmail_thread_examples.py",
            "--live-gmail",
            "--no-dry-run",
            "--label-filter",
            "Keystone/Library/Success",
            "--max-threads",
            "3",
            "--approved-for-drafting",
            "--database-url",
            database_url,
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    store = SQLiteStore(database_url)
    result = store.retrieve_outreach_examples("clinical AI compare notes")

    assert payload["captured_count"] == 1
    assert payload["read_only"] is True
    assert payload["gmail_mutations_enabled"] is False
    assert result.examples
    assert result.examples[0].approved_for_drafting is True
    encoded_rows = json.dumps(store.fetch_all("outreach_examples"), sort_keys=True)
    assert "alex.private@example.com" not in encoded_rows
    assert "private.raw@example.com" not in encoded_rows
    assert "555-123-4567" not in encoded_rows
    assert "Patient Jane" not in encoded_rows
    assert "token=localplaceholder123" not in encoded_rows
    assert "Hello Alex" not in encoded_rows
    assert "Happy to compare notes if useful" not in encoded_rows
    assert "Let's discuss next week" not in encoded_rows
