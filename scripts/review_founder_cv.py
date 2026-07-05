"""Create a local review packet from a founder CV document."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.founder_profile import build_founder_cv_review_packet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review a local founder CV for profile extraction."
    )
    parser.add_argument("--cv", default="documents/CV_Operator_2026.docx")
    parser.add_argument("--output", default=None, help="Optional JSON output path.")
    parser.add_argument(
        "--include-extracted-text",
        action="store_true",
        help="Include raw extracted CV text in the local output. Use only for private review.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    packet = build_founder_cv_review_packet(
        args.cv,
        include_extracted_text=args.include_extracted_text,
    )
    payload = json.dumps(packet, ensure_ascii=True, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
