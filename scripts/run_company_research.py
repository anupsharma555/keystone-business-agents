"""Run company research."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time as time_module
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from keystone_agents.agent_decision_contracts import (
    business_research_comparison_decision_contract,
    business_research_context_decision_contract,
)
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
    build_business_research_analyst_comparison_agent,
    build_business_research_analyst_focused_brief_agent,
    build_company_research_queries,
    compare_company_profiles_for_decision,
    comparison_context_from_result,
    comparison_input_from_result,
    focused_brief_input_from_profile,
    research_account_from_search_results,
    run_business_research_analyst_comparison_sdk,
    run_business_research_analyst_focused_brief_sdk,
    run_business_research_analyst_sdk,
)
from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.agents.web_query_planner import resolve_web_query_plan
from keystone_agents.cli_orchestrator_review import (
    add_orchestrator_review_arguments,
    build_cli_orchestrator_review,
)
from keystone_agents.cli_sdk import (
    SDKRunConfigFactory,
    add_sdk_run_arguments,
    jsonable,
    resolve_sdk_execution,
    sdk_agent_description,
    sdk_execution_requested,
    sdk_synthesis_payload,
)
from keystone_agents.company_research import (
    company_profile_markdown,
    company_research_comparison_markdown,
    parse_company_research_comparison_criteria,
    research_company_fixture,
    synthesize_company_profile_from_source_bundle,
)
from keystone_agents.config import (
    cli_default_dry_run,
    cli_default_live_research,
    cli_default_live_sdk,
    load_settings,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.founder_profile import (
    founder_profile_audit_payload,
    founder_search_context,
    load_founder_fit_profile,
)
from keystone_agents.instruction_following import (
    output_constraints_from_plan,
    validate_output_constraints,
)
from keystone_agents.live_retrieval import (
    company_research_request_text as _live_company_research_request_text,
)
from keystone_agents.live_retrieval import (
    company_source_matches_official_url,
    infer_official_company_url,
    retrieval_diagnostics_from_metadata,
    retrieve_company_profile_live,
)
from keystone_agents.live_retrieval import (
    search_provider_label as _live_search_provider_label,
)
from keystone_agents.memory import retrieval_tool_performance_memory_item
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import (
    BusinessResearchComparisonSDKInput,
    BusinessResearchFocusedBriefSDKInput,
    BusinessResearchSDKInput,
)
from keystone_agents.orchestrator.preflight_context import (
    apply_orchestrator_preflight_to_args,
    attach_orchestrator_preflight_payload,
    orchestrator_preflight_context_text,
)
from keystone_agents.reporting import (
    render_company_comparison_report,
    render_company_focused_brief,
    render_company_profile_report,
    render_orchestrator_output_review,
)
from keystone_agents.run import SDKSynthesisOutcome, run_retrieved_sdk_synthesis
from keystone_agents.schemas.company_profile import (
    CompanyProfile,
    CompanyResearchComparison,
    CompanyResearchFocusedBrief,
    SourceRecord,
)
from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.tools.serper_tool import (
    SearchProviderConfigurationError,
    SearchProviderError,
    SearchProviderName,
    build_search_provider,
)
from keystone_agents.tools.storage_tool import StorageTool
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    build_selected_url_source_bundle,
)

SDK_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
CR1_IMPROVEMENT_CASE_ID = "br-1"
CR1_IMPROVEMENT_PROMPT = (
    "Prepare a concise research brief on the company for possible partnership "
    "or advisory relevance to Keystone. Focus on product, customers, traction "
    "signals, leadership, and why it may matter."
)
CR1_ACCEPTANCE_CRITERIA = (
    "Use clear sections.",
    "Separate facts from inference.",
    "Flag unknowns.",
    "Cite sources for factual claims.",
)


def build_parser() -> argparse.ArgumentParser:
    dry_run_default = cli_default_dry_run()
    live_search_default = cli_default_live_research()
    parser = argparse.ArgumentParser(description="Run the business research analyst agent.")
    parser.add_argument("--company", default=None, help="Company name.")
    parser.add_argument(
        "--request-text",
        default=None,
        help="Original manual operator request. Used for structured pre-execution planning.",
    )
    parser.add_argument(
        "--inline-source-context",
        default=None,
        help=(
            "Bounded operator-provided evidence for direct, no-search SDK synthesis. "
            "The text is treated as one user-provided source and is never searched."
        ),
    )
    parser.add_argument(
        "--selected-url-extraction",
        action="store_true",
        help=(
            "Read only --company-url through the bounded selected-page extractor. "
            "Requires --no-dry-run and KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true."
        ),
    )
    parser.add_argument(
        "--live-manual-plan",
        action="store_true",
        help=(
            "Use the SDK manual-request planner to extract target and objective before "
            "company research. Falls back to local planning if unavailable."
        ),
    )
    parser.add_argument("--company-url", default=None, help="Optional company website URL.")
    parser.add_argument("--lead-name", default=None, help="Optional lead/contact name.")
    parser.add_argument("--linkedin-url", default=None, help="Optional LinkedIn or profile URL.")
    parser.add_argument("--fixture", default=None, help="Optional fixture JSON path.")
    parser.add_argument(
        "--compare-company",
        default=None,
        help="Optional second company name for side-by-side comparison mode.",
    )
    parser.add_argument(
        "--compare-company-url",
        default=None,
        help="Optional website URL for the comparison company.",
    )
    parser.add_argument(
        "--compare-lead-name",
        default=None,
        help="Optional lead/contact name for the comparison company.",
    )
    parser.add_argument(
        "--compare-linkedin-url",
        default=None,
        help="Optional LinkedIn or profile URL for the comparison company.",
    )
    parser.add_argument(
        "--compare-fixture",
        default=None,
        help="Optional fixture JSON path for the comparison company.",
    )
    parser.add_argument(
        "--decision-criteria",
        default=None,
        help=(
            "Optional comma-separated comparison criteria. Supported values: "
            "consulting_fit, evidence_strength, evidence_generation_need, "
            "outside_consulting_likelihood, clinical_relevance."
        ),
    )
    parser.add_argument(
        "--output-format",
        default=None,
        help=(
            "Optional comma-separated output sections such as "
            "'summary, evidence, concerns, next step'."
        ),
    )
    parser.add_argument(
        "--strict-format",
        action="store_true",
        help="Render markdown using exactly the requested --output-format sections.",
    )
    parser.add_argument(
        "--live-search",
        action=argparse.BooleanOptionalAction,
        default=live_search_default,
        help=(
            "Use live search through the configured provider. Supported in both "
            "standard company-profile mode and SDK BR-1 focused-brief mode. "
            "Requires --no-dry-run."
        ),
    )
    parser.add_argument(
        "--live-search-plan",
        action="store_true",
        help=(
            "Use a credential-gated LLM query-planning pass before live company "
            "retrieval. Falls back to deterministic queries if unavailable."
        ),
    )
    parser.add_argument(
        "--search-provider",
        choices=[provider.value for provider in SearchProviderName],
        default=None,
        help="Live search provider. Defaults to SEARCH_PROVIDER; dry-run is not valid live.",
    )
    parser.add_argument(
        "--founder-fit-profile",
        default=None,
        help=(
            "Optional approved founder-fit JSON profile for company fit and "
            "advisory relevance assessment."
        ),
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Maximum live search results per query.",
    )
    parser.add_argument(
        "--quick-retrieval",
        action="store_true",
        help=(
            "Use a narrow source-visible discovery profile for explicitly brief, "
            "low-latency company identification asks."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=dry_run_default,
        help="Use deterministic fixture mode. Defaults to KEYSTONE_DRY_RUN or true.",
    )
    parser.add_argument("--sdk", action="store_true", help="Also construct the SDK agent.")
    add_sdk_run_arguments(parser)
    parser.add_argument(
        "--focused-brief",
        action="store_true",
        help=(
            "Use Business Research Analyst SDK synthesis to generate the BR-1 focused brief. "
            "Requires --run-sdk or --live-sdk."
        ),
    )
    parser.add_argument(
        "--improvement-case",
        choices=[CR1_IMPROVEMENT_CASE_ID],
        default=None,
        help=(
            "Run a documented agent-improvement acceptance case through SDK synthesis. "
            "Currently supports br-1 from docs/AGENT_IMPROVEMENT_TEST_PACK.md. "
            "Use --live-search --no-dry-run to retrieve live sources before synthesis."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    parser.add_argument(
        "--result-output",
        type=Path,
        default=None,
        help=(
            "Atomically persist the complete bounded result before console rendering. "
            "Recommended for live SDK validation so output truncation cannot erase evidence."
        ),
    )
    parser.add_argument(
        "--retrieval-hint-json",
        default=None,
        help=(
            "Optional explicit retrieval-hint JSON from the orchestrator or control plane. "
            "Used as a recommendation only; deterministic quality gates still decide escalation."
        ),
    )
    add_orchestrator_review_arguments(parser)
    parser.add_argument(
        "--save", action="store_true", help="Save profile and audit records to SQLite."
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    return parser


def _apply_manual_request_plan(args: argparse.Namespace) -> argparse.Namespace:
    args = apply_orchestrator_preflight_to_args(args)
    if getattr(args, "manual_request_plan", None):
        plan = args.manual_request_plan
        if isinstance(plan, dict) and plan.get("target_agent") == "business_research_analyst":
            comparison_targets = _manual_plan_comparison_targets(plan)
            if comparison_targets is not None:
                args.company, args.compare_company = comparison_targets
            else:
                primary_target = str(plan.get("primary_target") or "").strip()
                if primary_target:
                    args.company = primary_target
        return args
    request_text = str(args.request_text or "").strip()
    args.manual_request_plan = None
    if not request_text:
        return args
    if args.live_search and sdk_execution_requested(args) and not args.live_manual_plan:
        # The owning specialist receives the full request and interprets the target,
        # query, and tool sequence. A standalone planner is comparison-only here.
        return args
    if args.live_manual_plan:
        load_settings(force_dotenv=True)
    plan = resolve_manual_request_plan(
        request_text,
        requested_agent="business_research_analyst",
        live=bool(args.live_manual_plan),
    )
    args.manual_request_plan = plan.model_dump(mode="json")
    if plan.target_agent == "business_research_analyst":
        comparison_targets = _manual_plan_comparison_targets(args.manual_request_plan)
        if comparison_targets is not None:
            args.company, args.compare_company = comparison_targets
        elif plan.primary_target:
            args.company = plan.primary_target
    if plan.objective and not args.output_format:
        args.output_format = None
    return args


_LOCAL_PERSISTENCE_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|without)\b[^.;\n]{0,100}"
    r"\b(?:save|persist|store|write)\b",
    re.I,
)


def _apply_local_persistence_boundary(args: argparse.Namespace) -> argparse.Namespace:
    """Keep bridge-added ``--save`` below the natural-request permission ceiling."""

    save_requested = bool(args.save)
    plan = getattr(args, "manual_request_plan", None)
    constraints = plan.get("constraints") if isinstance(plan, dict) else None
    constraint_text = " ".join(
        str(item or "").strip()
        for item in (constraints if isinstance(constraints, list) else [])
        if str(item or "").strip()
    )
    ask_shape = plan.get("ask_shape") if isinstance(plan, dict) else None
    permission_state = (
        str(ask_shape.get("permission_state") or "").strip()
        if isinstance(ask_shape, dict)
        else ""
    )
    persistence_forbidden = bool(
        permission_state == "read_only"
        and _LOCAL_PERSISTENCE_NEGATION_RE.search(constraint_text)
    )
    args.save = bool(save_requested and not persistence_forbidden)
    args.local_persistence_boundary_receipt = {
        "schema": "keystone.local_persistence_boundary.v1",
        "save_requested": save_requested,
        "save_allowed": bool(args.save),
        "local_persistence_performed": False,
        "reason": (
            "natural_request_forbids_local_persistence"
            if persistence_forbidden
            else "save_flag_not_requested"
            if not save_requested
            else "save_flag_allowed"
        ),
    }
    return args


def _attach_local_persistence_boundary(
    payload: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    receipt = getattr(args, "local_persistence_boundary_receipt", None)
    if not isinstance(receipt, dict):
        return
    if receipt.get("save_requested") and not receipt.get("save_allowed"):
        payload["local_persistence_boundary"] = dict(receipt)


def _manual_plan_comparison_targets(
    plan: dict[str, Any],
) -> tuple[str, str] | None:
    """Resolve exactly two named companies from the typed planner contract."""

    if (
        plan.get("target_agent") != "business_research_analyst"
        or plan.get("intent") not in {"company_research", "research_brief"}
        or plan.get("target_type") != "company"
    ):
        return None
    target_parts = [
        _clean_comparison_target_name(item)
        for item in re.split(
            r"\s+(?:vs\.?|versus)\s+",
            str(plan.get("primary_target") or ""),
            flags=re.I,
        )
    ]
    target_parts = [item for item in target_parts if item]
    if len(target_parts) == 2:
        return target_parts[0], target_parts[1]
    entities = list(
        dict.fromkeys(
            cleaned
            for item in plan.get("required_entities") or []
            if (cleaned := _clean_comparison_target_name(item))
        )
    )
    target_context = " ".join(
        str(plan.get(key) or "") for key in ("primary_target", "objective")
    ).lower()
    if len(entities) == 2 and all(entity.lower() in target_context for entity in entities):
        return entities[0], entities[1]
    return None


def _clean_comparison_target_name(value: Any) -> str:
    cleaned = " ".join(str(value or "").split()).strip(" .,:;-[]")
    cleaned = re.split(
        r"\b(?:as|for|with|using|about|relevant|possible|potential)\b",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .,:;-[]")
    return cleaned[:120]


def _manual_plan_objective(args: argparse.Namespace) -> str:
    plan = getattr(args, "manual_request_plan", None)
    if isinstance(plan, dict):
        return str(plan.get("objective") or "").strip()
    return ""


def _apply_interpreted_retrieval_mode(args: argparse.Namespace) -> argparse.Namespace:
    """Select the compact lane from typed ask shape, including bridge-owned runs."""

    if args.quick_retrieval:
        return args
    plan = getattr(args, "manual_request_plan", None)
    if not isinstance(plan, dict):
        return args
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, dict):
        return args
    evidence_depth = str(ask_shape.get("evidence_depth") or "unspecified")
    if evidence_depth == "deep":
        return args
    constraints = output_constraints_from_plan(plan)
    explicit_quick = (
        evidence_depth == "quick"
        or str(ask_shape.get("cost_mode") or "unspecified") == "minimize"
    )
    bounded_compact_output = (
        str(ask_shape.get("output_form") or "unspecified") in {"brief", "bullets"}
        and (
            str(ask_shape.get("ask_breadth") or "unspecified")
            in {"narrow", "bounded"}
            or (
                bool(args.compare_company)
                and constraints.maximum_items is not None
                and constraints.maximum_items <= 6
            )
        )
        and (
            (
                constraints.source_url_count is not None
                and constraints.source_url_count <= 3
            )
            or (
                constraints.word_count is not None
                and constraints.word_count <= 100
            )
            or (
                constraints.maximum_items is not None
                and constraints.maximum_items <= 4
                and constraints.include_source_urls
            )
        )
    )
    if explicit_quick or bounded_compact_output:
        args.quick_retrieval = True
        args.max_results = min(int(args.max_results), 2)
    return args


def _compact_official_source_page_limit(args: argparse.Namespace) -> int | None:
    """Return a tiny first-party extraction cap for compact official-source asks."""

    if not args.quick_retrieval:
        return None
    plan = getattr(args, "manual_request_plan", None)
    if not isinstance(plan, dict):
        return None
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, dict):
        return None
    source_types = {
        str(item)
        for item in (ask_shape.get("source_type_preference") or [])
        if str(item)
    }
    constraints = output_constraints_from_plan(plan)
    if "official" not in source_types:
        return None
    if constraints.source_url_count is None:
        return None
    return max(1, min(3, constraints.source_url_count))


def _orchestrator_review_request_summary(
    args: argparse.Namespace,
    *,
    fallback: str,
) -> str:
    """Use the raw/manual request as the review basis before target labels."""

    request_text = str(getattr(args, "request_text", "") or "").strip()
    if request_text:
        return request_text
    preflight = getattr(args, "orchestrator_preflight", None)
    if isinstance(preflight, dict):
        preflight_request = str(preflight.get("request_text") or "").strip()
        if preflight_request:
            return preflight_request
        memo = preflight.get("preflight_memo")
        if isinstance(memo, dict):
            memo_request = str(memo.get("raw_request") or "").strip()
            if memo_request:
                return memo_request
    objective = _manual_plan_objective(args)
    return objective or fallback


def _apply_live_test_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Promote env-backed live test defaults for SDK-only company research paths."""

    if (
        (args.focused_brief or args.improvement_case == CR1_IMPROVEMENT_CASE_ID)
        and not args.sdk
        and not sdk_execution_requested(args)
        and cli_default_live_sdk()
    ):
        args.live_sdk = True
    return args


