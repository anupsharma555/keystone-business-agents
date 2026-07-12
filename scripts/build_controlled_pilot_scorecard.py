#!/usr/bin/env python3
"""Build an ANU-61/ANU-125 scorecard from sanitized saved observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.controlled_pilot_scorecard import (
    build_controlled_pilot_scorecard,
    controlled_pilot_observation_from_dict,
    differentiation_observation_from_dict,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("Scorecard input must be a JSON object.")
    scorecard = build_controlled_pilot_scorecard(
        trusted_runtime_rows=dict(payload.get("trusted_runtime_rows") or {}),
        observations=[
            controlled_pilot_observation_from_dict(item)
            for item in payload.get("observations") or []
        ],
        baseline_observations=[
            differentiation_observation_from_dict(item)
            for item in payload.get("baseline_observations") or []
        ],
    )
    rendered = json.dumps(scorecard, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(args.output)
    print(rendered, end="")
    return 0 if scorecard["pilot_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
