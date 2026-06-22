from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from keystone_agents.slack_action_contract import business_agent_result_display_text

DEFAULT_EXAMPLES = Path("contracts/keystone_slack_result_rendering_examples.v1.json")
EXPECTED_SCHEMA = "keystone.slack_result_rendering_examples.v1"


def _load_examples(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing examples file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def validate_examples(path: Path = DEFAULT_EXAMPLES) -> list[str]:
    data = _load_examples(path)
    failures: list[str] = []

    if data.get("schema") != EXPECTED_SCHEMA:
        failures.append(
            f"schema: expected {EXPECTED_SCHEMA!r}, got {data.get('schema')!r}"
        )

    examples = data.get("examples")
    if not isinstance(examples, list) or not examples:
        failures.append("examples: expected a non-empty list")
        return failures

    for index, example in enumerate(examples, start=1):
        if not isinstance(example, dict):
            failures.append(f"example {index}: expected object")
            continue
        name = str(example.get("name") or f"example {index}")
        payload = example.get("payload")
        expected = example.get("expected_display_text")
        if not isinstance(payload, dict):
            failures.append(f"{name}: payload must be an object")
            continue
        if not isinstance(expected, str) or not expected.strip():
            failures.append(f"{name}: expected_display_text must be non-empty text")
            continue
        actual = business_agent_result_display_text(payload)
        if actual != expected:
            failures.append(
                f"{name}: display text mismatch "
                f"(expected {len(expected)} chars, got {len(actual)} chars)"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Slack result rendering examples against the KBA contract helper."
    )
    parser.add_argument(
        "examples",
        nargs="?",
        type=Path,
        default=DEFAULT_EXAMPLES,
        help=f"Examples JSON file to validate (default: {DEFAULT_EXAMPLES})",
    )
    args = parser.parse_args(argv)

    failures = validate_examples(args.examples)
    if failures:
        print("Slack result rendering examples failed validation:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    count = len(_load_examples(args.examples)["examples"])
    print(f"Validated {count} Slack result rendering examples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