def _apply_fixture_safety_defaults(
    args: argparse.Namespace,
    argv: list[str] | tuple[str, ...],
) -> argparse.Namespace:
    """Keep explicit fixtures offline unless the operator explicitly enables live flags."""

    if not (args.fixture or args.compare_fixture):
        return args
    if "--live-search" not in argv:
        args.live_search = False
    if "--no-dry-run" not in argv:
        args.dry_run = True
    return args


def _company_context(profile: CompanyProfile, founder_context: str = "") -> str:
    payload = {
        "name": profile.name,
        "website": profile.website,
        "description": profile.description,
        "fit_summary": profile.fit_summary,
        "evidence": profile.evidence,
        "sources": profile.sources,
        "missing_information": profile.missing_information,
        "confidence_score": profile.confidence_score,
        "consulting_fit_score": profile.consulting_fit_score,
    }
    company_context = (
        "Approved local company research context:\n"
        f"{json.dumps(jsonable(payload), ensure_ascii=True, sort_keys=True)}"
    )
    return "\n\n".join(item for item in (company_context, founder_context) if item)


def _retrieval_metadata(
    args: argparse.Namespace,
    *,
    retrieval_mode: str,
    fixture: str | None = None,
    search_queries: list[str] | None = None,
    raw_search_result_count: int = 0,
    hybrid_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = None
    if retrieval_mode == "live_search":
        provider = args.search_provider or load_settings().search_provider
    payload = {
        "mode": retrieval_mode,
        "fixture": fixture if fixture is not None else args.fixture,
        "live_search": retrieval_mode == "live_search",
        "search_provider": provider,
        "search_queries": list(search_queries or []),
        "raw_search_result_count": raw_search_result_count,
        "max_results": args.max_results,
    }
    if hybrid_metadata:
        payload.update(hybrid_metadata)
    payload["retrieval_diagnostics"] = retrieval_diagnostics_from_metadata(payload)
    return payload


def _verified_source_evidence_entry(
    profile: CompanyProfile,
    retrieval: Any,
) -> dict[str, Any]:
    """Capture deterministic source URLs before model synthesis."""

    metadata = retrieval if isinstance(retrieval, dict) else {}
    all_source_payloads = [
        {
            "source_id": source.source_id,
            "title": source.title,
            "url": source.url,
            "source_type": source.source_type,
        }
        for source in profile.sources
    ]
    resolved_official_url = (
        _summary_text(metadata.get("resolved_company_url"))
        or _summary_text(profile.website)
        or infer_official_company_url(
            company=profile.name,
            search_results=all_source_payloads,
        )
    )
    triage = metadata.get("source_triage")
    triage_available = isinstance(triage, dict)
    triage_payload = triage if triage_available else {}
    retained_urls = {
        canonical
        for value in (triage_payload.get("retained_urls") or [])
        if (canonical := _canonical_source_url(value))
    }
    official_sources = [
        source
        for source in all_source_payloads
        if resolved_official_url
        and company_source_matches_official_url(
            str(source.get("url") or ""),
            resolved_official_url,
        )
    ]
    admitted_sources = [
        source
        for source in all_source_payloads
        if source in official_sources
        or not triage_available
        or _canonical_source_url(source.get("url")) in retained_urls
    ]
    source_payloads = [
        *official_sources,
        *(source for source in admitted_sources if source not in official_sources),
    ]
    return {
        "entity": profile.name,
        "resolved_official_url": resolved_official_url,
        "sources": source_payloads,
        "official_sources": official_sources,
        "source_admission": {
            "candidate_count": len(all_source_payloads),
            "admitted_count": len(source_payloads),
            "rejected_count": max(0, len(all_source_payloads) - len(source_payloads)),
            "triage_applied": triage_available,
        },
    }


def _provider_candidate_universe(tool_receipts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Summarize the bounded identities returned by model-called web search tools."""

    search_receipts = [
        receipt
        for receipt in tool_receipts
        if str(receipt.get("tool_name") or "") == "search_web"
    ]
    identities = list(
        dict.fromkeys(
            str(identity or "").strip()
            for receipt in search_receipts
            for identity in list(receipt.get("identity_fingerprints") or [])
            if str(identity or "").strip()
        )
    )
    return {
        "schema": "keystone.provider_candidate_universe.v1",
        "source": "model_called_search_web",
        "search_receipt_count": len(search_receipts),
        "candidate_count": sum(
            int(receipt.get("item_count") or 0) for receipt in search_receipts
        ),
        "identity_fingerprints": identities,
        "bounded": True,
    }


def _agent_owned_model_metadata(
    *,
    run_config: Any | None,
    live: bool,
) -> tuple[str, str, str]:
    if run_config is not None:
        return "local", str(getattr(run_config, "model", "") or "sdk-local"), "local_sdk"
    config = get_runtime_agent_model_config("business_research_analyst")
    return config.provider, config.model, "live_sdk" if live else "sdk"


def _run_agent_owned_live_company_synthesis(
    args: argparse.Namespace,
    *,
    run_config: Any | None,
    live: bool,
    comparison_requested: bool,
    focused_brief_requested: bool,
    founder_context: str,
    preflight_context: str,
) -> dict[str, Any]:
    """Let Business Research own search, deepening, and source selection."""

    fallback = (
        f"Compare {args.company} with {args.compare_company}."
        if comparison_requested
        else _company_research_request_text(args)
    )
    raw_request = _orchestrator_review_request_summary(args, fallback=fallback)
    provider_guidance = (
        f"Control-plane search preference: use {args.search_provider} first; "
        "the shared retrieval policy may use reviewed fallbacks."
        if args.search_provider
        else ""
    )
    context = "\n\n".join(
        item
        for item in (
            f"Current operator request (authoritative):\n{raw_request}",
            provider_guidance,
            founder_context,
            preflight_context,
        )
        if item
    )
    retrieval_hint = _explicit_retrieval_hint(args)
    if comparison_requested:
        typed_input: Any = BusinessResearchComparisonSDKInput(
            company_a=args.company,
            company_b=args.compare_company,
            decision_goal=raw_request,
            decision_criteria=tuple(
                parse_company_research_comparison_criteria(args.decision_criteria)
            ),
            requested_output_format=args.output_format,
            source_context=context,
            retrieval_hint=retrieval_hint,
        )
        runner = run_business_research_analyst_comparison_sdk
    elif focused_brief_requested:
        typed_input = BusinessResearchFocusedBriefSDKInput(
            company_name=str(args.company or ""),
            company_url=args.company_url,
            source_context=context,
            brief_goal=raw_request,
            retrieval_hint=retrieval_hint,
        )
        runner = run_business_research_analyst_focused_brief_sdk
    else:
        typed_input = BusinessResearchSDKInput(
            company_name=str(args.company or ""),
            company_url=args.company_url,
            lead_name=args.lead_name,
            linkedin_url=args.linkedin_url,
            context=context,
            retrieval_hint=retrieval_hint,
        )
        runner = run_business_research_analyst_sdk

    started_at = time_module.time()
    result = runner(
        typed_input,
        run_config=run_config,
        live=live,
        manual_request_plan=getattr(args, "manual_request_plan", None),
        attach_tools=True,
        compact_instructions=args.compact_instructions,
        provider_retrieval_required=True,
    )
    model_provider, model_name, model_run_mode = _agent_owned_model_metadata(
        run_config=run_config,
        live=live,
    )
    storage_results: dict[str, Any] = {}
    if args.save:
        storage = StorageTool(args.database_url)
        storage_results["agent_run"] = storage.save_agent_run(
            agent_name="business_research_analyst",
            input_payload={
                "raw_request": raw_request,
                "company": args.company,
                "compare_company": args.compare_company,
                "live_search": True,
                "execution_mode": "agent_owned_tool_loop",
            },
            input_summary=raw_request,
            output=jsonable(result.final_output),
            model="sdk-live" if result.live else "sdk-local",
            dry_run=not result.live,
            status="success",
        )
    outcome = SDKSynthesisOutcome(
        agent_name="business_research_analyst",
        raw_context={
            "mode": "agent_owned_provider_selection",
            "raw_request_preserved": True,
            "preacquired_provider_context": False,
        },
        typed_input=typed_input,
        result=result,
        storage=storage_results,
        audit_notes=(
            "Business Research selected the search query, bounded tools, and sources.",
            "Python enforced provider availability, fallback, budgets, URL safety, "
            "and identity validation.",
            "No deterministic helper preselected or substituted a source.",
        ),
        model_provider=model_provider,
        model_name=model_name,
        model_run_mode=model_run_mode,
        usage=result.usage,
        cost=result.cost,
        budget_guard=result.budget_guard,
        request_cache=result.request_cache,
        execution_telemetry=result.execution_telemetry,
        started_at_unix=started_at,
        ended_at_unix=time_module.time(),
    )
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    payload["retrieval"] = {
        "mode": "agent_owned_tool_loop",
        "live_search": True,
        "requested_provider": args.search_provider,
    }
    payload["retrieval_diagnostics"] = jsonable(
        getattr(result.final_output, "retrieval_diagnostics", {})
    )
    payload["tool_receipts"] = list(result.tool_receipts)
    payload["provider_candidate_universe"] = _provider_candidate_universe(
        result.tool_receipts
    )
    payload["decision_ownership"] = result.request_cache.get("decision_ownership", {})
    payload["tool_execution"] = result.request_cache.get("tool_execution", {})
    payload["execution_telemetry"] = result.execution_telemetry
    if comparison_requested:
        payload["comparison_entities"] = [args.company, args.compare_company]
    if getattr(args, "manual_request_plan", None):
        payload["manual_request_plan"] = args.manual_request_plan
    human_summary = _company_research_sdk_human_summary(payload)
    _attach_company_research_display_text(payload, human_summary)
    _attach_company_research_output_constraint_validation(payload)
    attach_orchestrator_preflight_payload(payload, args)
    _save_retrieval_tool_performance_memory(args, payload)
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="business_research_analyst",
            output=result.final_output,
            request_summary=raw_request,
            run_type=(
                "live SDK + agent-owned live search"
                if live
                else "local SDK + agent-owned live search"
            ),
        )
    return payload


def _company_research_request_text(args: argparse.Namespace) -> str:
    return _live_company_research_request_text(
        args.company,
        args.company_url,
    )


def _explicit_retrieval_hint(args: argparse.Namespace) -> RetrievalHint | None:
    raw_value = getattr(args, "retrieval_hint_json", None)
    if not raw_value:
        return None
    try:
        return RetrievalHint.model_validate_json(raw_value)
    except ValueError as exc:
        raise SystemExit(f"Invalid --retrieval-hint-json: {exc}") from exc


def _search_provider_label(metadata: dict[str, Any]) -> str:
    return _live_search_provider_label(metadata)


def _company_query_builder_for_args(
    args: argparse.Namespace,
) -> Callable[[str, str | None], list[str]]:
    def build_queries(company: str, company_url: str | None) -> list[str]:
        queries = build_company_research_queries(company, company_url)
        if args.improvement_case == CR1_IMPROVEMENT_CASE_ID:
            return queries[:4]
        if getattr(args, "live_search_plan", False):
            request_text = " ".join(
                part
                for part in (
                    getattr(args, "request_text", ""),
                    getattr(args, "research_goal", ""),
                    getattr(args, "notes", ""),
                )
                if str(part or "").strip()
            )
            plan = resolve_web_query_plan(
                subject=company,
                request_text=request_text,
                fallback_queries=queries,
                max_queries=12,
                live=True,
                planner_context=orchestrator_preflight_context_text(args),
            )
            return plan.queries
        return queries

    return build_queries


def _retrieve_company_profile(
    args: argparse.Namespace,
    *,
    company: str | None = None,
    company_url: str | None = None,
    lead_name: str | None = None,
    linkedin_url: str | None = None,
    fixture: str | None = None,
) -> tuple[CompanyProfile, dict[str, Any]]:
    resolved_company = company or args.company
    resolved_company_url = company_url or args.company_url
    resolved_lead_name = lead_name or args.lead_name
    resolved_linkedin_url = linkedin_url or args.linkedin_url
    resolved_fixture = fixture if fixture is not None else args.fixture

    if args.selected_url_extraction:
        if args.live_search:
            raise SystemExit(
                "--selected-url-extraction cannot be combined with --live-search."
            )
        if not resolved_company_url:
            raise SystemExit("--selected-url-extraction requires --company-url.")
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--selected-url-extraction",
            live_action="bounded selected public URL extraction",
        )
        try:
            extraction = build_selected_url_source_bundle(
                company_name=str(resolved_company or resolved_company_url).strip(),
                company_url=resolved_company_url,
                selected_urls=[resolved_company_url],
                live_extraction=True,
            )
        except WebsiteExtractionError as exc:
            raise SystemExit(f"Selected public URL extraction failed: {exc}") from exc
        if extraction.extracted_source_count <= 0:
            limitations = "; ".join(
                str(item.error or item.status or "no source claims")
                for item in extraction.diagnostics[:3]
            )
            raise SystemExit(
                "Selected public URL extraction returned no source-backed claims"
                + (f": {limitations}" if limitations else ".")
            )
        profile = synthesize_company_profile_from_source_bundle(
            company_name=str(resolved_company or extraction.company_name).strip(),
            company_url=resolved_company_url,
            source_bundle=extraction.source_bundle,
        )
        diagnostics = [item.model_dump(mode="json") for item in extraction.diagnostics]
        metadata = _retrieval_metadata(args, retrieval_mode="selected_url_extraction")
        metadata["retrieval_diagnostics"] = {
            "mode": "selected_url_extraction",
            "live_search": False,
            "providers_used": list(
                dict.fromkeys(
                    item.provider for item in extraction.diagnostics if item.provider
                )
            ),
            "selected_urls": [resolved_company_url],
            "selected_url_count": extraction.selected_url_count,
            "extracted_source_count": extraction.extracted_source_count,
            "broad_search_performed": False,
            "external_write_performed": False,
            "website_extraction_summary": {
                "diagnostics": diagnostics,
                "firecrawl_calls_attempted": extraction.firecrawl_calls_attempted,
            },
        }
        return profile, metadata

    inline_source_context = str(
        getattr(args, "inline_source_context", "") or ""
    ).strip()
    if inline_source_context:
        if args.live_search:
            raise SystemExit("--inline-source-context cannot be combined with --live-search.")
        profile = CompanyProfile(
            name=str(resolved_company or "Operator-provided company context").strip(),
            website=resolved_company_url,
            description=inline_source_context,
            sources=[
                SourceRecord(
                    source_id="operator:inline_company_context",
                    title="Operator-provided company context",
                    url="operator://inline-company-context",
                    source_type="user_provided",
                    supported_claims=[inline_source_context],
                    evidence_excerpt=inline_source_context[:1000],
                    confidence=0.7,
                )
            ],
            missing_information=[
                "No external verification was requested; claims are limited to "
                "operator-provided context."
            ],
        )
        return profile, _retrieval_metadata(args, retrieval_mode="inline_context")

    if args.live_search:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-search",
            live_action="live company research network search",
        )
        compact_official_page_limit = _compact_official_source_page_limit(args)
        profile, metadata = retrieve_company_profile_live(
            company=resolved_company,
            company_url=resolved_company_url,
            request_text=" ".join(
                part
                for part in (
                    getattr(args, "request_text", ""),
                    getattr(args, "research_goal", ""),
                    getattr(args, "notes", ""),
                )
                if str(part or "").strip()
            )
            or None,
            requested_provider=args.search_provider,
            max_results=args.max_results,
            agents_web_search_max_calls=1 if args.quick_retrieval else None,
            tavily_search_fallback=False if args.quick_retrieval else None,
            exa_search_fallback=False if args.quick_retrieval else None,
            extract_selected_pages=(
                not args.quick_retrieval or compact_official_page_limit is not None
            ),
            website_extraction_max_pages=compact_official_page_limit,
            discover_internal_company_pages=compact_official_page_limit is None,
            official_company_sources_only=compact_official_page_limit is not None,
            max_queries=2 if args.quick_retrieval else None,
            retrieval_hint=_explicit_retrieval_hint(args),
            settings_loader=load_settings,
            query_builder=_company_query_builder_for_args(args),
            search_provider_builder=build_search_provider,
            profile_builder=research_account_from_search_results,
        )
        return profile, _retrieval_metadata(
            args,
            retrieval_mode="live_search",
            fixture=resolved_fixture,
            search_queries=metadata.get("search_queries"),
            raw_search_result_count=int(metadata.get("raw_search_result_count") or 0),
            hybrid_metadata=metadata,
        )

    profile = research_company_fixture(
        company_name=resolved_company,
        company_url=resolved_company_url,
        lead_name=resolved_lead_name,
        linkedin_url=resolved_linkedin_url,
        fixture_json=Path(resolved_fixture) if resolved_fixture else None,
    )
    return profile, _retrieval_metadata(args, retrieval_mode="fixture", fixture=resolved_fixture)


def _retrieve_company_comparison(
    args: argparse.Namespace,
) -> tuple[CompanyResearchComparison, dict[str, Any], CompanyProfile, CompanyProfile]:
    primary_profile, primary_retrieval = _retrieve_company_profile(args)
    comparison_profile, comparison_retrieval = _retrieve_company_profile(
        args,
        company=args.compare_company,
        company_url=args.compare_company_url,
        lead_name=args.compare_lead_name,
        linkedin_url=args.compare_linkedin_url,
        fixture=args.compare_fixture,
    )
    primary_profile = _namespace_company_profile_sources(
        primary_profile,
        namespace="company_a",
    )
    comparison_profile = _namespace_company_profile_sources(
        comparison_profile,
        namespace="company_b",
    )
    criteria = parse_company_research_comparison_criteria(args.decision_criteria)
    comparison = compare_company_profiles_for_decision(
        primary_profile,
        comparison_profile,
        decision_goal=(
            _manual_plan_objective(args)
            or str(args.request_text or "").strip()
            or (
                "Compare both companies as possible partners or advisory targets "
                "for Keystone."
            )
        ),
        criteria=criteria,
        requested_output_format=args.output_format,
    )
    return (
        comparison,
        {"primary": primary_retrieval, "comparison": comparison_retrieval},
        primary_profile,
        comparison_profile,
    )


def _namespace_company_profile_sources(
    profile: CompanyProfile,
    *,
    namespace: str,
) -> CompanyProfile:
    """Keep independently retrieved profile evidence identities collision-free."""

    prefix = re.sub(r"[^a-z0-9_-]+", "-", namespace.lower()).strip("-") or "company"
    source_id_map = {
        source.source_id: f"{prefix}:{source.source_id}" for source in profile.sources
    }
    return CompanyProfile.model_validate(
        profile.model_copy(
            update={
                "sources": [
                    source.model_copy(
                        update={"source_id": source_id_map[source.source_id]}
                    )
                    for source in profile.sources
                ],
                "claims": [
                    claim.model_copy(
                        update={
                            "source_id": source_id_map.get(
                                claim.source_id,
                                f"{prefix}:{claim.source_id}",
                            )
                        }
                    )
                    for claim in profile.claims
                ],
                "features": [
                    feature.model_copy(
                        update={
                            "source_id": source_id_map.get(
                                feature.source_id,
                                f"{prefix}:{feature.source_id}",
                            )
                        }
                    )
                    for feature in profile.features
                ],
                "research_data_points": [
                    data_point.model_copy(
                        update={
                            "source_ids": [
                                source_id_map.get(source_id, f"{prefix}:{source_id}")
                                for source_id in data_point.source_ids
                            ]
                        }
                    )
                    for data_point in profile.research_data_points
                ],
            }
        ).model_dump(mode="json")
    )


def _run_sdk_synthesis(args: argparse.Namespace) -> dict[str, Any]:
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    comparison_requested = bool(args.compare_company)
    focused_brief_requested = (
        bool(args.focused_brief) or args.improvement_case == CR1_IMPROVEMENT_CASE_ID
    )
    focused_comparison_requested = comparison_requested and focused_brief_requested
    structured_comparison_requested = (
        comparison_requested and not focused_comparison_requested
    )
    founder_profile = load_founder_fit_profile(args.founder_fit_profile)
    founder_context = founder_search_context(founder_profile) if founder_profile else ""
    preflight_context = orchestrator_preflight_context_text(args)

    if args.live_search and not args.improvement_case:
        return _run_agent_owned_live_company_synthesis(
            args,
            run_config=run_config,
            live=live,
            comparison_requested=comparison_requested,
            focused_brief_requested=focused_brief_requested,
            founder_context=founder_context,
            preflight_context=preflight_context,
        )

    retrieval_state: dict[str, Any] = {}
    verified_source_evidence_state: list[dict[str, Any]] = []

    def retrieve() -> CompanyProfile | CompanyResearchComparison:
        if comparison_requested:
            comparison, metadata, primary_profile, comparison_profile = (
                _retrieve_company_comparison(args)
            )
            retrieval_state.clear()
            retrieval_state.update(metadata)
            verified_source_evidence_state.clear()
            verified_source_evidence_state.extend(
                [
                    _verified_source_evidence_entry(
                        primary_profile,
                        metadata.get("primary"),
                    ),
                    _verified_source_evidence_entry(
                        comparison_profile,
                        metadata.get("comparison"),
                    ),
                ]
            )
            return comparison

        profile, metadata = _retrieve_company_profile(args)
        retrieval_state.clear()
        retrieval_state.update(metadata)
        verified_source_evidence_state.clear()
        verified_source_evidence_state.append(
            _verified_source_evidence_entry(profile, metadata)
        )
        return profile

    def normalize_profile(profile: CompanyProfile) -> BusinessResearchSDKInput:
        return BusinessResearchSDKInput(
            company_name=args.company,
            company_url=args.company_url,
            lead_name=args.lead_name,
            linkedin_url=args.linkedin_url,
            context=_company_context(
                profile,
                "\n\n".join(item for item in (founder_context, preflight_context) if item),
            ),
            retrieval_hint=_explicit_retrieval_hint(args),
        )

    def normalize_brief(
        profile: CompanyProfile | CompanyResearchComparison,
    ) -> BusinessResearchFocusedBriefSDKInput:
        manual_objective = _manual_plan_objective(args)
        if isinstance(profile, CompanyResearchComparison):
            typed_input = BusinessResearchFocusedBriefSDKInput(
                company_name=f"{profile.company_a.name} vs {profile.company_b.name}",
                source_context=comparison_context_from_result(profile),
                brief_goal=(
                    manual_objective
                    or str(args.request_text or "").strip()
                    or (
                        "Prepare a concise, source-backed comparison that directly "
                        "answers the operator's requested facets."
                    )
                ),
                retrieval_hint=_explicit_retrieval_hint(args),
            )
        else:
            typed_input = focused_brief_input_from_profile(
                profile,
                brief_goal=(
                    CR1_IMPROVEMENT_PROMPT
                    if args.improvement_case == CR1_IMPROVEMENT_CASE_ID
                    else manual_objective or None
                ),
            )
        if founder_context:
            return BusinessResearchFocusedBriefSDKInput(
                company_name=typed_input.company_name,
                company_url=typed_input.company_url,
                source_context="\n\n".join(
                    item
                    for item in (
                        typed_input.source_context,
                        founder_context,
                        preflight_context,
                    )
                    if item
                ),
                brief_goal=typed_input.brief_goal,
                retrieval_hint=_explicit_retrieval_hint(args),
            )
        if preflight_context:
            return BusinessResearchFocusedBriefSDKInput(
                company_name=typed_input.company_name,
                company_url=typed_input.company_url,
                source_context="\n\n".join(
                    item for item in (typed_input.source_context, preflight_context) if item
                ),
                brief_goal=typed_input.brief_goal,
                retrieval_hint=_explicit_retrieval_hint(args),
            )
        if _explicit_retrieval_hint(args) is not None:
            return BusinessResearchFocusedBriefSDKInput(
                company_name=typed_input.company_name,
                company_url=typed_input.company_url,
                source_context=typed_input.source_context,
                brief_goal=typed_input.brief_goal,
                retrieval_hint=_explicit_retrieval_hint(args),
            )
        return typed_input

    def normalize_comparison(
        comparison: CompanyResearchComparison,
    ) -> BusinessResearchComparisonSDKInput:
        typed_input = comparison_input_from_result(
            comparison,
            retrieval_hint=_explicit_retrieval_hint(args),
        )
        if founder_context:
            return BusinessResearchComparisonSDKInput(
                company_a=typed_input.company_a,
                company_b=typed_input.company_b,
                decision_goal=typed_input.decision_goal,
                decision_criteria=typed_input.decision_criteria,
                requested_output_format=typed_input.requested_output_format,
                source_context="\n\n".join(
                    item
                    for item in (
                        typed_input.source_context,
                        founder_context,
                        preflight_context,
                    )
                    if item
                ),
                retrieval_hint=typed_input.retrieval_hint,
            )
        if preflight_context:
            return BusinessResearchComparisonSDKInput(
                company_a=typed_input.company_a,
                company_b=typed_input.company_b,
                decision_goal=typed_input.decision_goal,
                decision_criteria=typed_input.decision_criteria,
                requested_output_format=typed_input.requested_output_format,
                source_context="\n\n".join(
                    item for item in (typed_input.source_context, preflight_context) if item
                ),
                retrieval_hint=typed_input.retrieval_hint,
            )
        return typed_input

    output_type: (
        type[CompanyProfile] | type[CompanyResearchFocusedBrief] | type[CompanyResearchComparison]
    )

    def source_bundle_decision_contract(raw: Any, _typed_input: Any) -> Any:
        return business_research_context_decision_contract(raw)

    if structured_comparison_requested:
        output_type = CompanyResearchComparison
        agent = build_business_research_analyst_comparison_agent(
            attach_tools=not args.compact_instructions,
            compact_instructions=args.compact_instructions,
        )
        normalize = normalize_comparison
        decision_contract = business_research_comparison_decision_contract()
    elif focused_brief_requested:
        output_type = CompanyResearchFocusedBrief
        # Retrieval is completed and normalized before this synthesis call. Keep the
        # focused brief tool-free so a bounded Slack research ask uses one model turn.
        agent = build_business_research_analyst_focused_brief_agent(
            attach_tools=False,
            compact_instructions=args.compact_instructions,
        )
        normalize = normalize_brief
        decision_contract = source_bundle_decision_contract
    else:
        output_type = CompanyProfile
        agent = build_business_research_analyst_agent(
            attach_tools=not args.compact_instructions,
            compact_instructions=args.compact_instructions,
        )
        normalize = normalize_profile
        decision_contract = source_bundle_decision_contract
    storage = StorageTool(args.database_url) if args.save else None
    outcome = run_retrieved_sdk_synthesis(
        agent=agent,
        output_type=output_type,
        retrieve=retrieve,
        normalize=normalize,
        input_summary=(
            f"{args.improvement_case} company research for {args.company}"
            if args.improvement_case
            else args.company
        ),
        input_audit_payload={
            "company": args.company,
            "company_url": args.company_url,
            "lead_name": args.lead_name,
            "linkedin_url": args.linkedin_url,
            "fixture": args.fixture,
            "compare_company": args.compare_company,
            "compare_company_url": args.compare_company_url,
            "compare_fixture": args.compare_fixture,
            "sdk_synthesis": True,
            "focused_brief": focused_brief_requested,
            "comparison": comparison_requested,
            "improvement_case": args.improvement_case,
            "live_search": bool(args.live_search),
            "search_provider": args.search_provider,
            "max_results": args.max_results,
            "retrieval_hint": (
                _explicit_retrieval_hint(args).model_dump(mode="json")
                if _explicit_retrieval_hint(args) is not None
                else None
            ),
            "improvement_prompt": (
                CR1_IMPROVEMENT_PROMPT if args.improvement_case == CR1_IMPROVEMENT_CASE_ID else None
            ),
            "acceptance_criteria": (
                list(CR1_ACCEPTANCE_CRITERIA)
                if args.improvement_case == CR1_IMPROVEMENT_CASE_ID
                else []
            ),
            "founder_fit_profile": founder_profile_audit_payload(
                args.founder_fit_profile,
                founder_profile,
            ),
            "manual_request_plan": getattr(args, "manual_request_plan", None),
        },
        run_config=run_config,
        live=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
        save=args.save,
        storage=storage,
        model_label="sdk-live" if live else "sdk-local",
        decision_contract=decision_contract,
    )
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    payload["retrieval"] = dict(retrieval_state) or _retrieval_metadata(
        args,
        retrieval_mode="live_search" if args.live_search else "fixture",
    )
    payload["retrieval_diagnostics"] = payload["retrieval"].get("retrieval_diagnostics")
    if comparison_requested:
        payload["comparison_entities"] = [args.company, args.compare_company]
    payload["verified_source_evidence"] = list(verified_source_evidence_state)
    if getattr(args, "manual_request_plan", None):
        payload["manual_request_plan"] = args.manual_request_plan
    human_summary = _company_research_sdk_human_summary(payload)
    _attach_company_research_display_text(payload, human_summary)
    _attach_company_research_output_constraint_validation(payload)
    attach_orchestrator_preflight_payload(payload, args)
    _save_retrieval_tool_performance_memory(args, payload)
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="business_research_analyst",
            output=outcome.final_output,
            request_summary=_orchestrator_review_request_summary(
                args,
                fallback=(
                    f"{args.company} vs {args.compare_company}"
                    if comparison_requested
                    else args.company
                ),
            ),
            run_type=(
                "live SDK + live search"
                if live and args.live_search
                else "local SDK + live search"
                if args.live_search
                else "live SDK"
                if live
                else "local SDK"
            ),
        )
    if args.improvement_case == CR1_IMPROVEMENT_CASE_ID:
        payload["improvement_case"] = CR1_IMPROVEMENT_CASE_ID
        payload["test_pack_prompt"] = CR1_IMPROVEMENT_PROMPT
        payload["acceptance_criteria"] = list(CR1_ACCEPTANCE_CRITERIA)
    return payload


_LIMITATION_PHRASES = (
    "could not verify",
    "did not find",
    "not independently verified",
    "rather than independently verified",
    "not verified",
    "remains unverified",
    "remains uncertain",
    "treat that part as unconfirmed",
)

_LIMITATION_TERM_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "clearly",
    "could",
    "did",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "not",
    "of",
    "or",
    "public",
    "rather",
    "so",
    "supplied",
    "that",
    "the",
    "this",
    "those",
    "to",
    "treat",
    "was",
    "were",
    "with",
    "would",
}


def _limitation_terms(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 2 and token not in _LIMITATION_TERM_STOPWORDS
    }


def _answer_with_consolidated_limitations(
    answer: str,
    unknowns: list[str],
) -> tuple[str, list[str]]:
    """Move answer-level caveats into one dedicated limitations section."""

    if not answer:
        return answer, unknowns

    consolidated = list(unknowns)

    def add_limitation(value: str) -> None:
        cleaned = re.sub(
            r"^(?:caveats?|limitations?|uncertainty)\s*:\s*",
            "",
            value.strip(),
            flags=re.I,
        ).strip()
        if not cleaned:
            return
        fingerprint = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
        existing = {
            re.sub(r"[^a-z0-9]+", " ", item.lower()).strip()
            for item in consolidated
        }
        if fingerprint not in existing:
            consolidated.append(cleaned)

    retained: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", answer.strip()):
        lowered = sentence.lower()
        if re.match(r"^(?:caveats?|limitations?|uncertainty)\s*:", sentence, flags=re.I):
            add_limitation(sentence)
            continue
        if not any(phrase in lowered for phrase in _LIMITATION_PHRASES):
            retained.append(sentence)
            continue

        parts = re.split(r",?\s+but\s+", sentence, maxsplit=1, flags=re.I)
        supported_prefix = parts[0]
        prefix_terms = _limitation_terms(supported_prefix)
        starts_with_reference = supported_prefix.lower().startswith(
            ("it ", "that ", "these ", "those ", "they ", "such ")
        )
        if (
            len(parts) == 2
            and len(prefix_terms) >= 5
            and not starts_with_reference
        ):
            retained.append(supported_prefix.rstrip(" ,;:.!?") + ".")
            limitation = parts[1].strip()
        else:
            limitation = sentence

        unknown_terms = _limitation_terms(" ".join(consolidated))
        if len(_limitation_terms(limitation) & unknown_terms) < 2:
            add_limitation(limitation)

    return " ".join(retained).strip() or answer, consolidated


def _company_research_sdk_human_summary(payload: dict[str, Any]) -> str:
    """Build a Slack-safe reader summary from structured SDK company output."""

    if payload.get("output_type") != "CompanyResearchFocusedBrief":
        return ""
    output = payload.get("output")
    if not isinstance(output, dict):
        return ""
    company = _summary_text(output.get("company_name")) or "The company"
    product = _summary_text(output.get("product"))
    customers = _summary_text(output.get("customers"))
    traction = _summary_text(output.get("traction_signals"))
    why_it_matters = _summary_text(output.get("why_it_matters"))
    unknowns = _summary_list(output.get("unknowns"), limit=4)
    facts = _summary_facts(output.get("facts"), limit=4)
    verified_sources = _verified_sources_for_payload(payload)
    source_value = verified_sources or output.get("sources")
    sources = _summary_sources(source_value, limit=5)
    output_constraints = output_constraints_from_plan(payload.get("manual_request_plan"))
    ask_shape = (
        payload.get("manual_request_plan", {}).get("ask_shape", {})
        if isinstance(payload.get("manual_request_plan"), dict)
        else {}
    )
    output_form = str(ask_shape.get("output_form") or "unspecified")
    source_type_preference = {
        str(item)
        for item in (ask_shape.get("source_type_preference") or [])
        if str(item)
    }

    narrow_answer_requested = output_constraints.scope == "answer" and (
        output_constraints.word_count_mode != "unspecified"
        or output_constraints.sentence_count_mode != "unspecified"
    )
    structured_answer = _summary_text(output.get("answer"))
    if narrow_answer_requested:
        answer = structured_answer
        if not answer:
            answer = product or why_it_matters or traction
        if not answer:
            answer = f"{company} has source-backed company information available for review."
        sections = ["*Answer:*\n" + answer]
        if sources:
            sections.append("*Useful reference:*\n" + sources[0])
        return "\n\n".join(sections)

    answer_parts = []
    answer_without_repeated_unknowns, unknowns = _answer_with_consolidated_limitations(
        structured_answer,
        unknowns,
    )
    if answer_without_repeated_unknowns:
        answer_parts.append(answer_without_repeated_unknowns)
    elif why_it_matters:
        answer_parts.append(_truncate_summary(why_it_matters, 360))
    elif product:
        answer_parts.append(f"{company} appears relevant based on its product/workflow context.")
    else:
        answer_parts.append(f"{company} has source-backed context available for review.")
    if traction and not structured_answer:
        answer_parts.append(f"Key signal: {_truncate_summary(traction, 220)}")

    if output_form == "bullets" and structured_answer:
        official_only = "official" in source_type_preference
        official_company_urls = _official_company_urls_for_payload(
            payload,
            company=company,
            sources=output.get("sources"),
        )
        visible_answer = _format_structured_bullet_answer(
            structured_answer,
            strip_urls=official_only,
        )
        compact_sources = _summary_sources_compact(
            source_value,
            limit=output_constraints.source_url_count or 5,
            official_only=official_only,
            official_company_urls=official_company_urls if official_only else [],
            required_entities=list(_verified_source_evidence_by_entity(payload)),
        )
        if output_constraints.include_source_urls:
            requested_count = output_constraints.source_url_count
            if requested_count is not None and len(compact_sources) < requested_count:
                source_note = (
                    f"Sources: only {len(compact_sources)} of {requested_count} requested "
                    f"{'official ' if official_only else ''}source URLs were verified."
                )
                if compact_sources:
                    source_note += " " + " | ".join(compact_sources)
                return f"{visible_answer}\n\n{source_note}".strip()
            if compact_sources:
                return (
                    f"{visible_answer}\n\nSources: "
                    + " | ".join(compact_sources)
                ).strip()
        return visible_answer

    detail_lines: list[str] = []
    if product:
        detail_lines.append(f"* Product/workflow: {_truncate_summary(product, 420)}")
    if customers:
        detail_lines.append(f"* Healthcare buyer fit: {_truncate_summary(customers, 420)}")
    if traction:
        detail_lines.append(f"* Evidence or deployment signals: {_truncate_summary(traction, 420)}")
    if facts:
        detail_lines.append("* Source-backed facts:")
        detail_lines.extend(f"  * {fact}" for fact in facts)
    if unknowns:
        detail_lines.append("* Limitations / what remains unverified:")
        detail_lines.extend(f"  * {_truncate_summary(item, 240)}" for item in unknowns)
    if not detail_lines:
        detail_lines.append(
            "* No detailed source-backed fields were returned by the focused brief."
        )

    sections = [
        "*Answer:*\n" + " ".join(answer_parts).strip(),
        "*Detailed Summary:*\n" + "\n".join(detail_lines).strip(),
    ]
    if sources:
        sections.append("*Useful references:*\n" + "\n".join(sources))
    return "\n\n".join(section for section in sections if section.strip())


def _attach_company_research_display_text(payload: dict[str, Any], human_summary: str) -> None:
    """Expose answer-first display text for Slack bridge compatibility."""

    display_text = str(human_summary or "").strip()
    if not display_text:
        return
    payload["human_summary"] = display_text
    payload["slack_display_text"] = display_text
    payload["display_text"] = display_text
    payload["summary"] = display_text


def _attach_company_research_output_constraint_validation(payload: dict[str, Any]) -> None:
    """Measure the LLM answer without rewriting it."""

    constraints = output_constraints_from_plan(payload.get("manual_request_plan"))
    if not constraints.is_explicit():
        return
    display_text = str(payload.get("human_summary") or "")
    validation = validate_output_constraints(display_text, constraints)
    validation_payload = validation.model_dump(
        mode="json", exclude={"checked_text"}
    )
    if validation_payload.get("source_url_count") is None:
        validation_payload.pop("source_url_count", None)
    ask_shape = (
        payload.get("manual_request_plan", {}).get("ask_shape", {})
        if isinstance(payload.get("manual_request_plan"), dict)
        else {}
    )
    source_types = {
        str(item)
        for item in (ask_shape.get("source_type_preference") or [])
        if str(item)
    }
    if "official" in source_types:
        output = payload.get("output")
        company = (
            _summary_text(output.get("company_name"))
            if isinstance(output, dict)
            else ""
        )
        official_urls = _official_company_urls_for_payload(
            payload,
            company=company,
            sources=output.get("sources") if isinstance(output, dict) else [],
        )
        visible_urls = re.findall(r"https?://[^\s)>]+", display_text, flags=re.I)
        verified_source_urls = {
            canonical
            for item in _verified_sources_for_payload(payload)
            if (canonical := _canonical_source_url(item.get("url")))
        }
        invalid_urls = [
            url
            for url in visible_urls
            if (
                verified_source_urls
                and _canonical_source_url(url) not in verified_source_urls
            )
            or not official_urls
            or not any(
                company_source_matches_official_url(url, official_url)
                for official_url in official_urls
            )
        ]
        if invalid_urls:
            validation_payload["passed"] = False
            violation = (
                "visible source URL was not present in deterministic retrieved evidence"
                if verified_source_urls
                and any(
                    _canonical_source_url(url) not in verified_source_urls
                    for url in visible_urls
                )
                else "visible source URL is outside the verified official company domain"
            )
            validation_payload.setdefault("violations", []).append(
                violation
            )
        elif visible_urls:
            validation_payload.setdefault("satisfied_constraints", []).append(
                "official company source domains"
            )
        entity_evidence = _verified_source_evidence_by_entity(payload)
        if len(entity_evidence) == 2:
            missing_entities = [
                entity
                for entity, evidence in entity_evidence.items()
                if not any(
                    _canonical_source_url(url)
                    in {
                        canonical
                        for source in evidence.get("official_sources") or []
                        if (canonical := _canonical_source_url(source.get("url")))
                    }
                    for url in visible_urls
                )
            ]
            if missing_entities:
                validation_payload["passed"] = False
                validation_payload.setdefault("violations", []).append(
                    "visible sources do not include an official URL for each company"
                )
            else:
                validation_payload.setdefault("satisfied_constraints", []).append(
                    "one official source domain per comparison company"
                )
    payload["output_constraint_validation"] = validation_payload


def _summary_text(value: Any) -> str:
    text = str(value or "").strip()
    replacements = (
        ("The approved context says", "Source evidence indicates"),
        ("the approved context says", "source evidence indicates"),
        ("in the approved context", "in the source evidence"),
        ("approved source-backed context", "source evidence"),
        ("approved context", "source evidence"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _summary_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_summary_text(item) for item in value if _summary_text(item)][:limit]


def _summary_facts(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    facts: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = _summary_text(item.get("text"))
            source_ids = _summary_list(item.get("source_ids"), limit=3)
            if text and source_ids:
                facts.append(f"{_truncate_summary(text, 260)} (sources: {', '.join(source_ids)})")
            elif text:
                facts.append(_truncate_summary(text, 260))
        else:
            text = _summary_text(item)
            if text:
                facts.append(_truncate_summary(text, 260))
        if len(facts) >= limit:
            break
    return facts


def _summary_sources(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _summary_text(item.get("url"))
        if not url:
            continue
        title = _summary_text(item.get("title")) or url
        lines.append(f"* {title}: {url}")
        if len(lines) >= limit:
            break
    return lines


def _summary_sources_compact(
    value: Any,
    *,
    limit: int,
    official_only: bool,
    official_company_urls: list[str],
    required_entities: list[str] | None = None,
) -> list[str]:
    if not isinstance(value, list):
        return []
    if official_only and not official_company_urls:
        return []
    candidates: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _summary_text(item.get("url"))
        if not url or url in seen_urls:
            continue
        if official_only and not any(
            company_source_matches_official_url(url, official_url)
            for official_url in official_company_urls
        ):
            continue
        title = _summary_text(item.get("title")) or url
        candidates.append(
            (
                url,
                f"{title}: {url}",
                _summary_text(item.get("entity")),
            )
        )
        seen_urls.add(url)
    ordered: list[tuple[str, str, str]] = []
    for entity in required_entities or []:
        match = next(
            (
                candidate
                for candidate in candidates
                if candidate not in ordered and candidate[2] == entity
            ),
            None,
        )
        if match is not None:
            ordered.append(match)
    if official_only:
        for official_url in official_company_urls:
            match = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate not in ordered
                    and company_source_matches_official_url(
                        candidate[0],
                        official_url,
                    )
                ),
                None,
            )
            if match is not None:
                ordered.append(match)
    ordered.extend(candidate for candidate in candidates if candidate not in ordered)
    lines = [line for _url, line, _entity in ordered[: max(1, limit)]]
    return lines


def _official_company_urls_for_payload(
    payload: dict[str, Any],
    *,
    company: str,
    sources: Any,
) -> list[str]:
    entity_evidence = _verified_source_evidence_by_entity(payload)
    if entity_evidence:
        return list(
            dict.fromkeys(
                resolved
                for evidence in entity_evidence.values()
                if (resolved := _summary_text(evidence.get("resolved_official_url")))
            )
        )
    urls: list[str] = []
    retrieval = payload.get("retrieval")
    if isinstance(retrieval, dict):
        retrieval_lanes = [retrieval]
        retrieval_lanes.extend(
            lane
            for key in ("primary", "comparison")
            if isinstance((lane := retrieval.get(key)), dict)
        )
        urls.extend(
            resolved
            for lane in retrieval_lanes
            if (resolved := _summary_text(lane.get("resolved_company_url")))
        )
    entities = [
        str(item).strip()
        for item in payload.get("comparison_entities") or []
        if str(item).strip()
    ]
    if not urls:
        entities = entities or [company]
        for entity in entities:
            inferred = infer_official_company_url(
                company=entity,
                search_results=sources if isinstance(sources, list) else [],
            )
            if inferred:
                urls.append(inferred)
    return list(dict.fromkeys(urls))


def _verified_source_evidence_by_entity(
    payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    entries = payload.get("verified_source_evidence")
    if not isinstance(entries, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        entity = _summary_text(item.get("entity"))
        if entity:
            result[entity] = item
    return result


def _verified_sources_for_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for entity, evidence in _verified_source_evidence_by_entity(payload).items():
        for item in evidence.get("sources") or []:
            if not isinstance(item, dict):
                continue
            canonical = _canonical_source_url(item.get("url"))
            if not canonical or canonical in seen_urls:
                continue
            sources.append({**item, "entity": entity})
            seen_urls.add(canonical)
    return sources


def _canonical_source_url(value: Any) -> str:
    raw = _summary_text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().removeprefix("www.")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit(
        (
            parsed.scheme.lower(),
            f"{host}{port}",
            parsed.path.rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _format_structured_bullet_answer(answer: str, *, strip_urls: bool) -> str:
    visible = str(answer or "").strip()
    if strip_urls:
        visible = re.sub(
            r"\[([^\]]+)\]\(https?://[^)\s]+\)",
            r"\1",
            visible,
            flags=re.I,
        )
        visible = re.sub(r"https?://[^\s)>]+", "", visible, flags=re.I)
        visible = re.sub(r"\(\s*\)|\[\s*\]", "", visible)
        visible = re.sub(r"[ \t]+([.,;:!?])", r"\1", visible)
        visible = re.sub(r"[ \t]{2,}", " ", visible)
    formatted: list[str] = []
    for line in visible.splitlines():
        match = re.match(r"^\s*[-*]\s+([^:\n]{1,60}):\s*(.+)$", line)
        if match:
            formatted.append(f"- *{match.group(1).strip()}:* {match.group(2).strip()}")
        else:
            formatted.append(line.rstrip())
    return "\n".join(formatted).strip()


def _truncate_summary(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    window = clean[:limit]
    sentence_ends = [window.rfind(marker) for marker in (". ", "? ", "! ")]
    sentence_end = max(sentence_ends)
    if sentence_end >= max(40, limit // 3):
        return window[: sentence_end + 1].rstrip()
    word_end = window.rfind(" ")
    if word_end > 0:
        window = window[:word_end]
    return window.rstrip(" ,;:-") + "…"


def _save_retrieval_tool_performance_memory(
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> None:
    if not args.save:
        return
    retrieval = payload.get("retrieval")
    if not isinstance(retrieval, dict):
        return
    item = retrieval_tool_performance_memory_item(
        retrieval,
        object_id=f"company_research:{args.company}",
    )
    if item is None:
        return
    storage = StorageTool(args.database_url, agent_name="memory")
    saved = storage.save_memory_item(item.model_dump(mode="json"))
    payload.setdefault("storage", {})
    if isinstance(payload["storage"], dict):
        payload["storage"]["retrieval_tool_performance_memory_id"] = saved["id"]


def _write_result_output_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _persist_requested_result(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if args.result_output is not None:
        _write_result_output_atomic(args.result_output, payload)


@with_cli_environment()
def main() -> int:
    raw_argv = sys.argv[1:]
    args = _apply_manual_request_plan(
        _apply_fixture_safety_defaults(
            _apply_live_test_defaults(build_parser().parse_args(raw_argv)),
            raw_argv,
        )
    )
    args = _apply_interpreted_retrieval_mode(args)
    args = _apply_local_persistence_boundary(args)

    if args.improvement_case and not sdk_execution_requested(args):
        raise SystemExit("--improvement-case requires --run-sdk or --live-sdk.")
    if args.focused_brief and not sdk_execution_requested(args):
        raise SystemExit("--focused-brief requires --run-sdk or --live-sdk.")
    direct_request_owned_target = bool(
        sdk_execution_requested(args)
        and args.live_search
        and str(args.request_text or "").strip()
    )
    if not args.company and not direct_request_owned_target:
        raise SystemExit(
            "--company is required unless --request-text can identify a research target."
        )

    if sdk_execution_requested(args):
        try:
            payload = _run_sdk_synthesis(args)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        _attach_local_persistence_boundary(payload, args)
        _persist_requested_result(args, payload)
        if args.markdown and not args.json:
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            if payload.get("output_type") == "CompanyResearchFocusedBrief":
                report = render_company_focused_brief(
                    CompanyResearchFocusedBrief.model_validate(payload["output"])
                )
                print("\n\n".join(item for item in (report, review_markdown) if item))
            elif payload.get("output_type") == "CompanyResearchComparison":
                report = render_company_comparison_report(payload["output"])
                print("\n\n".join(item for item in (report, review_markdown) if item))
            else:
                lines = [
                    "# Business Research Analyst SDK Synthesis",
                    "",
                    f"Agent: {payload['agent_name']}",
                    f"Live SDK: {str(payload['live_sdk']).lower()}",
                    "SDK run invoked: true",
                ]
                if review_markdown:
                    lines.extend(["", review_markdown])
                print("\n".join(lines))
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    agent_descriptor = (
        sdk_agent_description(build_business_research_analyst_agent()) if args.sdk else None
    )

    if args.compare_company:
        try:
            comparison, retrieval, primary_profile, comparison_profile = (
                _retrieve_company_comparison(args)
            )
        except (
            RuntimeError,
            SearchProviderConfigurationError,
            SearchProviderError,
            ValueError,
        ) as exc:
            raise SystemExit(str(exc)) from exc
        payload = comparison.model_dump(mode="json")
        payload["retrieval"] = retrieval
        if isinstance(retrieval, dict):
            primary = retrieval.get("primary")
            if isinstance(primary, dict):
                payload["retrieval_diagnostics"] = primary.get("retrieval_diagnostics")
        if getattr(args, "manual_request_plan", None):
            payload["manual_request_plan"] = args.manual_request_plan
        attach_orchestrator_preflight_payload(payload, args)
        if agent_descriptor is not None:
            payload["agent"] = agent_descriptor
        if args.orchestrator_review:
            payload["orchestrator_review"] = build_cli_orchestrator_review(
                args,
                run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
                agent_name="business_research_analyst",
                output=comparison,
                request_summary=_orchestrator_review_request_summary(
                    args,
                    fallback=f"{args.company} vs {args.compare_company}",
                ),
                run_type="live search" if args.live_search else "deterministic fixture",
            )
        if args.save:
            storage = StorageTool(args.database_url)
            payload["storage"] = {
                "company_a": storage.save_company(primary_profile),
                "company_b": storage.save_company(comparison_profile),
                "agent_run": storage.save_agent_run(
                    agent_name="business_research_analyst",
                    input_payload={
                        "company": args.company,
                        "company_url": args.company_url,
                        "fixture": args.fixture,
                        "compare_company": args.compare_company,
                        "compare_company_url": args.compare_company_url,
                        "compare_fixture": args.compare_fixture,
                        "decision_criteria": args.decision_criteria,
                        "output_format": args.output_format,
                        "strict_format": args.strict_format,
                        "live_search": args.live_search,
                        "search_provider": args.search_provider,
                        "request_text": args.request_text,
                        "manual_request_plan": getattr(args, "manual_request_plan", None),
                    },
                    input_summary=f"{args.company} vs {args.compare_company}",
                    output=payload,
                    model="fixture" if not args.live_search else "live-search",
                    dry_run=not args.live_search,
                    status="success",
                ),
            }
        _attach_local_persistence_boundary(payload, args)
        _persist_requested_result(args, payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            report = company_research_comparison_markdown(
                comparison,
                output_format=args.output_format,
                strict_format=args.strict_format,
            )
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            print("\n\n".join(item for item in (report, review_markdown) if item))
            if args.save:
                print(f"\nSaved: {payload['storage']}")
        return 0

    try:
        profile, retrieval = _retrieve_company_profile(args)
    except (RuntimeError, SearchProviderConfigurationError, SearchProviderError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    payload = profile.model_dump()
    payload["retrieval"] = retrieval
    payload["retrieval_diagnostics"] = retrieval.get("retrieval_diagnostics")
    if getattr(args, "manual_request_plan", None):
        payload["manual_request_plan"] = args.manual_request_plan
    attach_orchestrator_preflight_payload(payload, args)
    if args.output_format:
        payload["requested_output_format"] = args.output_format
    if agent_descriptor is not None:
        payload["agent"] = agent_descriptor
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="business_research_analyst",
            output=profile,
            request_summary=_orchestrator_review_request_summary(
                args,
                fallback=args.company,
            ),
            run_type="live search" if args.live_search else "deterministic fixture",
        )
    if args.save:
        storage = StorageTool(args.database_url)
        payload["storage"] = {
            "company": storage.save_company(profile),
            "agent_run": storage.save_agent_run(
                agent_name="business_research_analyst",
                input_payload={
                    "company": args.company,
                    "company_url": args.company_url,
                    "lead_name": args.lead_name,
                    "linkedin_url": args.linkedin_url,
                    "fixture": args.fixture,
                    "output_format": args.output_format,
                    "strict_format": args.strict_format,
                    "live_search": args.live_search,
                    "search_provider": args.search_provider,
                    "request_text": args.request_text,
                    "manual_request_plan": getattr(args, "manual_request_plan", None),
                },
                input_summary=args.company,
                output=payload,
                model="fixture" if not args.live_search else "live-search",
                dry_run=not args.live_search,
                status="success",
            ),
        }
    _attach_local_persistence_boundary(payload, args)
    _persist_requested_result(args, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if args.output_format:
            report = company_profile_markdown(
                profile,
                output_format=args.output_format,
                strict_format=args.strict_format,
            )
        else:
            report = (
                render_company_profile_report(profile)
                if args.markdown
                else company_profile_markdown(profile)
            )
        review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
        print("\n\n".join(item for item in (report, review_markdown) if item))
        if args.save:
            print(f"\nSaved: {payload['storage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
