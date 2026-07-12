#!/usr/bin/env python3
"""Validate or run one bounded Chief summary over local KNI capability evidence."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from keystone_agents.config import load_settings
from keystone_agents.local_kni_summary_runner import (
    local_kni_summary_privacy_preview,
    run_local_kni_capability_summary,
)

DEFAULT_QUERY = (
    "Search local KNI documents for the latest client proposal or capability statement "
    "and summarize the key service areas."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-sdk", action="store_true")
    parser.add_argument("--preview-privacy-minimized-context", action="store_true")
    parser.add_argument("--max-openai-requests", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=Decimal, default=Decimal("0.05"))
    parser.add_argument("--approve-privacy-minimized-context", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
    )
    args = parser.parse_args()
    load_settings(force_dotenv=True)
    if args.live_sdk and args.preview_privacy_minimized_context:
        parser.error("--live-sdk and --preview-privacy-minimized-context are mutually exclusive")
    if args.preview_privacy_minimized_context:
        result = local_kni_summary_privacy_preview(DEFAULT_QUERY)
    else:
        result = run_local_kni_capability_summary(
            DEFAULT_QUERY,
            live_sdk=bool(args.live_sdk),
            max_openai_requests=args.max_openai_requests,
            max_cost_usd=args.max_cost_usd,
            approved_privacy_minimized_context=bool(
                args.approve_privacy_minimized_context
            ),
        )
    rendered = json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    output_path = args.output
    if args.live_sdk and output_path is None:
        output_path = Path("artifacts/test-pack/local-kni-capability-summary-live.json")
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(output_path)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
