#!/usr/bin/env python3
"""Validate current-quarter finance context before one bounded Chief BD decision."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from keystone_agents.config import load_settings
from keystone_agents.finance_bd_priority_workflow import (
    BusinessDevelopmentOption,
    collect_current_quarter_finance_packet,
    execute_finance_bd_priority_decision,
    finance_bd_priority_privacy_preview,
)

DEFAULT_OPTIONS = (
    BusinessDevelopmentOption(
        option_id="option-alpha",
        title="Option Alpha",
        description="A low fixed-cost pilot with a fast validation cycle.",
    ),
    BusinessDevelopmentOption(
        option_id="option-beta",
        title="Option Beta",
        description="A higher fixed-cost commitment with a longer validation cycle.",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-airtable-reads", action="store_true")
    parser.add_argument("--live-sdk", action="store_true")
    parser.add_argument("--preview-privacy-minimized-context", action="store_true")
    parser.add_argument("--approve-privacy-minimized-context", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/finance-bd-priority-validation.json"),
    )
    args = parser.parse_args()
    if args.live_sdk and args.preview_privacy_minimized_context:
        parser.error("--live-sdk and --preview-privacy-minimized-context are mutually exclusive")
    load_settings(force_dotenv=True)
    if not args.live_airtable_reads:
        raise SystemExit("This proof requires --live-airtable-reads.")
    os.environ["KEYSTONE_AIRTABLE_LIVE_READS"] = "true"
    packet = collect_current_quarter_finance_packet(options=DEFAULT_OPTIONS, live=True)
    if args.preview_privacy_minimized_context:
        result = finance_bd_priority_privacy_preview(packet)
    else:
        result = execute_finance_bd_priority_decision(
            packet,
            live_sdk=bool(args.live_sdk),
            approved_privacy_minimized_context=bool(
                args.approve_privacy_minimized_context
            ),
            approved_private_context=False,
        )
    rendered = json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(rendered, end="")
    return (
        0
        if result["status"]
        in {"validated_offline", "privacy_minimized_preview", "passed"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
