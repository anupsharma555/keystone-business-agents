from __future__ import annotations

from pathlib import Path

from keystone_agents import workflow_runner
from keystone_agents.orchestration import stages


def test_stage_boundary_delegates_to_the_existing_single_kernel() -> None:
    assert stages.PreparedWorkItemStep is workflow_runner.PreparedWorkItemStep
    assert stages.prepare_work_item_step is workflow_runner.prepare_work_item_step
    assert (
        stages.run_prepared_work_item_specialist
        is workflow_runner.run_prepared_work_item_specialist
    )
    assert (
        stages.finalize_prepared_work_item_step
        is workflow_runner.finalize_prepared_work_item_step
    )
    assert (
        stages.chief_workflow_requests_marked_airtable_test_lifecycle
        is workflow_runner._chief_workflow_requests_marked_airtable_test_lifecycle
    )
    assert (
        stages.inline_gmail_fixture_from_request
        is workflow_runner._inline_gmail_fixture_from_request
    )
    assert stages.load_workflow_external_context is workflow_runner._load_external_context


def test_langgraph_uses_public_stage_boundary_not_private_runner_imports() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "keystone_agents"
        / "langgraph_workflow.py"
    ).read_text(encoding="utf-8")

    assert "from keystone_agents.orchestration.stages import" in source
    assert "from keystone_agents.workflow_runner import" not in source


def test_cli_uses_public_stage_boundary_not_private_runner_imports() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "keystone_agents"
        / "entrypoints"
        / "cli_impl.py"
    ).read_text(encoding="utf-8")

    assert "from keystone_agents.orchestration.stages import" in source
    assert "from keystone_agents.workflow_runner import" not in source
