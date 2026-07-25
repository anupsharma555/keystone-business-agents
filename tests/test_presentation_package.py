from __future__ import annotations

from keystone_agents import reporting, terminal_result_consistency
from keystone_agents.execution_request import (
    attach_execution_public_result as legacy_attach_execution_public_result,
)
from keystone_agents.presentation import consistency, renderers
from keystone_agents.presentation.public_result import (
    attach_execution_public_result,
)


def test_legacy_renderer_imports_are_compatibility_facades() -> None:
    assert reporting.render_work_item_result_text is renderers.render_work_item_result_text
    assert reporting.render_pipeline_report is renderers.render_pipeline_report
    assert reporting.sensitive_text_summary is renderers.sensitive_text_summary


def test_legacy_terminal_consistency_import_is_a_compatibility_facade() -> None:
    assert (
        terminal_result_consistency.reconcile_failed_review
        is consistency.reconcile_failed_review
    )


def test_public_result_boundary_preserves_legacy_assembly_contract() -> None:
    assert legacy_attach_execution_public_result is attach_execution_public_result

    payload = {
        "status": "done",
        "human_summary": "The bounded read completed.",
        "output": {"summary": "The bounded read completed."},
    }
    legacy_payload = {
        "status": "done",
        "human_summary": "The bounded read completed.",
        "output": {"summary": "The bounded read completed."},
    }

    result = attach_execution_public_result(payload)
    legacy_result = legacy_attach_execution_public_result(legacy_payload)

    assert result == legacy_result
    assert payload == legacy_payload
    assert payload["slack_display_text"] == "The bounded read completed."
