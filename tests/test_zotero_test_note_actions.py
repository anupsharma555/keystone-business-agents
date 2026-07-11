from __future__ import annotations

from typing import Any
from urllib.error import HTTPError

import pytest

from keystone_agents.tools import zotero_context_tools
from keystone_agents.tools.zotero_context_tools import (
    zotero_delete_test_note_impl,
    zotero_write_test_note_impl,
)


def _item(key: str, version: int, note: str) -> dict[str, Any]:
    return {
        "key": key,
        "version": version,
        "data": {
            "key": key,
            "version": version,
            "itemType": "note",
            "note": note,
            "tags": [{"tag": "KBA_TEST_NOTE"}],
        },
    }


def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        zotero_context_tools,
        "_zotero_live_write_config",
        lambda **_: ("test-api-key", "12345"),
    )


def test_zotero_test_note_dry_run_requires_marker() -> None:
    with pytest.raises(ValueError, match="KBA_TEST_NOTE"):
        zotero_write_test_note_impl("<p>ordinary note</p>")

    result = zotero_write_test_note_impl("<p>KBA_TEST_NOTE preview</p>")

    assert result["status"] == "dry-run"
    assert result["operation"] == "create"
    assert result["verification"] == {"status": "preview", "passed": False}


def test_zotero_test_note_create_reads_back_without_returning_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **_: object) -> tuple[int, object, dict[str, str]]:
        calls.append((method, path))
        if method == "POST":
            return 200, {"successful": {"0": {"key": "NOTE1234"}}}, {}
        return 200, _item("NOTE1234", 1, "<p>KBA_TEST_NOTE created</p>"), {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", fake_request)

    result = zotero_write_test_note_impl(
        "<p>KBA_TEST_NOTE created</p>",
        approval_reference="zotero-create-approved",
        live=True,
    )

    assert result["status"] == "success"
    assert result["verification"]["passed"] is True
    assert result["item_key"] == "NOTE1234"
    assert "note" not in result["after"]
    assert result["after"]["note_sha256"]
    assert [method for method, _path in calls] == ["POST", "GET"]


def test_zotero_test_note_update_uses_version_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_request(method: str, _path: str, **kwargs: Any) -> tuple[int, object, dict[str, str]]:
        calls.append((method, kwargs))
        if method == "GET" and len(calls) == 1:
            return 200, _item("NOTE1234", 4, "<p>KBA_TEST_NOTE original</p>"), {}
        if method == "PATCH":
            return 204, {}, {}
        return 200, _item("NOTE1234", 5, "<p>KBA_TEST_NOTE updated</p>"), {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", fake_request)

    result = zotero_write_test_note_impl(
        "<p>KBA_TEST_NOTE updated</p>",
        item_key="NOTE1234",
        operation="update",
        approval_reference="zotero-update-approved",
        live=True,
    )

    assert result["status"] == "success"
    assert result["before"]["note_sha256"] != result["after"]["note_sha256"]
    assert calls[1][0] == "PATCH"
    assert calls[1][1]["headers"] == {"If-Unmodified-Since-Version": "4"}


def test_zotero_delete_test_note_blocks_unmarked_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    calls: list[str] = []

    def fake_request(method: str, _path: str, **_: object) -> tuple[int, object, dict[str, str]]:
        calls.append(method)
        item = _item("NOTE1234", 2, "<p>ordinary note</p>")
        item["data"]["tags"] = []
        return 200, item, {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", fake_request)

    with pytest.raises(RuntimeError, match="KBA_TEST_NOTE"):
        zotero_delete_test_note_impl(
            "NOTE1234",
            approval_reference="zotero-delete-approved",
            live=True,
        )
    assert calls == ["GET"]


def test_zotero_delete_test_note_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_request(method: str, _path: str, **kwargs: Any) -> tuple[int, object, dict[str, str]]:
        calls.append((method, kwargs))
        if method == "GET" and len(calls) == 1:
            return 200, _item("NOTE1234", 6, "<p>KBA_TEST_NOTE cleanup</p>"), {}
        if method == "DELETE":
            return 204, {}, {}
        return 404, {}, {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", fake_request)

    result = zotero_delete_test_note_impl(
        "NOTE1234",
        approval_reference="zotero-delete-approved",
        live=True,
    )

    assert result["status"] == "success"
    assert result["verification"] == {
        "status": "verified",
        "passed": True,
        "item_absent_after": True,
    }
    assert [method for method, _kwargs in calls] == ["GET", "DELETE", "GET"]
    assert calls[1][1]["headers"] == {"If-Unmodified-Since-Version": "6"}


def test_zotero_http_request_accepts_non_json_expected_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = HTTPError(
        "https://api.zotero.org/users/123/items/NOTE1234",
        404,
        "Not Found",
        {},
        None,
    )
    error.read = lambda: b"Not found"  # type: ignore[method-assign]

    def raise_not_found(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(zotero_context_tools, "urlopen", raise_not_found)

    status, payload, _headers = zotero_context_tools._zotero_http_request(
        "GET",
        "/users/123/items/NOTE1234",
        api_key="test-key",
        expected_statuses=(200, 404),
    )

    assert status == 404
    assert payload == {"non_json_response": "Not found"}
