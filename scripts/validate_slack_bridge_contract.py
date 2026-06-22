# ruff: noqa: E402,E501
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from validate_slack_result_rendering_examples import (
    DEFAULT_EXAMPLES,
    validate_examples,
)

from keystone_agents.slack_action_contract import business_agent_slack_contract

DEFAULT_CONTRACT = Path("contracts/keystone_slack_business_agent_contract.v1.json")
REQUIRED_CAPABILITIES = {
    "context_agent_result_rendering",
    "named_agent_result_rendering",
    "source_channel_response_routing",
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def validate_bridge_contract(
    contract_path: Path = DEFAULT_CONTRACT,
    examples_path: Path = DEFAULT_EXAMPLES,
) -> list[str]:
    failures: list[str] = []
    expected = business_agent_slack_contract()
    actual = _load_json(contract_path)

    for key in ("schema", "version", "result_rendering", "slack_response_routing"):
        if actual.get(key) != expected.get(key):
            failures.append(f"{contract_path}: {key} does not match canonical contract")

    actual_capabilities = set(actual.get("capabilities") or [])
    missing_capabilities = sorted(REQUIRED_CAPABILITIES - actual_capabilities)
    if missing_capabilities:
        failures.append(
            f"{contract_path}: missing capabilities {', '.join(missing_capabilities)}"
        )

    routing = actual.get("slack_response_routing")
    if not isinstance(routing, dict):
        failures.append(f"{contract_path}: missing slack_response_routing object")
    else:
        policy = str(routing.get("policy") or "")
        if "source Slack channel/thread" not in policy or "do not funnel" not in policy:
            failures.append(
                f"{contract_path}: slack_response_routing policy must require source-channel replies"
            )

    failures.extend(validate_examples(examples_path))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the KBA Slack bridge contract without Slack or model calls."
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT,
        help=f"Exported contract JSON to validate (default: {DEFAULT_CONTRACT})",
    )
    parser.add_argument(
        "--examples",
        type=Path,
        default=DEFAULT_EXAMPLES,
        help=f"Rendering examples JSON to validate (default: {DEFAULT_EXAMPLES})",
    )
    args = parser.parse_args(argv)

    failures = validate_bridge_contract(args.contract, args.examples)
    if failures:
        print("Slack bridge contract validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("Validated Slack bridge contract, rendering examples, and source-channel routing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
