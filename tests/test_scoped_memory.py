"""Scoped local-memory contracts use temporary stores and no provider calls."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from keystone_agents.memory import chief_of_staff_memory_item
from keystone_agents.runtime.scoped_memory import (
    ScopedMemoryCaptureInput,
    capture_scoped_memory,
    memory_capture_fingerprint,
    project_scoped_memory_facts,
    query_scoped_memory_facts,
)
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.memory_scope import (
    MEMORY_CAPTURE_KEY,
    MEMORY_SCOPE_KEY,
    MemoryCaptureProvenance,
    SlackMemoryScope,
)
from keystone_agents.tools.storage_tool import StorageTool


@pytest.fixture
def scope():
    return SlackMemoryScope(
        team_id="T111TEST", channel_id="C111TEST", user_id="U111TEST", project_key="synthetic",
    )


@pytest.fixture
def storage(tmp_path):
    return StorageTool(database_url=f"sqlite:///{tmp_path / 'scoped-memory.db'}", agent_name="test")


def provenance(scope, turn=1):
    request_ts = f"179467560{turn}.000001"
    return MemoryCaptureProvenance(
        thread_ts="1794675600.000001", request_ts=request_ts,
        request_fingerprint=memory_capture_fingerprint(
            scope, thread_ts="1794675600.000001", request_ts=request_ts,
            raw_request="Save this synthetic preference.",
        ),
    )


def item(**updates):
    value = chief_of_staff_memory_item(
        memory_type="operator_strategy", title="Synthetic pilot preference",
        summary="Prefer a bounded experiment with a reviewable artifact.",
        source_ids=["synthetic:operator"],
    )
    return MemoryItem.model_validate({**value.model_dump(mode="json"), **updates})


def test_scope_and_provenance_are_strict_immutable_host_bindings(scope):
    with pytest.raises(ValidationError):
        scope.user_id = "U222OTHER"
    with pytest.raises(ValidationError):
        SlackMemoryScope(**{**scope.model_dump(), "schema_version": "unknown"})
    with pytest.raises(ValidationError):
        MemoryCaptureProvenance(
            thread_ts="1794675602.000001", request_ts="1794675601.000001",
            request_fingerprint="a" * 64,
        )
    first = provenance(scope)
    assert first.request_fingerprint != provenance(scope, 2).request_fingerprint
    assert first.request_fingerprint != memory_capture_fingerprint(
        scope, thread_ts=first.thread_ts, request_ts=first.request_ts,
        raw_request="Save this synthetic preference. ",
    )


@pytest.mark.parametrize("key", [MEMORY_SCOPE_KEY, MEMORY_CAPTURE_KEY])
def test_untrusted_metadata_cannot_forge_scoped_memory(storage, scope, key):
    record = item(metadata={key: scope.model_dump(mode="json")})
    with pytest.raises(ValueError, match="trusted host scope"):
        storage.save_memory_item(record)
    assert not storage.store.list_memory_items()
    with storage.store.managed_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_events WHERE tool_name='storage_save_memory_item'"
        ).fetchone()[0] == 1


def test_trusted_scope_rejects_conflicting_metadata_and_arbitrary_replacement(storage, scope):
    capture = provenance(scope)
    with pytest.raises(ValueError, match="conflicts"):
        storage.save_memory_item(
            item(metadata={MEMORY_SCOPE_KEY: {}}), trusted_scope=scope,
            capture_provenance=capture,
        )
    for record in (item(id=3), item(supersedes_memory_id=3)):
        with pytest.raises(ValueError, match="creation only"):
            storage.save_memory_item(record, trusted_scope=scope, capture_provenance=capture)
    assert not storage.store.list_memory_items()


def test_scoped_save_is_atomic_idempotent_and_audited(storage, scope):
    capture = provenance(scope)
    record = item()
    store = storage.store
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: store.save_memory_item(
            record, trusted_scope=scope, capture_provenance=capture,
        ), range(2)))
    assert ids[0] == ids[1]
    receipt = storage.save_memory_item(record, trusted_scope=scope, capture_provenance=capture)
    assert receipt["id"] == ids[0] and receipt["status"] == "saved"
    assert len(storage.store.list_memory_items()) == 1
    stored = storage.store.retrieve_memory(admission_scope=scope)[0]
    assert stored.metadata[MEMORY_SCOPE_KEY] == scope.model_dump(mode="json")
    assert stored.metadata[MEMORY_CAPTURE_KEY] == capture.model_dump(mode="json")
    with pytest.raises(ValueError, match="different memory"):
        storage.save_memory_item(
            item(summary="A changed claim cannot overwrite this capture."), trusted_scope=scope,
            capture_provenance=capture,
        )
    assert storage.store.retrieve_memory(admission_scope=scope)[0].summary == record.summary


@pytest.mark.parametrize("field,value", [
    ("team_id", "T222OTHER"), ("channel_id", "C222OTHER"), ("user_id", "U222OTHER"),
    ("project_key", "different-project"),
])
def test_scope_filters_before_ranking_and_top_k(storage, scope, field, value):
    other = SlackMemoryScope(**{**scope.model_dump(), field: value})
    storage.store.save_memory_item(
        item(title="pilot pilot pilot pilot"), trusted_scope=other,
        capture_provenance=provenance(other),
    )
    storage.store.save_memory_item(item(title="pilot pilot pilot"))
    storage.store.save_memory_item(
        item(memory_type="manager_loop_efficiency", title="pilot pilot pilot"),
        trusted_scope=scope, capture_provenance=provenance(scope, 2),
    )
    wanted = storage.store.save_memory_item(
        item(title="pilot"), trusted_scope=scope, capture_provenance=provenance(scope, 3),
    )
    assert [r.id for r in storage.store.retrieve_memory(
        "pilot", limit=1, admission_scope=scope,
    )] == [wanted]
    assert len(storage.store.retrieve_memory("pilot")) == 4  # Legacy semantics remain available.


def test_exact_memory_id_filter_runs_before_ranking_and_keeps_scope(storage, scope):
    wanted = storage.store.save_memory_item(
        item(title="pilot"), trusted_scope=scope,
        capture_provenance=provenance(scope),
    )
    for turn in range(2, 32):
        storage.store.save_memory_item(
            item(title=f"pilot pilot pilot {turn}"), trusted_scope=scope,
            capture_provenance=provenance(scope, turn),
        )
    assert [record.id for record in storage.store.retrieve_memory(
        "pilot", admission_scope=scope, memory_ids=[wanted],
    )] == [wanted]
    assert storage.store.retrieve_memory(admission_scope=scope, memory_ids=[]) == []
    with pytest.raises(ValueError, match="at most four"):
        storage.store.retrieve_memory(admission_scope=scope, memory_ids=[1, 2, 3, 4, 5])


def test_scoped_retrieval_preserves_approval_safety_and_expiry(storage, scope):
    for turn, changes in enumerate((
        {"approval_state": "pending"}, {"safe_for_prompt": False},
        {"expires_at": "2020-01-01T00:00:00Z"}, {"memory_type": "approval_decision"},
    ), 1):
        storage.store.save_memory_item(
            item(**changes), trusted_scope=scope, capture_provenance=provenance(scope, turn),
        )
    assert not storage.store.retrieve_memory(
        admission_scope=scope, approved_only=False, safe_for_prompt=False,
    )
    result = query_scoped_memory_facts(storage, scope=scope)
    assert result.model_context is None and result.serialized_bytes == 0


def test_capture_requires_current_authorization_and_exact_allowed_references(storage, scope):
    capture = ScopedMemoryCaptureInput(
        kind="preference", title="Preference", summary="Bounded work.",
    )
    with pytest.raises(ValueError, match="authorization"):
        capture_scoped_memory(
            storage, scope=scope, provenance=provenance(scope), capture_authorized=False,
            capture=capture, source_refs={},
        )
    for refs, source_refs in ((["unknown"], {}), ([], {}), (["known"], {"known": "not-a-url"})):
        with pytest.raises(ValueError):
            capture_scoped_memory(
                storage, scope=scope, provenance=provenance(scope), capture_authorized=True,
                capture=ScopedMemoryCaptureInput(
                    kind="reference", title="Reference", summary="Saved source.",
                    source_ref_ids=refs,
                ), source_refs=source_refs,
            )
    assert not storage.store.list_memory_items()


def test_capture_uses_existing_builders_and_audited_store_without_model_authority(storage, scope):
    url = "https://example.org/public-source/" + "a" * 400
    for turn, kind in enumerate(("preference", "reference"), 1):
        receipt = capture_scoped_memory(
            storage, scope=scope, provenance=provenance(scope, turn), capture_authorized=True,
            capture=ScopedMemoryCaptureInput(
                kind=kind, title=f"Synthetic {kind}", summary="Use this bounded source.",
                source_ref_ids=["known"] if kind == "reference" else [],
            ), source_refs={"known": url},
        )
        assert receipt["table"] == "memory_items"
    result = query_scoped_memory_facts(storage, scope=scope)
    assert len(result.model_context["facts"]) == 2
    by_type = {fact["memory_type"]: fact for fact in result.model_context["facts"]}
    assert by_type["operator_reference"]["source_ids"] == ["known"]
    assert by_type["operator_reference"]["source_urls"] == [url]
    assert by_type["operator_strategy"]["source_ids"][0].startswith("operator-request:")
    with pytest.raises(ValidationError):
        ScopedMemoryCaptureInput(
            kind="preference", title="Bounded", summary="No authority fields.",
            approval_state="approved",
        )


def test_projection_uses_actual_utf8_bytes_and_omits_whole_oversized_facts(storage, scope):
    for turn, record in enumerate((
        item(summary="方法" * 350, content={"source_urls": ["https://example.org/" + "x" * 5000]}),
        item(title="Smaller", summary="A small whole fact.", source_ids=["exact-source-id"]),
        item(title="Second", summary="Another whole fact."),
        item(title="Third", summary="A third fact is omitted."),
    ), 1):
        storage.store.save_memory_item(
            record, trusted_scope=scope, capture_provenance=provenance(scope, turn),
        )
    records = sorted(storage.store.retrieve_memory(admission_scope=scope), key=lambda r: r.id)
    result = project_scoped_memory_facts(records, max_bytes=9999, max_facts=99)
    assert [f["title"] for f in result.model_context["facts"]] == [
        "Smaller", "Second", "Third",
    ]
    assert result.omitted_count == 1
    actual = len(json.dumps(
        result.model_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    assert result.serialized_bytes == actual <= 4096
    assert result.model_context["facts"][0]["source_ids"] == ["exact-source-id"]
    for tiny in (0, 2, actual - 1):
        bounded = project_scoped_memory_facts(records, max_bytes=tiny)
        assert bounded.serialized_bytes <= tiny
    assert project_scoped_memory_facts(records, max_bytes=0).model_context is None


def test_scoped_query_admits_at_most_four_whole_facts(storage, scope):
    for turn in range(1, 6):
        storage.store.save_memory_item(
            item(title=f"Synthetic bounded plan {turn}"),
            trusted_scope=scope, capture_provenance=provenance(scope, turn),
        )
    result = query_scoped_memory_facts(storage, scope=scope, query="bounded plan")
    assert len(result.model_context["facts"]) == 4
    assert result.omitted_count == 1
    assert result.serialized_bytes <= 4096
    assert len({fact["memory_id"] for fact in result.model_context["facts"]}) == 4
    assert len(project_scoped_memory_facts(
        storage.store.retrieve_memory(admission_scope=scope),
        max_bytes=99999, max_facts=999,
    ).model_context["facts"]) == 4
