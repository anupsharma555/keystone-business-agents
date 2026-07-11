#!/usr/bin/env python3
"""Run a persisted Business Research result through a reversible Google Doc lifecycle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from keystone_agents.config import (
    load_settings,
    parse_bool,
    require_cli_live_confirmation,
)
from keystone_agents.research_doc_lifecycle import execute_research_doc_lifecycle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--folder-path", default="KNIOps")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-google-workspace", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/research-doc-lifecycle.json"),
    )
    args = parser.parse_args()
    load_settings(force_dotenv=True)
    if args.live_google_workspace:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-google-workspace",
            live_action="creating and cleaning up one marked source-backed Google Doc",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-google-workspace.")
    live = bool(args.live_google_workspace and not args.dry_run)
    if live and not parse_bool(os.getenv("KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE")):
        raise SystemExit(
            "Live research Doc lifecycle is disabled. Set "
            "KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true for the approved process."
        )
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    suffix = uuid4().hex[:10]
    approval = args.approval_reference.strip() or f"anu-174-research-doc-{suffix}"
    result = execute_research_doc_lifecycle(
        payload,
        suffix=suffix,
        folder_path=args.folder_path,
        approval_reference=approval,
        live=live,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(rendered, end="")
    return 0 if result["status"] in {"passed", "dry-run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
