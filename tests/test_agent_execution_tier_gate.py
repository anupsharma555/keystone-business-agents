from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_module():
    path = Path("scripts/run_agent_execution_tier_gate.py")
    spec = spec_from_file_location("run_agent_execution_tier_gate", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_execution_tiers_cover_framework_behavior_without_live_flags() -> None:
    module = _load_module()

    assert list(module.EXECUTION_TIER_CASES) == ["simple", "intermediate", "advanced"]
    assert all(len(cases) >= 4 for cases in module.EXECUTION_TIER_CASES.values())
    rendered = "\n".join(
        case for cases in module.EXECUTION_TIER_CASES.values() for case in cases
    )
    assert "test_sdk_execution.py" in rendered
    assert "test_workflow_runner.py" in rendered
    assert "test_langgraph_workflow.py" in rendered
    assert "promptfoo" not in rendered.lower()
    assert "--live-sdk" not in rendered
    assert "--live-search" not in rendered


def test_execution_tier_commands_use_repo_python_and_selected_tier() -> None:
    module = _load_module()
    commands = module.tier_commands(["intermediate"])

    assert len(commands) == 1
    tier, command = commands[0]
    assert tier == "intermediate"
    assert command[1:3] == ["-m", "pytest"]
    assert command[-1] == "-q"
