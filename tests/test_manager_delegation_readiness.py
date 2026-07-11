from __future__ import annotations

from pathlib import Path

from keystone_agents.manager_delegation_readiness import MANAGER_DELEGATION_READINESS_CASES


def test_manager_delegation_readiness_covers_required_provider_families() -> None:
    assert [case.provider_family for case in MANAGER_DELEGATION_READINESS_CASES] == [
        "calendar",
        "gmail",
        "airtable",
        "google_workspace",
        "zotero",
    ]
    assert all(case.manager_entry_nodeids for case in MANAGER_DELEGATION_READINESS_CASES)
    assert all(case.provider_lifecycle_nodeids for case in MANAGER_DELEGATION_READINESS_CASES)


def test_manager_delegation_readiness_separates_joined_from_layered_proof() -> None:
    joined = {
        case.provider_family
        for case in MANAGER_DELEGATION_READINESS_CASES
        if case.manager_provider_join_proven
    }
    assert joined == {"calendar", "gmail", "airtable", "zotero"}
    assert all(
        case.next_live_proof.startswith("Reuse")
        for case in MANAGER_DELEGATION_READINESS_CASES
        if case.manager_provider_join_proven
    )
    assert all(
        "One manager-entry" in case.next_live_proof
        for case in MANAGER_DELEGATION_READINESS_CASES
        if not case.manager_provider_join_proven
    )


def test_manager_delegation_readiness_guards_identity_approval_and_cleanup() -> None:
    invariants = {
        invariant
        for case in MANAGER_DELEGATION_READINESS_CASES
        for invariant in case.required_invariants
    }
    assert "provider_id_internal" in invariants
    assert "direct_operator_scope_not_reapproved" in invariants
    assert "zero_or_ambiguous_match_blocks" in invariants
    assert "same_record_identity_preserved" in invariants
    assert "same_artifact_identity_preserved" in invariants
    assert "same_note_identity_preserved" in invariants
    assert "marker_restricted_cleanup" in invariants
    assert "absence_verified" in invariants


def test_manager_delegation_proof_nodeids_exist() -> None:
    for case in MANAGER_DELEGATION_READINESS_CASES:
        for nodeid in (*case.manager_entry_nodeids, *case.provider_lifecycle_nodeids):
            path_value, separator, test_name = nodeid.partition("::")
            assert separator and test_name.startswith("test_")
            path = Path(path_value)
            assert path.is_file(), nodeid
            assert f"def {test_name}(" in path.read_text(encoding="utf-8"), nodeid
