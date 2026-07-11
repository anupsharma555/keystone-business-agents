#!/usr/bin/env python3
"""Run one bounded natural public-contact discovery through KBA provider tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.tools.public_contact_tool import discover_public_company_contacts_impl

DEFAULT_REQUEST = (
    "Find a public business-development contact for NeuroFlow, but do not draft outreach."
)


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", default=DEFAULT_REQUEST)
    parser.add_argument("--company", default="NeuroFlow")
    parser.add_argument("--about-url", default="https://www.neuroflow.com/about-us/")
    parser.add_argument("--contact-url", default="https://www.neuroflow.com/contact-us/")
    parser.add_argument("--search-provider", default="exa")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/neuroflow-public-contact-live.json"),
    )
    args = parser.parse_args()
    plan = infer_manual_request_plan(args.request, requested_agent="orchestrator")
    result = discover_public_company_contacts_impl(
        args.company,
        args.about_url,
        args.contact_url,
        live=args.live,
        search_provider=args.search_provider,
        max_candidates=1,
    )
    plan_ready = bool(
        plan.target_agent == "business_research_analyst"
        and plan.task_objective == "contact_discovery"
        and plan.expected_artifact_type == "contact_candidates"
        and result.no_draft
        and not result.send_enabled
        and not result.inferred_personal_email
        and not result.raw_source_content_included
        and result.search_requests <= 1
        and (not args.live or result.search_result_count > 0)
    )
    passed = bool(result.status == "pass" and result.candidates and plan_ready)
    dry_run_ready = bool(result.status == "dry-run" and plan_ready)
    payload = {
        "status": "pass" if passed else "dry-run" if dry_run_ready else "partial",
        "scenario": "natural_public_company_contact_discovery",
        "request": args.request,
        "plan": {
            "target_agent": plan.target_agent,
            "task_objective": plan.task_objective,
            "expected_artifact_type": plan.expected_artifact_type,
            "primary_target": plan.primary_target,
            "side_effect_policy": plan.side_effect_policy,
        },
        "result": result.model_dump(mode="json"),
        "openai_requests": 0,
    }
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed or dry_run_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
