from __future__ import annotations

from typing import Any

import pytest

from keystone_agents.tools import zotero_context_tools
from keystone_agents.tools.zotero_context_tools import (
    zotero_delete_test_collection_impl,
    zotero_delete_test_item_impl,
    zotero_write_test_collection_impl,
    zotero_write_test_item_impl,
)


def _collection(key: str, version: int, name: str) -> dict[str, Any]:
    return {
        "key": key,
        "version": version,
        "data": {
            "key": key,
            "version": version,
            "name": name,
            "parentCollection": False,
        },
    }


def _item(
    key: str,
    version: int,
    title: str,
    collection_key: str,
    *,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "version": version,
        "data": {
            "key": key,
            "version": version,
            "itemType": "webpage",
            "title": title,
            "url": "https://example.test/kba-zotero-validation",
            "tags": [{"tag": tag} for tag in (tags or ["KBA_TEST_ITEM"])],
            "collections": [collection_key],
        },
    }


def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        zotero_context_tools,
        "_zotero_live_test_library_write_config",
        lambda **_: ("test-api-key", "12345"),
    )


def test_zotero_test_library_previews_require_markers_and_identity() -> None:
    with pytest.raises(ValueError, match="KBA_TEST_COLLECTION"):
        zotero_write_test_collection_impl("ordinary collection")
    with pytest.raises(ValueError, match="collection_key"):
        zotero_write_test_item_impl("KBA_TEST_ITEM preview", collection_key="")

    collection = zotero_write_test_collection_impl("KBA_TEST_COLLECTION preview")
    item = zotero_write_test_item_impl(
        "KBA_TEST_ITEM preview",
        collection_key="COLL1234",
        tags=["validation"],
    )

    assert collection["status"] == "dry-run"
    assert item["status"] == "dry-run"
    assert item["tags"] == ["validation", "KBA_TEST_ITEM"]
    assert collection["verification"]["passed"] is False
    assert item["verification"]["passed"] is False


def test_zotero_test_collection_create_and_update_use_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []
    state = {"name": "KBA_TEST_COLLECTION created", "version": 1}

    def fake_request(
        method: str, _path: str, **kwargs: Any
    ) -> tuple[int, object, dict[str, str]]:
        calls.append((method, kwargs))
        if method == "POST":
            return 200, {"successful": {"0": {"key": "COLL1234"}}}, {}
        if method == "PATCH":
            state["name"] = str(kwargs["payload"]["name"])
            state["version"] = 2
            return 204, {}, {}
        return 200, _collection("COLL1234", int(state["version"]), str(state["name"])), {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", fake_request)

    created = zotero_write_test_collection_impl(
        "KBA_TEST_COLLECTION created",
        approval_reference="approved:create",
        live=True,
    )
    updated = zotero_write_test_collection_impl(
        "KBA_TEST_COLLECTION updated",
        collection_key="COLL1234",
        approval_reference="approved:update",
        operation="update",
        live=True,
    )

    assert created["status"] == "success"
    assert updated["status"] == "success"
    assert updated["collection_key"] == "COLL1234"
    patch = next(kwargs for method, kwargs in calls if method == "PATCH")
    assert patch["headers"] == {"If-Unmodified-Since-Version": "1"}


def test_zotero_test_item_create_and_update_verify_tags_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []
    state = {
        "title": "KBA_TEST_ITEM created",
        "version": 3,
        "tags": ["KBA_TEST_ITEM", "created"],
    }

    def fake_request(
        method: str, path: str, **kwargs: Any
    ) -> tuple[int, object, dict[str, str]]:
        calls.append((method, kwargs))
        if "/collections/" in path:
            return 200, _collection("COLL1234", 2, "KBA_TEST_COLLECTION active"), {}
        if method == "POST":
            return 200, {"successful": {"0": {"key": "ITEM1234"}}}, {}
        if method == "PATCH":
            payload = kwargs["payload"]
            state["title"] = payload["title"]
            state["tags"] = [entry["tag"] for entry in payload["tags"]]
            state["version"] = 4
            return 204, {}, {}
        return 200, _item(
            "ITEM1234",
            int(state["version"]),
            str(state["title"]),
            "COLL1234",
            tags=list(state["tags"]),
        ), {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", fake_request)

    created = zotero_write_test_item_impl(
        "KBA_TEST_ITEM created",
        collection_key="COLL1234",
        tags=["created"],
        approval_reference="approved:create",
        live=True,
    )
    updated = zotero_write_test_item_impl(
        "KBA_TEST_ITEM updated",
        item_key="ITEM1234",
        collection_key="COLL1234",
        tags=["updated"],
        approval_reference="approved:update",
        operation="update",
        live=True,
    )

    assert created["verification"]["passed"] is True
    assert updated["verification"]["passed"] is True
    assert updated["verification"]["collection_membership_match"] is True
    assert set(updated["tags"]) == {"updated", "KBA_TEST_ITEM"}
    patch = next(kwargs for method, kwargs in calls if method == "PATCH")
    assert patch["headers"] == {"If-Unmodified-Since-Version": "3"}


def test_zotero_test_item_delete_requires_both_markers_and_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_request(
        method: str, _path: str, **kwargs: Any
    ) -> tuple[int, object, dict[str, str]]:
        calls.append((method, kwargs))
        if method == "GET" and len(calls) == 1:
            return 200, _item(
                "ITEM1234", 8, "KBA_TEST_ITEM cleanup", "COLL1234"
            ), {}
        if method == "DELETE":
            return 204, {}, {}
        return 404, {}, {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", fake_request)

    result = zotero_delete_test_item_impl(
        "ITEM1234",
        approval_reference="approved:delete",
        live=True,
    )

    assert result["status"] == "success"
    assert result["verification"]["item_absent_after"] is True
    assert calls[1][1]["headers"] == {"If-Unmodified-Since-Version": "8"}


def test_zotero_test_collection_delete_refuses_nonempty_and_then_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)

    def nonempty_request(
        method: str, path: str, **_kwargs: Any
    ) -> tuple[int, object, dict[str, str]]:
        if path.endswith("/items?limit=1"):
            return 200, [_item("ITEM1234", 1, "KBA_TEST_ITEM", "COLL1234")], {}
        return 200, _collection("COLL1234", 5, "KBA_TEST_COLLECTION cleanup"), {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", nonempty_request)
    with pytest.raises(RuntimeError, match="non-empty"):
        zotero_delete_test_collection_impl(
            "COLL1234", approval_reference="approved:delete", live=True
        )

    calls: list[tuple[str, dict[str, Any]]] = []

    def empty_request(
        method: str, path: str, **kwargs: Any
    ) -> tuple[int, object, dict[str, str]]:
        calls.append((method, kwargs))
        if path.endswith("/items?limit=1"):
            return 200, [], {}
        if method == "DELETE":
            return 204, {}, {}
        if method == "GET" and len(calls) == 4:
            return 404, {}, {}
        return 200, _collection("COLL1234", 5, "KBA_TEST_COLLECTION cleanup"), {}

    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", empty_request)
    result = zotero_delete_test_collection_impl(
        "COLL1234", approval_reference="approved:delete", live=True
    )

    assert result["status"] == "success"
    assert result["verification"]["collection_absent_after"] is True
    assert calls[2][1]["headers"] == {"If-Unmodified-Since-Version": "5"}
