#!/usr/bin/env python3
"""Run the bounded latest-research-note to disposable Google Doc proof."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from keystone_agents.config import load_settings, parse_bool, require_cli_live_confirmation
from keystone_agents.research_note_brief_workflow import (
    execute_research_note_brief_workflow,
    research_note_brief_privacy_preview,
    resolve_latest_research_note,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-google-workspace-reads", action="store_true")
    parser.add_argument("--live-sdk", action="store_true")
    parser.add_argument("--preview-privacy-minimized-context", action="store_true")
    parser.add_argument("--approve-privacy-minimized-context", action="store_true")
    parser.add_argument("--live-google-workspace-writes", action="store_true")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/workspace-research-brief-lifecycle.json"),
    )
    args = parser.parse_args()
    if args.live_sdk and args.preview_privacy_minimized_context:
        parser.error("--live-sdk and --preview-privacy-minimized-context are mutually exclusive")
    if args.live_google_workspace_writes and args.preview_privacy_minimized_context:
        parser.error(
            "--live-google-workspace-writes and --preview-privacy-minimized-context "
            "are mutually exclusive"
        )
    load_settings(force_dotenv=True)
    if not args.live_google_workspace_reads:
        raise SystemExit("This proof requires --live-google-workspace-reads.")
    os.environ["KEYSTONE_GOOGLE_WORKSPACE_LIVE_READS"] = "true"
    if args.live_google_workspace_writes:
        if not args.live_sdk:
            raise SystemExit("Workspace writes require a completed --live-sdk synthesis.")
        require_cli_live_confirmation(
            dry_run=False,
            live_flag=True,
            flag_name="--live-google-workspace-writes",
            live_action="creating, modifying, and trashing one marked KBA_TEST_DOC",
        )
        if not parse_bool(os.getenv("KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE")):
            raise SystemExit(
                "Set KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true for this "
                "approved disposable lifecycle."
            )
        if not args.approval_reference.strip():
            raise SystemExit("Workspace writes require --approval-reference.")
        os.environ["GOOGLE_WORKSPACE_WRITES_ENABLED"] = "true"

    note = resolve_latest_research_note(live=True)
    if args.preview_privacy_minimized_context:
        result = research_note_brief_privacy_preview(note)
    else:
        result = execute_research_note_brief_workflow(
            note=note,
            suffix=uuid4().hex[:10],
            approval_reference=args.approval_reference.strip()
            or "anu-174-l174-16-preview",
            live_sdk=bool(args.live_sdk),
            live_workspace_writes=bool(args.live_google_workspace_writes),
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
        in {
            "validated_offline",
            "privacy_minimized_preview",
            "synthesis_passed_write_pending",
            "passed",
        }
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
