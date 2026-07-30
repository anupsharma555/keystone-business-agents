from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from keystone_agents.agents import manual_request_planner as planner_module
from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.planning.decision_cache import (
    PlannerDecisionCache,
    build_planner_decision_cache_identity,
    planner_context_revision_payload,
    planner_decision_cache_eligibility,
    planner_profile_fingerprint,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _plan(**updates) -> ManualRequestPlan:
    payload = dict(
        source="llm",
        target_agent="google_workspace_context_agent",
        intent="context_lookup",
        provider_system="google_workspace",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        objective="Find and summarize the named document.",
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
    )
    payload.update(updates)
    return ManualRequestPlan(**payload)


def _profile() -> str:
    return planner_profile_fingerprint(
        instructions="stable planner instructions",
        output_type=ManualRequestPlan,
        provider="openai",
        model="gpt-5.4-mini",
    )


def test_exact_identity_is_stable_across_transport_threads() -> None:
    first = build_planner_decision_cache_identity(
        request_text="@KNI  find README.doc in KNIOps ",
        requested_agent="google_workspace_context_agent",
        workflow_context=None,
        profile_fingerprint=_profile(),
        scope="operator-fixture",
    )
    second = build_planner_decision_cache_identity(
        request_text="@kni find README.doc in KNIOps",
        requested_agent="google_workspace_context_agent",
        workflow_context={},
        profile_fingerprint=_profile(),
        scope="operator-fixture",
    )

    assert first.cache_key == second.cache_key
    assert "README" not in first.cache_key
    assert "operator-fixture" not in first.cache_key


def test_identity_invalidates_on_context_profile_and_scope_changes() -> None:
    base = dict(
        request_text="summarize the same document",
        requested_agent="google_workspace_context_agent",
        profile_fingerprint=_profile(),
        scope="operator-fixture",
    )
    first = build_planner_decision_cache_identity(
        workflow_context={"slack_thread_root": "find README.doc"},
        **base,
    )
    changed_context = build_planner_decision_cache_identity(
        workflow_context={"slack_thread_root": "find OPERATIONS.doc"},
        **base,
    )
    changed_profile = build_planner_decision_cache_identity(
        workflow_context={"slack_thread_root": "find README.doc"},
        **{**base, "profile_fingerprint": "new-profile"},
    )
    changed_scope = build_planner_decision_cache_identity(
        workflow_context={"slack_thread_root": "find README.doc"},
        **{**base, "scope": "another-operator"},
    )

    assert (
        len(
            {
                first.cache_key,
                changed_context.cache_key,
                changed_profile.cache_key,
                changed_scope.cache_key,
            }
        )
        == 4
    )


def test_identity_invalidates_when_verified_provider_result_count_changes() -> None:
    common = dict(
        request_text="Summarize that without changing anything.",
        requested_agent="google_workspace_context_agent",
        profile_fingerprint=_profile(),
        scope="operator-fixture",
    )
    provider_scope = {
        "source_run_id": "run-workspace-read",
        "provider_system": "google_workspace",
        "provider_read_scope": "bounded_collection",
        "item_count": 1,
        "item_refs": ["file-one"],
        "complete": True,
        "verified": True,
    }

    first = build_planner_decision_cache_identity(
        workflow_context={"prior_provider_result_scope": provider_scope},
        **common,
    )
    changed_count = build_planner_decision_cache_identity(
        workflow_context={
            "prior_provider_result_scope": {
                **provider_scope,
                "item_count": 2,
            }
        },
        **common,
    )

    assert first.context_hash
    assert changed_count.context_hash
    assert first.cache_key != changed_count.cache_key


def test_identity_invalidates_when_exact_verified_object_changes() -> None:
    common = dict(
        request_text="Summarize that without changing anything.",
        requested_agent="google_workspace_context_agent",
        profile_fingerprint=_profile(),
        scope="operator-fixture",
    )

    def workflow_context(
        *,
        object_id: str = "doc-provider-id-one",
        google_account: str = "workspace-account-one",
    ) -> dict[str, object]:
        return {
            "execution_continuation": {
                "verified_objects": [
                    {
                        "provider_system": "google_drive",
                        "object_type": "google_document",
                        "object_id": object_id,
                        "display_name": "README.doc",
                        "verification_status": "verified",
                        "lifecycle_state": "active",
                        "provider_scope": {
                            "folder_path": "KNIOps",
                            "google_account": google_account,
                        },
                    }
                ]
            }
        }

    first = build_planner_decision_cache_identity(
        workflow_context=workflow_context(),
        **common,
    )
    changed_object = build_planner_decision_cache_identity(
        workflow_context=workflow_context(object_id="doc-provider-id-two"),
        **common,
    )
    changed_scope = build_planner_decision_cache_identity(
        workflow_context=workflow_context(google_account="workspace-account-two"),
        **common,
    )

    assert first.context_hash
    assert len(
        {
            first.cache_key,
            changed_object.cache_key,
            changed_scope.cache_key,
        }
    ) == 3


def test_context_revision_ignores_unverified_objects_and_hashes_identity() -> None:
    verified_context = {
        "execution_continuation": {
            "provider_affinity": "google_workspace",
            "verified_objects": [
                {
                    "provider_system": "google_drive",
                    "object_type": "google_document",
                    "object_id": "private-provider-document-id",
                    "display_name": "Private Operating Notes.doc",
                    "verification_status": "verified",
                    "provider_scope": {
                        "google_account": "private-workspace-account",
                    },
                },
                {
                    "provider_system": "google_drive",
                    "object_type": "google_document",
                    "object_id": "unverified-document-id",
                    "display_name": "Unverified Notes.doc",
                    "verification_status": "unverified",
                },
            ],
        }
    }
    payload = planner_context_revision_payload(verified_context)
    serialized = str(payload)

    assert len(payload["verified_objects"]) == 1
    assert payload["continuation"] == {
        "provider_affinity": "google_workspace",
    }
    assert "private-provider-document-id" not in serialized
    assert "Private Operating Notes.doc" not in serialized
    assert "private-workspace-account" not in serialized
    assert "unverified-document-id" not in serialized
    assert payload["verified_objects"][0]["object_id_hash"]
    assert payload["verified_objects"][0]["provider_scope_hash"]


def test_cache_round_trip_is_advisory_and_expires(tmp_path: Path) -> None:
    cache = PlannerDecisionCache(tmp_path / "planner.sqlite", ttl_seconds=60)
    identity = build_planner_decision_cache_identity(
        request_text="find README.doc in KNIOps",
        requested_agent="google_workspace_context_agent",
        workflow_context=None,
        profile_fingerprint=_profile(),
        scope="operator-fixture",
    )

    cache.put(identity, _plan(), now=100.0)
    hit = cache.get(identity, now=120.0)

    assert hit is not None
    assert hit.source == "canonical:planner_cache"
    assert hit.target_agent == "google_workspace_context_agent"
    assert cache.stats() == {"entry_count": 1, "hit_count": 1}
    assert cache.get(identity, now=161.0) is None


def test_cache_rejects_mutation_temporal_and_unresolved_context() -> None:
    mutation = _plan(
        intent="business_system_write",
        provider_operations=["create"],
        side_effect_policy="approval_required",
    )
    assert not planner_decision_cache_eligibility(
        "Create a document.",
        mutation,
    ).eligible
    assert not planner_decision_cache_eligibility(
        "Find my latest document today.",
        _plan(),
    ).eligible
    assert not planner_decision_cache_eligibility(
        "Summarize that.",
        _plan(),
    ).eligible
    assert planner_decision_cache_eligibility(
        "Summarize that.",
        _plan(),
        context_hash="verified-context",
    ).eligible


def test_cache_rejects_sensitive_and_approval_dependent_plans() -> None:
    assert not planner_decision_cache_eligibility(
        "Find patient MRN 123.",
        _plan(),
    ).eligible
    assert not planner_decision_cache_eligibility(
        "Draft external outreach.",
        _plan(
            target_agent="outreach_composer",
            intent="outreach_draft",
            requires_approved_context=True,
        ),
    ).eligible


def test_resolver_reuses_exact_cached_advice_across_transport_threads(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_run_typed_sdk_agent(**kwargs):
        calls.append(kwargs["typed_input"].request_text)
        return SimpleNamespace(output=_plan())

    monkeypatch.setattr(
        planner_module,
        "run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )
    cache = PlannerDecisionCache(":memory:", ttl_seconds=300)

    first = resolve_manual_request_plan(
        "Find and summarize README.doc in KNIOps without changing anything.",
        requested_agent="google_workspace_context_agent",
        live=True,
        run_config=object(),
        workflow_state={"slack_context": {"thread_ts": "thread-one"}},
        planner_cache=cache,
    )
    second = resolve_manual_request_plan(
        "  find and summarize README.doc in KNIOps without changing anything.  ",
        requested_agent="google_workspace_context_agent",
        live=True,
        run_config=object(),
        workflow_state={"slack_context": {"thread_ts": "thread-two"}},
        planner_cache=cache,
    )

    assert first.source == "llm"
    assert second.source == "canonical:planner_cache"
    assert second.target_agent == first.target_agent
    assert calls == ["Find and summarize README.doc in KNIOps without changing anything."]


def test_resolver_reuses_eligible_followup_with_same_bounded_context(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_run_typed_sdk_agent(**kwargs):
        calls.append(kwargs["typed_input"].request_text)
        return SimpleNamespace(output=_plan())

    monkeypatch.setattr(
        planner_module,
        "run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )
    cache = PlannerDecisionCache(":memory:", ttl_seconds=300)

    for thread_ts in ("thread-one", "thread-two"):
        resolved = resolve_manual_request_plan(
            "Summarize that without changing anything.",
            requested_agent="google_workspace_context_agent",
            live=True,
            run_config=object(),
            workflow_state={
                "slack_context": {
                    "thread_ts": thread_ts,
                    "thread_root_request": "Find README.doc in KNIOps.",
                }
            },
            planner_cache=cache,
        )

    assert resolved.source == "canonical:planner_cache"
    assert calls == ["Summarize that without changing anything."]


def test_resolver_does_not_cache_temporal_advice(monkeypatch) -> None:
    call_count = 0

    def fake_run_typed_sdk_agent(**_kwargs):
        nonlocal call_count
        call_count += 1
        return SimpleNamespace(output=_plan())

    monkeypatch.setattr(
        planner_module,
        "run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )
    cache = PlannerDecisionCache(":memory:", ttl_seconds=300)

    for _ in range(2):
        resolve_manual_request_plan(
            "Find the latest README.doc today.",
            requested_agent="google_workspace_context_agent",
            live=True,
            run_config=object(),
            planner_cache=cache,
        )

    assert call_count == 2
    assert cache.stats() == {"entry_count": 0, "hit_count": 0}
