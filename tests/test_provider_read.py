from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from keystone_agents.provider_read import (
    ProviderReadConcurrencyLimitExceeded,
    ProviderReadDeadlineExceeded,
    ProviderReadExecutionContext,
    ProviderReadPlan,
    ProviderReadPolicy,
    ProviderReadSnapshot,
    activate_provider_read_context,
    current_provider_read_context,
    record_provider_read_result,
)
from keystone_agents.tool_receipt_journal import (
    reset_tool_receipt_journal,
    tool_receipt_journal,
)


def _plan(**overrides: object) -> ProviderReadPlan:
    values: dict[str, object] = {
        "provider": "gmail",
        "operation": "search",
        "resource": "messages",
        "identity": {"account": "private@example.test", "thread_id": "thread-secret"},
        "scope": {"mailbox": "inbox", "tenant": "private-tenant"},
        "query": "from:private@example.test confidential terms",
        "continuation_token": "opaque-page-token",
        "projection": ("message_id", "subject"),
        "capability_profile_fingerprint": "a" * 64,
        "policy": ProviderReadPolicy(),
    }
    values.update(overrides)
    return ProviderReadPlan(**values)


def _complete_snapshot(**overrides: object) -> ProviderReadSnapshot:
    values: dict[str, object] = {
        "completeness": "complete",
        "item_count": 2,
        "page_count": 1,
        "bytes_read": 256,
        "identity": {"account": "private@example.test"},
        "payload": [{"message_id": "secret-1"}, {"message_id": "secret-2"}],
    }
    values.update(overrides)
    return ProviderReadSnapshot(**values)


