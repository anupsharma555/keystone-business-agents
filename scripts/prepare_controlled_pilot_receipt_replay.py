#!/usr/bin/env python3
"""Prepare zero-model ANU-61 Slack replay packets from saved specialist receipts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.pilot_receipt_replay import build_pilot_receipt_replays


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opportunity-evidence",
        type=Path,
        default=Path("artifacts/test-pack/controlled-pilot-opportunity-compact-revalidated.json"),
    )
    parser.add_argument(
        "--research-model",
        type=Path,
        default=Path("artifacts/test-pack/controlled-pilot-research-plan-model-v2.json"),
    )
    parser.add_argument(
        "--research-plan",
        type=Path,
        default=Path("artifacts/test-pack/controlled-pilot-research-plan-v2.json"),
    )
    parser.add_argument(
        "--weekly-evidence",
        type=Path,
        default=Path("artifacts/test-pack/weekly-chief-packet-live.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/controlled-pilot-receipt-replay.json"),
    )
    args = parser.parse_args()
    replays = build_pilot_receipt_replays(
        opportunity_evidence_path=args.opportunity_evidence,
        research_model_path=args.research_model,
        research_plan_path=args.research_plan,
        weekly_evidence_path=args.weekly_evidence,
    )
    payload = {
        "schema": "keystone.controlled_pilot.receipt_replay.v1",
        "status": "ready_for_authorized_slack_transport",
        "replay_count": len(replays),
        "replay_openai_requests": 0,
        "repeated_model_synthesis": False,
        "provider_writes": 0,
        "slack_posts": 0,
        "pilot_observations_claimed": 0,
        "replays": [replay.model_dump(mode="json") for replay in replays],
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
