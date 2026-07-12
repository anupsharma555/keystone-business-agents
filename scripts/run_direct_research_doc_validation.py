#!/usr/bin/env python3
"""Run one official-URL Business Research synthesis through reversible Google Docs."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_focused_brief_agent,
    focused_brief_input_from_profile,
    research_account_from_search_results,
)
from keystone_agents.config import load_settings, parse_bool
from keystone_agents.research_doc_lifecycle import execute_research_doc_lifecycle
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.company_profile import CompanyResearchFocusedBrief
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionResult,
    extract_website_content,
)

EXPECTED_MODEL = "gpt-5.4-mini"
MAX_REQUESTS = 1
MAX_BUDGET_USD = 0.05
DEFAULT_COMPANY = "NeuroFlow"
DEFAULT_URL = "https://www.neuroflow.com/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", default=DEFAULT_COMPANY)
    parser.add_argument("--company-url", default=DEFAULT_URL)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--folder-path", default="KNIOps")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Return a reviewed Google Doc write plan without creating or modifying a Doc.",
    )
    parser.add_argument(
        "--research-output",
        type=Path,
        default=Path("artifacts/test-pack/neuroflow-direct-research-live.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/neuroflow-direct-research-doc-live.json"),
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.model != EXPECTED_MODEL:
        raise SystemExit(f"Direct research validation requires model={EXPECTED_MODEL}.")
    if args.max_openai_requests != MAX_REQUESTS:
        raise SystemExit("Direct research validation requires max_openai_requests=1.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Direct research validation requires a budget at or below $0.05.")
    if not args.company_url.startswith("https://"):
        raise SystemExit("Direct research validation requires one explicit HTTPS company URL.")


def _require_workspace_write_gates() -> None:
    required = (
        "GOOGLE_WORKSPACE_WRITES_ENABLED",
        "KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE",
    )
    missing = [name for name in required if not parse_bool(os.getenv(name))]
    if missing:
        raise SystemExit(
            "Direct research validation requires approved Workspace gates before the "
            f"model call: {', '.join(missing)}."
        )


def _profile_from_extraction(
    company: str,
    company_url: str,
    extraction: WebsiteExtractionResult,
):
    if extraction.status != "success" or not extraction.text_or_markdown.strip():
        raise RuntimeError("Official company URL extraction returned no usable text.")
    if not extraction.claims:
        raise RuntimeError("Official company URL extraction returned no claim candidates.")
    return research_account_from_search_results(
        company_name=company,
        company_url=company_url,
        website_inputs=[
            {
                "source_id": "direct:official-company-page",
                "url": extraction.url,
                "title": extraction.title or f"{company} official website",
                "source_type": "company_site",
                "text_or_markdown": extraction.text_or_markdown,
                "claims": extraction.claims,
                "confidence": 0.85,
            }
        ],
    )


def _run_model(profile: Any, *, model: str) -> Any:
    typed_input = focused_brief_input_from_profile(
        profile,
        brief_goal=(
            "Research the selected company for current KNI relevance, then prepare concise "
            "source-backed Google Doc content and an exact reviewed write plan without "
            "creating or modifying a document. Cover product, customers, source-visible "
            "traction signals, leadership only if supported, and why the company may matter "
            "to Keystone. Use only the supplied official page."
        ),
    )
    return run_typed_sdk_agent(
        agent=build_business_research_analyst_focused_brief_agent(
            model=model,
            request_text=typed_input.brief_goal,
            attach_tools=False,
        ),
        typed_input=typed_input,
        output_type=CompanyResearchFocusedBrief,
        live=True,
        workflow_name="Keystone direct official URL research to Google Doc validation",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        max_turns=1,
    )


def _normalize_brief_source_identity(
    brief: CompanyResearchFocusedBrief,
    profile: Any,
) -> CompanyResearchFocusedBrief:
    canonical_by_url = {source.url: source.source_id for source in profile.sources}
    source_id_map = {
        source.source_id: canonical_by_url[source.url]
        for source in brief.sources
        if source.url in canonical_by_url
    }
    if not source_id_map:
        return brief
    payload = brief.model_dump(mode="json")
    for source in payload["sources"]:
        source["source_id"] = source_id_map.get(source["source_id"], source["source_id"])
    for fact in payload["facts"]:
        fact["source_ids"] = [source_id_map.get(item, item) for item in fact["source_ids"]]
    for contact in payload["contact_candidates"]:
        contact["source_ids"] = [
            source_id_map.get(item, item) for item in contact["source_ids"]
        ]
    payload["source_ids_used"] = [
        source_id_map.get(item, item) for item in payload["source_ids_used"]
    ]
    return CompanyResearchFocusedBrief.model_validate(payload)


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def execute_validation(
    *,
    company: str,
    company_url: str,
    model: str,
    budget_usd: float,
    folder_path: str,
    research_output: Path,
    extractor: Callable[..., WebsiteExtractionResult] = extract_website_content,
    model_runner: Callable[..., Any] = _run_model,
    doc_runner: Callable[..., dict[str, Any]] = execute_research_doc_lifecycle,
    plan_only: bool = False,
) -> dict[str, Any]:
    extraction = extractor(company_url, company_name=company, live=True)
    profile = _profile_from_extraction(company, company_url, extraction)
    model_result = model_runner(profile, model=model)
    brief = _normalize_brief_source_identity(
        CompanyResearchFocusedBrief.model_validate(model_result.final_output),
        profile,
    )
    usage = dict(model_result.usage or {})
    cost = dict(model_result.cost or {})
    retries = int((model_result.request_cache or {}).get("rate_limit_retries") or 0)
    source_urls = {source.url for source in brief.sources}
    official_source_preserved = extraction.url in source_urls
    requests = int(usage.get("requests") or 0)
    estimated_usd = float(cost.get("estimated_usd") or 0.0)
    research_passed = bool(
        requests == MAX_REQUESTS
        and estimated_usd <= budget_usd
        and retries == 0
        and official_source_preserved
        and brief.company_name == company
        and brief.product.strip()
        and brief.customers.strip()
        and brief.why_it_matters.strip()
    )
    research_payload = {
        "status": "pass" if research_passed else "partial",
        "agent_name": "business_research_analyst",
        "live_sdk": True,
        "output_type": "CompanyResearchFocusedBrief",
        "output": brief.model_dump(mode="json"),
        "retrieval": {
            "mode": "direct_official_url",
            "live_search": False,
            "provider": extraction.provider,
            "url": extraction.url,
            "claim_count": len(extraction.claims),
        },
        "usage": usage,
        "cost": cost,
        "request_cache": dict(model_result.request_cache or {}),
        "safety": {
            "tools_attached": False,
            "live_search": False,
            "provider_writes": False,
            "send_enabled": False,
        },
    }
    _write_atomic(research_output, research_payload)
    if not research_passed:
        return {
            "status": "partial",
            "failure": "Direct Business Research acceptance checks failed.",
            "research": {
                "requests": requests,
                "estimated_usd": estimated_usd,
                "official_source_preserved": official_source_preserved,
            },
            "doc": {"executed": False},
        }

    if plan_only:
        return {
            "status": "pass",
            "failure": "",
            "scenario": "direct_official_company_research_to_reviewed_doc_plan",
            "model": model,
            "research": {
                "requests": requests,
                "estimated_usd": estimated_usd,
                "rate_limit_retries": retries,
                "official_source_preserved": official_source_preserved,
                "extraction_provider": extraction.provider,
                "claim_count": len(extraction.claims),
                "live_search": False,
                "tools_attached": False,
            },
            "doc": {
                "executed": False,
                "plan_status": "reviewed_no_write",
                "folder_path": folder_path,
                "title": f"{company} — KNI relevance brief",
                "sections": [
                    "Product",
                    "Customers",
                    "Traction signals",
                    "Leadership evidence",
                    "Why it matters to Keystone",
                    "Sources and unknowns",
                ],
                "source_urls": sorted(source_urls),
                "approval_required_before_create": True,
            },
            "safety": {
                "send_enabled": False,
                "search_requests": 0,
                "provider_writes": 0,
                "source_url_count": len(source_urls),
            },
        }

    suffix = uuid4().hex[:10]
    doc_result = doc_runner(
        research_payload,
        suffix=suffix,
        folder_path=folder_path,
        approval_reference=f"operator-command:l174-04:{suffix}",
        live=True,
    )
    passed = bool(doc_result.get("status") == "passed")
    return {
        "status": "pass" if passed else "partial",
        "failure": str(doc_result.get("failure") or ""),
        "scenario": "direct_official_company_research_to_reversible_google_doc",
        "model": model,
        "research": {
            "requests": requests,
            "estimated_usd": estimated_usd,
            "rate_limit_retries": retries,
            "official_source_preserved": official_source_preserved,
            "extraction_provider": extraction.provider,
            "claim_count": len(extraction.claims),
            "live_search": False,
            "tools_attached": False,
        },
        "doc": doc_result,
        "safety": {
            "send_enabled": False,
            "search_requests": 0,
            "source_url_count": len(source_urls),
        },
    }


def main() -> int:
    args = build_parser().parse_args()
    _validate_args(args)
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    if not args.plan_only:
        _require_workspace_write_gates()
    payload = execute_validation(
        company=args.company,
        company_url=args.company_url,
        model=args.model,
        budget_usd=args.budget_usd,
        folder_path=args.folder_path,
        research_output=args.research_output,
        plan_only=args.plan_only,
    )
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
