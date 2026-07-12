#!/usr/bin/env python3
"""Validate or run the bounded Chief Prior Week Packet workflow."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.weekly_ops_runner import (
    run_weekly_ops_packet_synthesis,
    weekly_ops_privacy_preview,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Chief Prior Week Packet: validate locally by default or run one "
            "explicitly approved, cost-bounded Chief synthesis request."
        )
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--live-sdk", action="store_true")
    parser.add_argument("--max-openai-requests", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=Decimal, default=Decimal("0.05"))
    parser.add_argument("--approve-privacy-minimized-context", action="store_true")
    parser.add_argument(
        "--preview-privacy-minimized-context",
        action="store_true",
        help="Print the exact identity-free bundle that live mode would transmit.",
    )
    parser.add_argument(
        "--output",
        help="Atomically save the preview or validated live result as JSON.",
    )
    args = parser.parse_args()
    if args.preview_privacy_minimized_context and args.live_sdk:
        parser.error("--preview-privacy-minimized-context cannot be combined with --live-sdk")

    load_dotenv(Path(".env"))
    payload = WeeklyOpsAssemblyInput.model_validate(
        json.loads(Path(args.input).read_text(encoding="utf-8"))
    )
    if args.preview_privacy_minimized_context:
        result = weekly_ops_privacy_preview(payload)
    else:
        result = run_weekly_ops_packet_synthesis(
            payload,
            live_sdk=bool(args.live_sdk),
            max_openai_requests=args.max_openai_requests,
            max_cost_usd=args.max_cost_usd,
            approved_privacy_minimized_context=bool(
                args.approve_privacy_minimized_context
            ),
            approved_private_context=False,
        )
    rendered = json.dumps(result, ensure_ascii=True, sort_keys=True, default=str)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
        temporary_path.write_text(f"{rendered}\n", encoding="utf-8")
        temporary_path.replace(output_path)
    print(rendered)


if __name__ == "__main__":
    main()
