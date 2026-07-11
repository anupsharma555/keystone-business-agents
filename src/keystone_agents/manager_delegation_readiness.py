"""Pre-live evidence registry for manager-delegated provider operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManagerDelegationReadinessCase:
    """Separate manager-entry, owner execution, and joined-proof evidence."""

    provider_family: str
    owning_agent: str
    natural_request: str
    manager_entry_nodeids: tuple[str, ...]
    provider_lifecycle_nodeids: tuple[str, ...]
    required_invariants: tuple[str, ...]
    manager_provider_join_proven: bool
    next_live_proof: str


MANAGER_DELEGATION_READINESS_CASES: tuple[ManagerDelegationReadinessCase, ...] = (
    ManagerDelegationReadinessCase(
        provider_family="calendar",
        owning_agent="chief_of_staff/calendar_actions",
        natural_request=(
            "Change the note on the named paper due-date event, verify the same "
            "event, and keep its provider ID internal."
        ),
        manager_entry_nodeids=(
            "tests/test_cli.py::test_chief_calendar_fast_path_resolves_natural_update_reference",
        ),
        provider_lifecycle_nodeids=(
            "tests/test_google_calendar_tool.py::"
            "test_calendar_create_update_delete_lifecycle_verifies_provider",
        ),
        required_invariants=(
            "natural_reference_resolves_unique_active_object",
            "provider_id_internal",
            "direct_operator_scope_not_reapproved",
            "read_back_required",
            "no_duplicate_final",
        ),
        manager_provider_join_proven=True,
        next_live_proof="Reuse existing Slack natural-reference PASS; do not rerun.",
    ),
    ManagerDelegationReadinessCase(
        provider_family="gmail",
        owning_agent="gmail_triage/outreach_composer",
        natural_request=(
            "Read the selected Gmail thread, prepare one draft for review, revise "
            "the same draft, verify it, and do not send."
        ),
        manager_entry_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_chief_of_staff_email_reply_workflow_delegates_to_gmail_then_outreach",
        ),
        provider_lifecycle_nodeids=(
            "tests/test_sdk_execution.py::"
            "test_gmail_sdk_joined_natural_create_executes_verified_provider_draft",
            "tests/test_sdk_execution.py::"
            "test_gmail_sdk_joined_natural_update_reuses_uniquely_resolved_draft",
        ),
        required_invariants=(
            "selected_thread_identity_preserved",
            "same_draft_identity_preserved",
            "zero_or_ambiguous_match_blocks",
            "no_send",
            "marker_restricted_cleanup",
        ),
        manager_provider_join_proven=True,
        next_live_proof="Reuse selected-thread to verified draft PASS; do not rerun.",
    ),
    ManagerDelegationReadinessCase(
        provider_family="airtable",
        owning_agent="airtable_context_agent",
        natural_request=(
            "Create one marked expense in the named table, verify it, update the "
            "same record, and remove only that test record."
        ),
        manager_entry_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_business_expense_receipt_request_is_not_generic_airtable_context_advisory",
        ),
        provider_lifecycle_nodeids=(
            "tests/test_chief_of_staff.py::"
            "test_airtable_delete_test_record_reads_deletes_and_verifies_absence",
        ),
        required_invariants=(
            "manager_selects_airtable_owner",
            "exact_table_and_record_scope",
            "same_record_identity_preserved",
            "read_back_required",
            "marker_restricted_cleanup",
        ),
        manager_provider_join_proven=False,
        next_live_proof=(
            "One manager-entry rerun with the delegated Airtable turn limit capped at "
            "four and the sanitized provider-ID-free receipt; reuse the verified "
            "create/update/delete mechanics."
        ),
    ),
    ManagerDelegationReadinessCase(
        provider_family="google_workspace",
        owning_agent="google_workspace_context_agent",
        natural_request=(
            "Create one marked Sheet in KNIOps, update the same row without an ID, "
            "verify it, and move the test Sheet to trash."
        ),
        manager_entry_nodeids=(
            "tests/test_chief_of_staff.py::"
            "test_run_script_allows_bounded_workspace_writes_for_explicit_doc_request",
        ),
        provider_lifecycle_nodeids=(
            "tests/test_google_sheet_test_lifecycle.py::"
            "test_google_sheet_lifecycle_verifies_each_step_and_trash",
        ),
        required_invariants=(
            "manager_selects_workspace_owner",
            "approved_location_preserved",
            "same_artifact_identity_preserved",
            "read_back_required",
            "trash_verified",
        ),
        manager_provider_join_proven=False,
        next_live_proof=(
            "One manager-entry marked Sheet lifecycle using the existing owner-session proof."
        ),
    ),
    ManagerDelegationReadinessCase(
        provider_family="zotero",
        owning_agent="zotero_context_agent",
        natural_request=(
            "Create one marked Zotero note for the selected item, revise the same "
            "note without its key, verify it, and remove the test note."
        ),
        manager_entry_nodeids=(
            "tests/test_manager_rwm_acceptance.py::"
            "test_chief_structured_handoff_keeps_nested_specialist_advisory",
        ),
        provider_lifecycle_nodeids=(
            "tests/test_sdk_execution.py::"
            "test_structured_context_agent_session_followup_preserves_identity_and_updates",
            "tests/test_zotero_test_note_actions.py::"
            "test_zotero_delete_test_note_verifies_absence",
        ),
        required_invariants=(
            "manager_selects_zotero_context",
            "nested_call_does_not_inherit_write_authority",
            "same_note_identity_preserved",
            "version_aware_update",
            "absence_verified",
        ),
        manager_provider_join_proven=False,
        next_live_proof=(
            "One manager-entry marked note lifecycle using the existing versioned owner proof."
        ),
    ),
)
