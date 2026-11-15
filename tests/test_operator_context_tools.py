"""Native SDK calls to host-bound local operator context tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from agents.tool_context import ToolContext

from keystone_agents.runtime.scoped_memory import memory_capture_fingerprint
from keystone_agents.schemas.memory_scope import MemoryCaptureProvenance, SlackMemoryScope
from keystone_agents.tools.operator_context_tools import OperatorContextCapability

RAW_REQUEST = "Save this synthetic preference for later review."


def _capability(
    tmp_path, *, scope: SlackMemoryScope | None = None,
    capture_authorized: bool = False, source_refs: dict[str, str] | None = None,
    max_bytes: int = 4096, raw_request: str = RAW_REQUEST,
    initial_facts: list[dict] | None = None,
    include_approved_profile: bool = False,
) -> OperatorContextCapability:
    scope = scope or SlackMemoryScope(
        team_id="T111TEST", channel_id="C111TEST", user_id="U111TEST",
        project_key="synthetic",
    )
    thread_ts = "1794675600.000001"
    request_ts = "1794675600.000002"
    provenance = MemoryCaptureProvenance(
        thread_ts=thread_ts, request_ts=request_ts,
        request_fingerprint=memory_capture_fingerprint(
            scope, thread_ts=thread_ts, request_ts=request_ts, raw_request=raw_request,
        ),
    )
    return OperatorContextCapability(
        scope=scope, provenance=provenance,
        database_url=f"sqlite:///{tmp_path / 'operator-context.db'}",
        raw_request=raw_request, source_refs=source_refs,
        capture_authorized=capture_authorized, max_bytes=max_bytes,
        initial_facts=initial_facts,
        include_approved_profile=include_approved_profile,
    )


def _invoke(tool, arguments: dict[str, object]) -> dict[str, object]:
    native_tool = getattr(tool, "sdk_tool", tool)
    serialized = json.dumps(arguments)
    context = ToolContext(
        context=None, tool_name=native_tool.name, tool_call_id="synthetic-call",
        tool_arguments=serialized,
    )
    return json.loads(asyncio.run(native_tool.on_invoke_tool(context, serialized)))


def test_profile_query_is_bounded_and_unrelated_query_is_empty(tmp_path) -> None:
    capability = _capability(tmp_path, include_approved_profile=True)
    assert [tool.name for tool in capability.tools] == ["query_operator_context"]
    query = capability.tools[0]
    answer = _invoke(query, {
        "query": "What are my approved professional background and skills?",
        "include_profile": True,
    })
    assert answer["status"] == "ok"
    assert 1 <= len(answer["facts"]) <= 4
    assert len(json.dumps(answer, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) <= 4096
    assert all(fact["memory_type"] == "approved_operator_profile" for fact in answer["facts"])
    assert "operator@" not in json.dumps(answer)
    assert capability.evidence["profile_source_sha256"]
    assert capability.evidence["serialized_bytes"] <= 4096
    assert capability.descriptor["tool_names"] == ["query_operator_context"]
    assert "raw_request" not in capability.descriptor

    unrelated = _invoke(query, {"query": "What is tomorrow's weather?", "include_profile": True})
    assert unrelated["status"] == "empty"
    assert unrelated["facts"] == []


def test_model_cannot_enable_host_disabled_approved_profile(tmp_path) -> None:
    capability = _capability(tmp_path)
    response = _invoke(capability.tools[0], {
        "query": "approved professional background and skills",
        "include_profile": True,
    })
    assert response["facts"] == []
    approved = _capability(tmp_path, include_approved_profile=True)
    fact = _invoke(approved.tools[0], {
        "query": "approved professional background", "include_profile": True,
    })["facts"][0]
    with pytest.raises(ValueError, match="not enabled"):
        _capability(tmp_path, initial_facts=[fact])


def test_capture_is_host_gated_idempotent_and_scope_bound(tmp_path) -> None:
    capability = _capability(tmp_path, capture_authorized=True)
    assert [tool.name for tool in capability.tools] == [
        "query_operator_context", "capture_operator_memory",
    ]
    capture = capability.tools[1]
    arguments = {
        "kind": "preference", "title": "Synthetic review preference",
        "summary": "Prefer a bounded review with a saved artifact.",
        "source_ref_ids": [],
    }
    first = _invoke(capture, arguments)
    second = _invoke(capture, arguments)
    assert first == second
    assert first["status"] == "saved"
    assert isinstance(first["id"], int)
    assert first["verified"] is True
    assert capability.evidence["capture_receipts"] == [first]
    assert capability.evidence["capture_count"] == 1
    assert capability.usage["capture_saved_count"] == 1

    different = _invoke(capture, {
        **arguments, "title": "A different synthetic preference",
    })
    assert different == {"status": "blocked", "reason": "capture_limit_reached"}
    assert capability.usage["capture_limit_rejections"] == 1

    found = _invoke(capability.tools[0], {
        "query": "bounded review preference", "include_profile": False,
    })
    assert found["status"] == "ok"
    assert found["facts"][0]["memory_id"] == first["id"]
    assert capability.evidence["model_context"]["facts"][0]["memory_id"] == first["id"]

    other_scope = SlackMemoryScope(
        team_id="T111TEST", channel_id="C222TEST", user_id="U111TEST",
        project_key="synthetic",
    )
    other = _capability(tmp_path, scope=other_scope)
    not_found = _invoke(other.tools[0], {
        "query": "bounded review preference", "include_profile": False,
    })
    assert not_found["facts"] == []


def test_reference_requires_exact_host_allowlisted_source(tmp_path) -> None:
    capability = _capability(
        tmp_path, capture_authorized=True,
        source_refs={"source-1": "https://example.org/synthetic-source"},
    )
    capture = capability.tools[1]
    base = {
        "kind": "reference", "title": "Synthetic public reference",
        "summary": "Remember the selected public source.",
    }
    blocked = _invoke(capture, {**base, "source_ref_ids": ["unknown-source"]})
    assert blocked == {"status": "blocked", "reason": "capture_scope_or_storage_unavailable"}
    saved = _invoke(capture, {**base, "source_ref_ids": ["source-1"]})
    assert saved["status"] == "saved"
    found = _invoke(capability.tools[0], {
        "query": "synthetic public reference", "include_profile": False,
    })
    assert found["facts"][0]["source_urls"] == ["https://example.org/synthetic-source"]


def test_capture_descriptor_exposes_only_two_whole_public_references(tmp_path) -> None:
    exact_url = "https://example.org/article_(review)"
    other_url = "https://example.org/second-source"
    source_refs = {
        "request-url:1": exact_url,
        "request-url:2": "http://127.0.0.1/private",
        "request-url:3": "https://example.org/" + "x" * 600,
        "request-url:4": other_url,
    }
    query_only = _capability(tmp_path, source_refs=source_refs)
    assert "allowed_capture_sources" not in query_only.descriptor
    assert "request-url:1" not in json.dumps(query_only.descriptor)

    capture_cap = _capability(
        tmp_path, capture_authorized=True, source_refs=source_refs,
    )
    allowed = capture_cap.descriptor["allowed_capture_sources"]
    assert allowed == [
        {"id": "request-url:1", "url": exact_url},
        {"id": "request-url:4", "url": other_url},
    ]
    assert len(json.dumps(allowed, ensure_ascii=True, sort_keys=True).encode()) <= 512
    assert "request-url:2" not in json.dumps(capture_cap.descriptor)
    assert "request-url:3" not in json.dumps(capture_cap.descriptor)
    assert "database_url" not in capture_cap.descriptor
    saved = _invoke(capture_cap.tools[1], {
        "kind": "reference", "title": "Synthetic public reference",
        "summary": "Remember the exact public reference.",
        "source_ref_ids": ["request-url:1"],
    })
    assert saved["status"] == "saved"
    found = _invoke(capture_cap.tools[0], {
        "query": "exact public reference", "include_profile": False,
    })
    assert found["facts"][0]["source_urls"] == [exact_url]


def test_small_budget_omits_whole_fact_and_profile_source_must_match(tmp_path, monkeypatch) -> None:
    capability = _capability(tmp_path, max_bytes=128, include_approved_profile=True)
    response = _invoke(capability.tools[0], {
        "query": "approved professional background", "include_profile": True,
    })
    assert response["facts"] == []
    assert response["omitted_count"] >= 1
    assert len(json.dumps(response, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) <= 128

    from keystone_agents.tools import operator_context_tools

    unsupported = tmp_path / "unapproved-profile.md"
    unsupported.write_text("Unrelated content")
    monkeypatch.setattr(operator_context_tools, "_PROFILE_PATH", unsupported)
    empty = _invoke(_capability(tmp_path, include_approved_profile=True).tools[0], {
        "query": "approved professional background", "include_profile": True,
    })
    assert empty["facts"] == []


def test_provenance_mismatch_and_insufficient_budget_do_not_offer_tools(tmp_path) -> None:
    capability = _capability(tmp_path, max_bytes=0)
    assert capability.tools == []
    assert capability.evidence["omission_reason"] == "context_budget_unavailable"
    with pytest.raises(ValueError, match="provenance"):
        OperatorContextCapability(
            scope=capability.scope, provenance=capability.provenance,
            database_url=f"sqlite:///{tmp_path / 'operator-context.db'}",
            raw_request="Different current request.",
        )


def test_query_budget_allows_two_valid_queries_and_no_third_read(tmp_path) -> None:
    capability = _capability(tmp_path, include_approved_profile=True)
    tool = capability.tools[0]
    assert _invoke(tool, {"query": "approved professional background"})["status"] == "ok"
    assert _invoke(tool, {"query": "approved decision priorities"})["status"] == "ok"
    third = _invoke(tool, {"query": "approved research skills"})
    assert third == {"status": "query_limit_reached", "facts": []}
    assert capability.usage["query_calls"] == 2
    assert capability.usage["query_limit_rejections"] == 1
    assert len(capability.evidence["model_context"]["facts"]) <= 4


def test_two_queries_share_exact_forwardable_fact_ledger(tmp_path) -> None:
    full = _capability(tmp_path, include_approved_profile=True)
    first = _invoke(full.tools[0], {
        "query": "approved professional background and skills",
    })
    assert len(first["facts"]) == 2
    first_ids = [fact["memory_id"] for fact in first["facts"]]
    second = _invoke(full.tools[0], {"query": "approved decision priorities"})
    assert second["status"] == "ok"
    assert len(second["facts"]) == 1
    assert second["remaining_fact_slots"] == 1
    assert [fact["memory_id"] for fact in full.evidence["model_context"]["facts"]] == [
        *first_ids, second["facts"][0]["memory_id"],
    ]

    repeat = _capability(tmp_path, include_approved_profile=True)
    seen = _invoke(repeat.tools[0], {"query": "approved professional background and skills"})
    seen_again = _invoke(repeat.tools[0], {"query": "approved professional background"})
    assert seen_again["status"] == "already_admitted"
    assert seen_again["facts"] == []
    assert seen_again["already_admitted_ids"]
    assert all(
        admitted in [fact["memory_id"] for fact in seen["facts"]]
        for admitted in seen_again["already_admitted_ids"]
    )
    assert all(
        fact in repeat.evidence["model_context"]["facts"]
        for fact in seen["facts"]
    )


def test_four_facts_can_be_forwarded_across_two_queries_without_exceeding_budget(
    tmp_path,
) -> None:
    capability = _capability(
        tmp_path, capture_authorized=True, include_approved_profile=True,
    )
    saved = _invoke(capability.tools[1], {
        "kind": "preference", "title": "Synthetic bounded preference",
        "summary": "Prefer a bounded review with an inspectable result.",
        "source_ref_ids": [],
    })
    assert saved["status"] == "saved"
    first = _invoke(capability.tools[0], {
        "query": "approved professional background and skills",
    })
    second = _invoke(capability.tools[0], {
        "query": "synthetic bounded preference and decision priorities",
    })
    assert len(first["facts"]) == len(second["facts"]) == 2
    assert second["remaining_fact_slots"] == 0
    forwarded = capability.evidence["model_context"]["facts"]
    assert len(forwarded) == 4
    assert {fact["memory_id"] for fact in forwarded} == {
        *(fact["memory_id"] for fact in first["facts"]),
        *(fact["memory_id"] for fact in second["facts"]),
    }
    assert saved["id"] in {fact["memory_id"] for fact in forwarded}
    assert capability.evidence["serialized_bytes"] <= 4096
    assert all(
        len(json.dumps(response, ensure_ascii=True, sort_keys=True).encode()) <= 4096
        for response in (first, second)
    )
    assert capability.usage["query_calls"] == 2
    assert capability.usage["capture_saved_count"] == 1
    resumed = _capability(
        tmp_path, initial_facts=forwarded, include_approved_profile=True,
    )
    assert resumed.evidence["model_context"] == {"facts": forwarded}
    assert resumed.evidence["remaining_fact_slots"] == 0
    with pytest.raises(ValueError, match="at most four"):
        _capability(
            tmp_path, initial_facts=[*forwarded, forwarded[0]],
            include_approved_profile=True,
        )


def test_second_query_can_admit_remaining_profile_facts(tmp_path) -> None:
    capability = _capability(tmp_path, include_approved_profile=True)
    first = _invoke(capability.tools[0], {"query": "approved professional background"})
    assert len(first["facts"]) == 1
    second = _invoke(capability.tools[0], {
        "query": "approved skills and decision priorities",
    })
    assert len(second["facts"]) == 2
    assert second["remaining_fact_slots"] == 1
    forwarded = capability.evidence["model_context"]["facts"]
    assert len(forwarded) == 3
    assert first["facts"][0] in forwarded
    assert all(fact in forwarded for fact in second["facts"])


def test_profile_query_reports_partial_when_scoped_memory_is_unavailable(
    tmp_path, monkeypatch,
) -> None:
    from keystone_agents.tools import operator_context_tools

    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable store")

    monkeypatch.setattr(operator_context_tools, "query_scoped_memory_facts", unavailable)
    capability = _capability(tmp_path, include_approved_profile=True)
    answer = _invoke(capability.tools[0], {
        "query": "approved professional background", "include_profile": True,
    })
    assert answer["status"] == "partial"
    assert len(answer["facts"]) == 1
    assert capability.evidence["query_error_type"] == "OSError"
    assert "synthetic unavailable store" not in json.dumps(answer)


def test_seeded_chief_facts_share_query_ledger_and_admit_remaining_fact(tmp_path) -> None:
    preflight = _capability(tmp_path, include_approved_profile=True)
    original = _invoke(preflight.tools[0], {
        "query": "approved professional background and skills",
    })
    seed = original["facts"]
    assert len(seed) == 2
    chief = _capability(tmp_path, initial_facts=seed, include_approved_profile=True)
    assert chief.evidence["model_context"] == {"facts": seed}
    assert chief.descriptor["seed_fact_count"] == 2
    next_query = _invoke(chief.tools[0], {"query": "approved decision priorities"})
    assert next_query["status"] == "ok"
    assert len(next_query["facts"]) == 1
    assert chief.evidence["model_context"] == {"facts": [*seed, *next_query["facts"]]}
    assert chief.evidence["remaining_fact_slots"] == 1
    repeated = _capability(tmp_path, initial_facts=seed, include_approved_profile=True)
    repeated_query = _invoke(repeated.tools[0], {"query": "approved professional background"})
    assert repeated_query["status"] == "already_admitted"
    assert repeated_query["facts"] == []
    assert repeated_query["already_admitted_ids"] == [seed[0]["memory_id"]]

    one_seed = _capability(tmp_path, initial_facts=seed[:1], include_approved_profile=True)
    followup = _invoke(one_seed.tools[0], {"query": "approved decision priorities"})
    assert len(followup["facts"]) == 1
    assert followup["facts"][0] in one_seed.evidence["model_context"]["facts"]
    assert len(one_seed.evidence["model_context"]["facts"]) == 2


def test_scoped_seed_requires_current_exact_store_record_and_scope(tmp_path) -> None:
    original = _capability(tmp_path, capture_authorized=True)
    _invoke(original.tools[1], {
        "kind": "preference", "title": "Synthetic current preference",
        "summary": "Prefer a bounded review with a visible result.",
        "source_ref_ids": [],
    })
    fact = _invoke(original.tools[0], {
        "query": "synthetic current preference", "include_profile": False,
    })["facts"][0]
    valid = _capability(tmp_path, initial_facts=[fact])
    assert valid.evidence["model_context"] == {"facts": [fact]}
    assert _invoke(valid.tools[0], {
        "query": "synthetic current preference", "include_profile": False,
    })["already_admitted_ids"] == [fact["memory_id"]]

    foreign_scope = SlackMemoryScope(
        team_id="T111TEST", channel_id="G222OTHER", user_id="U111TEST",
        project_key="synthetic",
    )
    for candidate, scope in (
        (fact, foreign_scope),
        ({**fact, "summary": "Altered memory text."}, None),
        ({**fact, "memory_id": fact["memory_id"] + 1000}, None),
    ):
        resumed = _capability(tmp_path, scope=scope, initial_facts=[candidate])
        assert resumed.evidence["model_context"] is None
        assert resumed.descriptor["seed_fact_count"] == 0

    with original._storage.store.managed_connection() as connection:
        connection.execute(
            "UPDATE memory_items SET approval_state = 'pending' WHERE id = ?",
            (fact["memory_id"],),
        )
    revoked = _capability(tmp_path, initial_facts=[fact])
    assert revoked.evidence["model_context"] is None
    assert revoked.tools

    with original._storage.store.managed_connection() as connection:
        row = connection.execute(
            "SELECT memory_json FROM memory_items WHERE id = ?", (fact["memory_id"],),
        ).fetchone()
        payload = json.loads(row["memory_json"])
        payload["expires_at"] = "2020-01-01T00:00:00Z"
        connection.execute(
            "UPDATE memory_items SET approval_state = 'approved_for_research', "
            "expires_at = '2020-01-01T00:00:00Z', memory_json = ? WHERE id = ?",
            (json.dumps(payload), fact["memory_id"]),
        )
    assert _capability(tmp_path, initial_facts=[fact]).evidence["model_context"] is None

    with original._storage.store.managed_connection() as connection:
        payload["expires_at"] = None
        connection.execute(
            "UPDATE memory_items SET expires_at = NULL, memory_json = ? WHERE id = ?",
            (json.dumps(payload), fact["memory_id"]),
        )
    from keystone_agents.memory import chief_of_staff_memory_item

    replacement = chief_of_staff_memory_item(
        memory_type="operator_strategy", title="Replacement preference",
        summary="Use the replacement only.", source_ids=["synthetic:replacement"],
    ).model_copy(update={"supersedes_memory_id": fact["memory_id"]})
    original._storage.store.save_memory_item(replacement)
    assert _capability(tmp_path, initial_facts=[fact]).evidence["model_context"] is None


def test_initial_facts_reject_extra_prose_bad_refs_and_oversized_payload(tmp_path) -> None:
    preflight = _capability(tmp_path, include_approved_profile=True)
    fact = _invoke(preflight.tools[0], {"query": "approved professional background"})["facts"][0]
    with pytest.raises(ValueError, match="approved context"):
        _capability(tmp_path, initial_facts=[{**fact, "old_answer": "unaccepted"}],
                    include_approved_profile=True)
    with pytest.raises(ValueError, match="byte budget"):
        _capability(tmp_path, initial_facts=[fact], max_bytes=128,
                    include_approved_profile=True)

    captured = _capability(tmp_path, capture_authorized=True)
    _invoke(captured.tools[1], {
        "kind": "preference", "title": "Synthetic preference",
        "summary": "Prefer a bounded review.", "source_ref_ids": [],
    })
    scoped = _invoke(captured.tools[0], {
        "query": "bounded review preference", "include_profile": False,
    })["facts"][0]
    with pytest.raises(ValueError, match="invalid shape"):
        _capability(tmp_path, initial_facts=[{**scoped, "old_answer": "unaccepted"}])
    with pytest.raises(ValueError, match="references"):
        _capability(tmp_path, initial_facts=[{
            **scoped, "source_urls": ["http://127.0.0.1/private"],
        }])


def test_seed_byte_cap_uses_actual_sdk_json_for_non_ascii_text(tmp_path) -> None:
    seed = {
        "memory_id": 1, "memory_type": "operator_strategy",
        "title": "Synthetic preference", "summary": "é" * 100,
        "source_ids": ["synthetic-source"], "source_urls": [],
        "created_at": "2026-01-01T00:00:00Z",
    }
    compact = len(json.dumps({"facts": [seed]}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode())
    sdk = len(json.dumps({"facts": [seed]}, ensure_ascii=True, sort_keys=True).encode())
    assert compact < 500 < sdk
    with pytest.raises(ValueError, match="byte budget"):
        _capability(tmp_path, initial_facts=[seed], max_bytes=500)
