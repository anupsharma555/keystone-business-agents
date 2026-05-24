"""Run company research."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
    build_business_research_analyst_comparison_agent,
    build_business_research_analyst_focused_brief_agent,
    build_company_research_queries,
    compare_company_profiles_for_decision,
    comparison_input_from_result,
    focused_brief_input_from_profile,
    research_account_from_search_results,
)
from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
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
from keystone_agents.live_retrieval import (
    company_research_request_text as _live_company_research_request_text,
)
from keystone_agents.live_retrieval import (
    retrieve_company_profile_live,
)
from keystone_agents.live_retrieval import (
    retrieval_diagnostics_from_metadata,
)
from keystone_agents.live_retrieval import (
    search_provider_label as _live_search_provider_label,
)
from keystone_agents.memory import retrieval_tool_performance_memory_item
from keystone_agents.models import (
    BusinessResearchComparisonSDKInput,
    BusinessResearchFocusedBriefSDKInput,
    BusinessResearchSDKInput,
)
from keystone_agents.reporting import (
    render_company_comparison_report,
    render_company_focused_brief,
    render_company_profile_report,
    render_orchestrator_output_review,
)
from keystone_agents.run import run_retrieved_sdk_synthesis
from keystone_agents.schemas.company_profile import (
    CompanyProfile,
    CompanyResearchComparison,
    CompanyResearchFocusedBrief,
)
from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.tools.serper_tool import (
    SearchProviderConfigurationError,
    SearchProviderError,
    SearchProviderName,
    build_search_provider,
)
from keystone_agents.tools.storage_tool import StorageTool

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
        action="store_true",
        default=live_search_default,
        help=(
            "Use live search through the configured provider. Supported in both "
            "standard company-profile mode and SDK BR-1 focused-brief mode. "
            "Requires --no-dry-run."
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
    request_text = str(args.request_text or "").strip()
    args.manual_request_plan = None
    if not request_text:
        return args
    if args.live_manual_plan:
        load_settings(force_dotenv=True)
    plan = resolve_manual_request_plan(
        request_text,
        requested_agent="business_research_analyst",
        live=bool(args.live_manual_plan),
    )
    args.manual_request_plan = plan.model_dump(mode="json")
    if plan.target_agent == "business_research_analyst" and plan.primary_target:
        args.company = plan.primary_target
    if plan.objective and not args.output_format:
        args.output_format = None
    return args


def _manual_plan_objective(args: argparse.Namespace) -> str:
    plan = getattr(args, "manual_request_plan", None)
    if isinstance(plan, dict):
        return str(plan.get("objective") or "").strip()
    return ""


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

    if args.live_search:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-search",
            live_action="live company research network search",
        )
        profile, metadata = retrieve_company_profile_live(
            company=resolved_company,
            company_url=resolved_company_url,
            requested_provider=args.search_provider,
            max_results=args.max_results,
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
    criteria = parse_company_research_comparison_criteria(args.decision_criteria)
    comparison = compare_company_profiles_for_decision(
        primary_profile,
        comparison_profile,
        decision_goal=(
            "Compare both companies as possible partners or advisory targets for Keystone."
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


def _run_sdk_synthesis(args: argparse.Namespace) -> dict[str, Any]:
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    comparison_requested = bool(args.compare_company)
    focused_brief_requested = (
        bool(args.focused_brief) or args.improvement_case == CR1_IMPROVEMENT_CASE_ID
    )
    founder_profile = load_founder_fit_profile(args.founder_fit_profile)
    founder_context = founder_search_context(founder_profile) if founder_profile else ""

    retrieval_state: dict[str, Any] = {}

    def retrieve() -> CompanyProfile | CompanyResearchComparison:
        if comparison_requested:
            comparison, metadata, _primary_profile, _comparison_profile = (
                _retrieve_company_comparison(args)
            )
            retrieval_state.clear()
            retrieval_state.update(metadata)
            return comparison

        profile, metadata = _retrieve_company_profile(args)
        retrieval_state.clear()
        retrieval_state.update(metadata)
        return profile

    def normalize_profile(profile: CompanyProfile) -> BusinessResearchSDKInput:
        return BusinessResearchSDKInput(
            company_name=args.company,
            company_url=args.company_url,
            lead_name=args.lead_name,
            linkedin_url=args.linkedin_url,
            context=_company_context(profile, founder_context),
            retrieval_hint=_explicit_retrieval_hint(args),
        )

    def normalize_brief(profile: CompanyProfile) -> BusinessResearchFocusedBriefSDKInput:
        manual_objective = _manual_plan_objective(args)
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
                    item for item in (typed_input.source_context, founder_context) if item
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
                    item for item in (typed_input.source_context, founder_context) if item
                ),
                retrieval_hint=typed_input.retrieval_hint,
            )
        return typed_input

    output_type: (
        type[CompanyProfile] | type[CompanyResearchFocusedBrief] | type[CompanyResearchComparison]
    )
    if comparison_requested:
        output_type = CompanyResearchComparison
        agent = build_business_research_analyst_comparison_agent()
        normalize = normalize_comparison
    elif focused_brief_requested:
        output_type = CompanyResearchFocusedBrief
        agent = build_business_research_analyst_focused_brief_agent()
        normalize = normalize_brief
    else:
        output_type = CompanyProfile
        agent = build_business_research_analyst_agent()
        normalize = normalize_profile
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
    if getattr(args, "manual_request_plan", None):
        payload["manual_request_plan"] = args.manual_request_plan
    _save_retrieval_tool_performance_memory(args, payload)
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="business_research_analyst",
            output=outcome.final_output,
            request_summary=(
                f"{args.company} vs {args.compare_company}"
                if comparison_requested
                else args.company
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


@with_cli_environment()
def main() -> int:
    args = _apply_manual_request_plan(_apply_live_test_defaults(build_parser().parse_args()))

    if args.improvement_case and not sdk_execution_requested(args):
        raise SystemExit("--improvement-case requires --run-sdk or --live-sdk.")
    if args.focused_brief and not sdk_execution_requested(args):
        raise SystemExit("--focused-brief requires --run-sdk or --live-sdk.")
    if not args.company:
        raise SystemExit(
            "--company is required unless --request-text can identify a research target."
        )

    if sdk_execution_requested(args):
        try:
            payload = _run_sdk_synthesis(args)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
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
        if agent_descriptor is not None:
            payload["agent"] = agent_descriptor
        if args.orchestrator_review:
            payload["orchestrator_review"] = build_cli_orchestrator_review(
                args,
                run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
                agent_name="business_research_analyst",
                output=comparison,
                request_summary=f"{args.company} vs {args.compare_company}",
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
            request_summary=args.company,
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