def test_plan_fingerprints_are_stable_private_and_round_trip_safe() -> None:
    first = _plan()
    reordered = _plan(
        identity={"thread_id": "thread-secret", "account": "private@example.test"},
        scope={"tenant": "private-tenant", "mailbox": "inbox"},
    )

    assert first.plan_fingerprint == reordered.plan_fingerprint
    assert first.identity_fingerprints == reordered.identity_fingerprints
    assert _plan(query="different query").plan_fingerprint != first.plan_fingerprint
    assert (
        _plan(identity={"account": "another@example.test"}).plan_fingerprint
        != first.plan_fingerprint
    )

    serialized = first.model_dump(mode="json")
    encoded = json.dumps(serialized, sort_keys=True)
    assert "private@example.test" not in encoded
    assert "thread-secret" not in encoded
    assert "confidential terms" not in encoded
    assert "opaque-page-token" not in encoded
    assert ProviderReadPlan.model_validate(serialized) == first.model_copy(
        update={
            "identity": {},
            "scope": {},
            "query": "",
            "continuation_token": "",
        }
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_provider_calls", 201),
        ("max_retries", 4),
        ("max_concurrency", 9),
    ],
)
def test_policy_rejects_values_above_hard_caps(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        ProviderReadPolicy(**{field: value})

    with pytest.raises(ValidationError, match="retry allowlist"):
        ProviderReadPolicy(retryable_http_statuses=(404,))


def test_snapshot_bounds_and_required_completeness_are_enforced() -> None:
    context = ProviderReadExecutionContext(
        _plan(
            completeness_required=True,
            policy=ProviderReadPolicy(max_items=2, max_pages=1, max_bytes=256),
        )
    )
    context.remember_snapshot("complete", _complete_snapshot())
    assert context.snapshot("complete") is not None

    with pytest.raises(ValueError, match="item_count"):
        context.remember_snapshot(
            "too-many-items",
            _complete_snapshot(item_count=3),
        )
    with pytest.raises(ValueError, match="page_count"):
        context.remember_snapshot(
            "too-many-pages",
            _complete_snapshot(page_count=2),
        )
    with pytest.raises(ValueError, match="bytes_read"):
        context.remember_snapshot(
            "too-many-bytes",
            _complete_snapshot(bytes_read=257),
        )
    with pytest.raises(ValueError, match="requires a complete"):
        context.remember_snapshot(
            "partial",
            ProviderReadSnapshot(
                completeness="truncated",
                continuation_token="next-page",
            ),
        )


def test_completeness_and_continuation_semantics_reach_safe_receipts() -> None:
    complete_context = ProviderReadExecutionContext(_plan())
    complete = _complete_snapshot()
    complete_receipt = complete_context.build_receipt(snapshot=complete)
    assert complete.complete is True
    assert complete_receipt.status == "success"
    assert complete_receipt.completeness == "complete"
    assert complete_receipt.continuation_available is False

    partial_context = ProviderReadExecutionContext(_plan())
    partial = ProviderReadSnapshot(
        completeness="truncated",
        item_count=10,
        page_count=1,
        bytes_read=512,
        continuation_token="secret-next-page",
        payload={"private": "provider content"},
    )
    partial_receipt = partial_context.build_receipt(snapshot=partial)
    assert partial.truncated is True
    assert partial.continuation_available is True
    assert partial_receipt.status == "partial"
    assert partial_receipt.completeness == "truncated"
    assert partial_receipt.continuation_available is True
    assert "secret-next-page" not in json.dumps(partial_receipt.receipt())

    with pytest.raises(ValidationError, match="complete snapshot"):
        ProviderReadSnapshot(
            completeness="complete",
            continuation_token="impossible-next-page",
        )


def test_retry_decisions_and_accounting_are_bounded_and_deterministic() -> None:
    context = ProviderReadExecutionContext(
        _plan(
            policy=ProviderReadPolicy(
                max_provider_calls=3,
                max_retries=2,
                base_retry_delay_seconds=0.5,
                max_retry_delay_seconds=2.0,
                retry_jitter_ratio=0.0,
            )
        )
    )

    assert context.try_start_attempt() is True
    assert context.retry_delay_seconds(404) is None
    assert context.retry_delay_seconds(429, retry_after_seconds=99.0) == 2.0
    assert context.try_start_attempt(retry=True) is True
    assert context.retry_delay_seconds(503) == 1.0
    assert context.try_start_attempt(retry=True) is True
    assert context.try_start_attempt(retry=True) is False
    assert context.try_start_attempt() is False

    receipt = context.build_receipt(snapshot=_complete_snapshot())
    assert receipt.attempt_count == 3
    assert receipt.retry_count == 2


def test_retry_cannot_precede_an_initial_attempt() -> None:
    context = ProviderReadExecutionContext(_plan())

    assert context.try_start_attempt(retry=True) is False
    assert context.attempt_count == 0
    assert context.retry_count == 0
    with pytest.raises(ValidationError, match="initial attempt"):
        ProviderReadPolicy(max_provider_calls=1, max_retries=1)


def test_deadline_blocks_attempts_slots_and_marks_receipt() -> None:
    now = [100.0]
    context = ProviderReadExecutionContext(
        _plan(policy=ProviderReadPolicy(timeout_seconds=1.0)),
        clock=lambda: now[0],
    )
    assert context.try_start_attempt() is True

    now[0] = 101.01
    assert context.try_start_attempt() is False
    with pytest.raises(ProviderReadDeadlineExceeded):
        with context.read_slot():
            raise AssertionError("deadline should prevent slot entry")

    receipt = context.build_receipt(error_codes=("provider_timeout",))
    assert receipt.status == "deadline_exceeded"
    assert receipt.deadline_exceeded is True
    assert receipt.error_codes == ("provider_timeout",)


def test_concurrency_is_capped_without_starting_provider_io() -> None:
    context = ProviderReadExecutionContext(_plan(policy=ProviderReadPolicy(max_concurrency=1)))

    with context.read_slot():
        with pytest.raises(ProviderReadConcurrencyLimitExceeded):
            with context.read_slot(timeout_seconds=0.0):
                raise AssertionError("second slot should not be available")

    assert context.max_active_count == 1


def test_request_context_reuses_services_and_snapshots_then_resets() -> None:
    calls: list[str] = []
    plan = _plan()
    assert current_provider_read_context() is None

    with activate_provider_read_context(None) as inactive:
        assert inactive is None
        assert current_provider_read_context() is None

    with activate_provider_read_context(plan) as context:
        assert context is not None
        assert current_provider_read_context() is context
        first = context.service("gmail-client", lambda: calls.append("built") or object())
        second = context.service("gmail-client", lambda: object())
        assert first is second
        assert calls == ["built"]

        snapshot = _complete_snapshot()
        context.remember_snapshot("first-page", snapshot)
        assert context.snapshot("first-page") is snapshot

        with activate_provider_read_context(None) as inherited:
            assert inherited is context
            assert current_provider_read_context() is context

    assert current_provider_read_context() is None


def test_result_receipt_keeps_content_out_of_generic_journal() -> None:
    reset_tool_receipt_journal()
    with activate_provider_read_context(_plan(provider="google_workspace")) as context:
        assert context is not None
        assert context.try_start_attempt() is True
        receipt = record_provider_read_result(
            "google_drive_search_files",
            {
                "status": "success",
                "items": [
                    {
                        "id": "private-file-id",
                        "name": "Work README.doc",
                        "text": "work-only body content",
                    }
                ],
                "item_count": 1,
            },
        )

    assert receipt is not None
    assert receipt.status == "success"
    captured = tool_receipt_journal()
    assert captured[-1]["provider"] == "google_workspace"
    assert captured[-1]["provider_read"] is True
    assert captured[-1]["provider_write"] is False
    encoded = json.dumps(captured)
    assert "private-file-id" not in encoded
    assert "Work README.doc" not in encoded
    assert "work-only body content" not in encoded


def test_serialized_contracts_never_include_provider_content_or_raw_identity() -> None:
    secret_fragments = (
        "private@example.test",
        "thread-secret",
        "private-tenant",
        "confidential terms",
        "opaque-page-token",
        "provider-secret-value",
        "secret-next-page",
    )
    plan = _plan()
    snapshot = ProviderReadSnapshot(
        completeness="truncated",
        item_count=1,
        page_count=1,
        bytes_read=128,
        identity={"message_id": "provider-secret-value"},
        continuation_token="secret-next-page",
        payload={"body": "provider-secret-value"},
    )
    context = ProviderReadExecutionContext(plan)
    assert context.try_start_attempt() is True
    receipt = context.build_receipt(snapshot=snapshot, cache_hit=True)

    serialized = json.dumps(
        {
            "plan": plan.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
            "receipt": receipt.receipt(),
        },
        sort_keys=True,
    )
    for fragment in secret_fragments:
        assert fragment not in serialized
    assert receipt.provider_read is True
    assert receipt.provider_write is False
    assert receipt.send_enabled is False
    assert receipt.cache_hit is True

    with pytest.raises(ValidationError):
        context.build_receipt(error_codes=("provider-secret-value",))
