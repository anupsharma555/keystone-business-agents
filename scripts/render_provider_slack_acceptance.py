#!/usr/bin/env python3
"""Render the zero-model provider Slack acceptance manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.provider_slack_acceptance import (
    build_provider_slack_acceptance_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    report = build_provider_slack_acceptance_report()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.json_output.with_suffix(args.json_output.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(args.json_output)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
