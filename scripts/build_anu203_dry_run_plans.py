#!/usr/bin/env python3
"""Build the first disabled ANU-203 combined and scheduled dry-run plans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.multi_agent_workflow_templates import (
    WorkflowRunMode,
    build_workflow_dry_run_plan,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/anu203-first-workflow-dry-run-plans.json"),
    )
    args = parser.parse_args(argv)
    project_id = "project-kni-synthetic-001"
    source_refs = (
        "fixture:gmail-thread:synthetic-partner",
        "fixture:slack-thread:project-kni-synthetic-001",
    )
    plans = [
        build_workflow_dry_run_plan(
            "gmail_thread_research_opportunity_outreach",
            run_mode=WorkflowRunMode.COMBINED,
            project_id=project_id,
            source_refs=source_refs,
        ),
        build_workflow_dry_run_plan(
            "key_email_response_queue",
            run_mode=WorkflowRunMode.SCHEDULED,
            project_id=project_id,
            source_refs=source_refs,
        ),
    ]
    payload = {
        "schema": "keystone.anu203.dry_run_plan_set.v1",
        "status": "dry_run_ready",
        "dependency_gate": "ANU-61 must pass before activation",
        "plans": [plan.model_dump(mode="json", by_alias=True) for plan in plans],
        "totals": {
            "openai_requests": 0,
            "provider_reads": 0,
            "provider_writes": 0,
            "external_posts": 0,
        },
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
