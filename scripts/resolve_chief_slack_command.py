"""Resolve one natural-language Chief ask to a configured native KS command."""

from __future__ import annotations

import argparse
import json

from keystone_agents.agents.chief_of_staff import (
    chief_slack_command_resolution_is_applicable,
    resolve_high_confidence_chief_slack_command,
    run_chief_slack_command_resolver,
    validate_chief_slack_command_resolution,
)
from keystone_agents.config import load_settings
from keystone_agents.tools.chief_of_staff_tool import list_slack_slash_commands


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--slack-repo-path", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    load_settings(force_dotenv=True)
    catalog_payload = json.loads(
        list_slack_slash_commands(repo_path=args.slack_repo_path)
    )
    command_catalog = list(catalog_payload.get("command_details") or [])
    if not chief_slack_command_resolution_is_applicable(args.input):
        print(
            json.dumps(
                {
                    "output": {
                        "status": "no_match",
                        "command_text": "",
                        "rationale": (
                            "Provider-owned action continues through Chief of Staff "
                            "interpretation and typed tools."
                        ),
                        "confidence": "high",
                    },
                    "usage": {"available": True, "requests": 0, "total_tokens": 0},
                    "cost": {"available": True, "estimated_cost_usd": 0.0},
                    "model": "provider-action-admission-gate",
                    "provider": "local",
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    deterministic_output = resolve_high_confidence_chief_slack_command(
        args.input,
        command_catalog,
    )
    if deterministic_output is not None:
        print(
            json.dumps(
                {
                    "output": deterministic_output.model_dump(mode="json"),
                    "usage": {"available": True, "requests": 0, "total_tokens": 0},
                    "cost": {"available": True, "estimated_cost_usd": 0.0},
                    "model": "deterministic-manifest-match",
                    "provider": "local",
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    result = run_chief_slack_command_resolver(
        args.input,
        command_catalog,
        live=True,
        model=args.model,
    )
    validated_output = validate_chief_slack_command_resolution(
        result.output,
        command_catalog,
    )
    payload = {
        "output": validated_output.model_dump(mode="json"),
        "usage": result.usage,
        "cost": result.cost,
        "model": result.model_name,
        "provider": result.model_provider,
    }
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
