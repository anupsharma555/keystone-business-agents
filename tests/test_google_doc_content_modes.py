from __future__ import annotations

import pytest

from keystone_agents.tools.internal_data_tools import (
    _append_doc_requests,
    google_doc_write_impl,
)


class _Execute:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, object]:
        return self.payload


class _Documents:
    def __init__(self, document: dict[str, object]) -> None:
        self.document = document

    def documents(self) -> _Documents:
        return self

    def get(self, *, documentId: str) -> _Execute:  # noqa: N803 - provider field name
        assert documentId == "doc-1"
        return _Execute(self.document)


def test_google_doc_append_inserts_before_provider_terminal_newline() -> None:
    docs = _Documents(
        {
            "body": {
                "content": [
                    {
                        "endIndex": 15,
                        "paragraph": {
                            "elements": [{"textRun": {"content": "Existing note\n"}}]
                        },
                    }
                ]
            }
        }
    )

    requests = _append_doc_requests(docs, "doc-1", "Added reference")

    assert requests == [
        {
            "insertText": {
                "location": {"index": 14},
                "text": "\nAdded reference",
            }
        }
    ]


def test_google_doc_write_dry_run_reports_append_without_mutating() -> None:
    result = google_doc_write_impl(
        "Existing notes",
        "Added reference",
        document_id="doc-1",
        content_mode="append",
    )

    assert result["status"] == "dry-run"
    assert result["document_id"] == "doc-1"
    assert result["content_mode"] == "append"


def test_google_doc_write_rejects_unknown_content_mode() -> None:
    with pytest.raises(ValueError, match="content_mode must be replace or append"):
        google_doc_write_impl(
            "Existing notes",
            "Added reference",
            document_id="doc-1",
            content_mode="merge",  # type: ignore[arg-type]
        )
