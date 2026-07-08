from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIBLING_SLACK_ROOT = PROJECT_ROOT.parent / "keystone-slack"
EXPANSION_ARTIFACT = "artifacts/anu60_expansion_gate_after_acceptance_map.json"


SIBLING_BRIDGE_TESTS = (
    "test_direct_rss_context_agent_request_renders_human_summary",
    "test_direct_preprints_context_agent_request_renders_human_summary",
    "test_conversational_named_agent_request_uses_canonical_ask_and_human_summary",
    "test_focused_company_brief_prefers_human_summary_over_metadata",
    "test_focused_company_brief_renders_links_contacts_and_clean_copy",
    "test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group",
    "test_business_agents_streaming_run_command_timeout_kills_group",
    "test_business_agents_run_agent_submission_blocked_preflight_renders_blocked",
)


def _run(label: str, command: list[str], cwd: Path = PROJECT_ROOT) -> None:
    print(f"\n== {label} ==")
    print(" ".join(command))
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(cwd)
        if not existing_pythonpath
        else f"{cwd}{os.pathsep}{existing_pythonpath}"
    )
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _run_sibling_bridge_tests() -> None:
    print("\n== Sibling keystone-slack ANU-60 bridge fixtures ==")
    test_file = SIBLING_SLACK_ROOT / "tests" / "test_app_mentions.py"
    print(f"load {test_file} :: SlackAppMentionTests[{len(SIBLING_BRIDGE_TESTS)}]")

    previous_cwd = Path.cwd()
    sys.path.insert(0, str(SIBLING_SLACK_ROOT))
    try:
        os.chdir(SIBLING_SLACK_ROOT)
        spec = importlib.util.spec_from_file_location(
            "anu60_sibling_test_app_mentions",
            test_file,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load sibling test file: {test_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        suite = unittest.TestSuite()
        for test_name in SIBLING_BRIDGE_TESTS:
            suite.addTest(module.SlackAppMentionTests(test_name))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if not result.wasSuccessful():
            raise RuntimeError("Sibling keystone-slack ANU-60 bridge fixtures failed")
    finally:
        os.chdir(previous_cwd)
        try:
            sys.path.remove(str(SIBLING_SLACK_ROOT))
        except ValueError:
            pass


def main() -> int:
    if not SIBLING_SLACK_ROOT.exists():
        print(f"Missing sibling keystone-slack repo: {SIBLING_SLACK_ROOT}", file=sys.stderr)
        return 1

    commands = (
        (
            "ANU-60 proof packet validator",
            [sys.executable, "scripts/validate_anu60_live_slack_proof_packet.py"],
            PROJECT_ROOT,
        ),
        (
            "KBA prompt/doc and Slack action contract tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_prompt_contracts.py",
                "tests/test_slack_action_contract.py",
                "-q",
            ],
            PROJECT_ROOT,
        ),
        (
            "KBA Slack bridge contract validator",
            [sys.executable, "scripts/validate_slack_bridge_contract.py"],
            PROJECT_ROOT,
        ),
        (
            "KBA Slack rendering examples validator",
            [sys.executable, "scripts/validate_slack_result_rendering_examples.py"],
            PROJECT_ROOT,
        ),
        (
            "KBA Slack expansion gate",
            [
                sys.executable,
                "scripts/run_slack_agent_expansion_gate.py",
                "--quiet",
                "--json-output",
                EXPANSION_ARTIFACT,
            ],
            PROJECT_ROOT,
        ),
    )

    for label, command, cwd in commands:
        _run(label, command, cwd)
    _run_sibling_bridge_tests()

    print("\nANU-60 no-live preflight passed; no live Slack/API/model/search calls made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
