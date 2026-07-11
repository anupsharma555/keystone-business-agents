from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from keystone_agents.advanced_manager_scenarios import (
    ADVANCED_MANAGER_SCENARIOS,
    get_advanced_manager_scenario,
)


def test_advanced_manager_registry_covers_anu222_acceptance_families() -> None:
    assert len(ADVANCED_MANAGER_SCENARIOS) == 12
    assert [scenario.scenario_id for scenario in ADVANCED_MANAGER_SCENARIOS] == [
        f"AMS-{index:02d}" for index in range(1, 13)
    ]
    families = {scenario.family for scenario in ADVANCED_MANAGER_SCENARIOS}
    assert families == {
        "multi_turn_correction",
        "selected_object_modification",
        "conflicting_instructions",
        "partial_specialist_failure",
        "evidence_disagreement",
        "approved_write_transition",
        "reversal",
        "advanced_advisory_synthesis",
        "output_receipt",
    }


def test_advanced_manager_scenarios_preserve_safety_and_low_friction() -> None:
    rendered = "\n".join(
        item
        for scenario in ADVANCED_MANAGER_SCENARIOS
        for item in (
            scenario.natural_request,
            *scenario.required_invariants,
            *scenario.proof_nodeids,
        )
    )
    assert all(not scenario.live_model_required for scenario in ADVANCED_MANAGER_SCENARIOS)
    assert all(
        not scenario.external_side_effects_allowed for scenario in ADVANCED_MANAGER_SCENARIOS
    )
    assert "named_low_risk_owner_routes_directly" in rendered
    assert "no_chief_wrapper" in rendered
    assert "direct_operator_scope_not_reapproved" in rendered
    assert "stale_write_cannot_execute" in rendered
    assert "completed_provider_action_not_duplicated" in rendered
    assert "promptfoo" not in rendered.lower()


def test_advanced_manager_scenario_proof_nodeids_exist() -> None:
    for scenario in ADVANCED_MANAGER_SCENARIOS:
        assert scenario.proof_nodeids
        for nodeid in scenario.proof_nodeids:
            path_value, separator, test_name = nodeid.partition("::")
            assert separator and test_name.startswith("test_")
            path = Path(path_value)
            assert path.is_file(), nodeid
            assert f"def {test_name}(" in path.read_text(encoding="utf-8"), nodeid


def test_advanced_manager_scenario_lookup_is_stable() -> None:
    assert get_advanced_manager_scenario("AMS-09").family == "approved_write_transition"


def test_advanced_manager_runner_uses_registry_without_live_flags() -> None:
    path = Path("scripts/run_advanced_manager_acceptance.py")
    spec = spec_from_file_location("run_advanced_manager_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.ADVANCED_MANAGER_SCENARIOS is ADVANCED_MANAGER_SCENARIOS
    source = path.read_text(encoding="utf-8")
    assert "--live-sdk" not in source
    assert "--live-search" not in source
    assert "legacy_promptfoo_included" in source


def test_package_exposes_advanced_manager_no_live_gate() -> None:
    package = Path("package.json").read_text(encoding="utf-8")
    assert '"test:advanced-manager:no-live"' in package
    assert "scripts/run_advanced_manager_acceptance.py" in package
