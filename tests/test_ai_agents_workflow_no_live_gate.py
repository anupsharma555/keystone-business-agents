from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_gate_module():
    path = Path("scripts/run_ai_agents_workflow_no_live_gate.py")
    spec = spec_from_file_location("run_ai_agents_workflow_no_live_gate", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_no_live_gate_excludes_legacy_promptfoo_and_live_flags() -> None:
    module = _load_gate_module()
    commands = module.gate_commands()
    rendered = [" ".join(command) for command in commands]

    assert any(
        "compare_langgraph_quality.py --all-scenarios --require-ready" in row
        for row in rendered
    )
    assert any("run_slack_agent_expansion_gate.py --quiet" in row for row in rendered)
    assert any("run_advanced_manager_acceptance.py" in row for row in rendered)
    assert any("run_manager_delegation_readiness.py" in row for row in rendered)
    assert any("run_slack_entrypoint_readiness.py" in row for row in rendered)
    assert any("eval:slack:anu60-preflight" in row for row in rendered)
    assert all("eval:slack:strict-readiness" not in row for row in rendered)
    assert all("eval:promptfoo:json" not in row for row in rendered)
    assert all("--live-sdk" not in row for row in rendered)
    assert all("--live-search" not in row for row in rendered)
    assert all("strict-live-readiness" not in row for row in rendered)


def test_current_no_live_gate_can_isolate_repo_only_checks() -> None:
    module = _load_gate_module()
    commands = module.gate_commands(include_anu60=False)

    assert len(commands) == 6
    assert commands[0][2:4] == ["pytest", "tests/test_advanced_manager_scenarios.py"]


def test_current_no_live_gate_can_optionally_check_deferred_evals_runtime() -> None:
    module = _load_gate_module()
    commands = module.gate_commands(include_evals_readiness=True)

    assert any("eval:slack:strict-readiness" in " ".join(row) for row in commands)
