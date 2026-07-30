"""Run Opportunity Scout with env-aware dry-run or live-test defaults."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from keystone_agents.agents.opportunity_scout import (
    apply_opportunity_scout_synthesis,
    build_opportunity_scout_agent,
    build_opportunity_scout_synthesis_agent,
    scout_opportunities_fixture,
)
from keystone_agents.agents.opportunity_search_planner import resolve_opportunity_search_plan
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
from keystone_agents.config import (
    cli_default_dry_run,
    cli_default_live_research,
    cli_default_live_sdk,
    load_settings,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.founder_profile import (
    DEFAULT_FOUNDER_FIT_PROFILE_PATH,
    founder_profile_audit_payload,
    founder_search_context,
    load_founder_fit_profile,
)
from keystone_agents.live_retrieval import (
    build_shared_search_provider_config,
    retrieval_diagnostics_from_metadata,
    run_opportunity_scout_live,
)
from keystone_agents.memory import retrieval_tool_performance_memory_item
from keystone_agents.models import OpportunityScoutSDKInput
from keystone_agents.orchestrator.preflight_context import (
    apply_orchestrator_preflight_to_args,
    attach_orchestrator_preflight_payload,
    orchestrator_preflight_context_text,
)
from keystone_agents.reporting import (
    render_opportunity_scout_report,
    render_orchestrator_output_review,
)
from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    assess_role_search_quality,
    derive_request_autonomy_hint,
)
from keystone_agents.run import run_retrieved_sdk_synthesis
from keystone_agents.schemas.opportunity import (
    OpportunityScoutResult,
    OpportunityScoutSynthesis,
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
OS1_IMPROVEMENT_CASE_ID = "os-1"
OS1_IMPROVEMENT_PROMPT = (
    "Find up to 5 active U.S.-based remote roles posted in the last 7 days for a "
    "physician-scientist with behavioral health, clinical research, and AI experience. "
    "Exclude AI tutor roles."
)
OS1_ACCEPTANCE_CRITERIA = (
    "Return 3 to 5 roles maximum only when enough strong matches exist.",
    "Show fit for each role.",
    "Do not pad weak matches to hit a target count.",
    "Preserve source attribution and recency evidence.",
)


def build_parser() -> argparse.ArgumentParser:
    dry_run_default = cli_default_dry_run()
    live_search_default = cli_default_live_research()
    parser = argparse.ArgumentParser(description="Run the Opportunity Scout agent.")
    parser.add_argument(
        "--fixture", default=None, help="Optional local opportunity fixture JSON path."
    )
    parser.add_argument("--topic", default=None, help="Optional topic filter.")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum records to return.")
    parser.add_argument(
        "--live-search",
        action=argparse.BooleanOptionalAction,
        default=live_search_default,
        help=(
            "Use live search through the configured provider instead of local fixture "
            "data. Use --no-live-search to force fixture-only SDK synthesis when the "
            "environment enables live research by default."
        ),
    )
    parser.add_argument(
        "--live-search-plan",
        action="store_true",
        help=(
            "Use a credential-gated LLM planning pass before live retrieval. If the "
            "planner is unavailable, Opportunity Scout falls back to the local "
            "structured planner."
        ),
    )
    parser.add_argument(
        "--search-provider",
        choices=[provider.value for provider in SearchProviderName],
        default=None,
        help="Live search provider. Defaults to SEARCH_PROVIDER; dry-run is not valid live.",
    )
    parser.add_argument(
        "--fallback-search-provider",
        choices=[
            provider.value
            for provider in SearchProviderName
            if provider not in {SearchProviderName.DRY_RUN, SearchProviderName.SERPER}
        ],
        default=None,
        help=(
            "Optional backup provider for OS-1 live search if the primary provider is "
            "missing configuration or returns a provider error."
        ),
    )
    parser.add_argument(
        "--founder-fit-profile",
        default=(
            str(DEFAULT_FOUNDER_FIT_PROFILE_PATH)
            if DEFAULT_FOUNDER_FIT_PROFILE_PATH.is_file()
            else None
        ),
        help=(
            "Approved founder-fit JSON profile for search query planning and "
            "opportunity-fit assessment. The repo-local reviewed profile is used "
            "automatically when present; clean checkouts continue without private "
            "documents content."
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save opportunities and audit records to SQLite.",
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    parser.add_argument(
        "--existing-state",
        default=None,
        help=(
            "Optional local JSON file or JSON string with existing opportunity pipeline state. "
            "When omitted with --save, existing SQLite opportunity rows are used for dedupe."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=dry_run_default,
        help="Use deterministic fixture mode. Defaults to KEYSTONE_DRY_RUN or true.",
    )
    parser.add_argument(
        "--improvement-case",
        choices=[OS1_IMPROVEMENT_CASE_ID],
        default=None,
        help=(
            "Run a documented agent-improvement acceptance case through SDK synthesis. "
            "Currently supports os-1 from docs/AGENT_IMPROVEMENT_TEST_PACK.md."
        ),
    )
    parser.add_argument("--sdk", action="store_true", help="Also construct the SDK agent.")
    add_sdk_run_arguments(parser)
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
    parser.add_argument(
        "--sandbox-search-review",
        action="store_true",
        help=(
            "Stage a sandbox second-pass review packet for Opportunity Scout live search. "
            "Preview only unless explicit execution is wired by the caller."
        ),
    )
    parser.add_argument(
        "--force-sandbox-search-review",
        action="store_true",
        help=(
            "Stage the sandbox second-pass review packet even when retrieval quality gates "
            "did not recommend it."
        ),
    )
    parser.add_argument(
        "--sandbox-search-review-artifact-root",
        default=None,
        help=(
            "Optional directory where staged sandbox search-review packets should be written. "
            "Defaults to a temporary run directory."
        ),
    )
    parser.add_argument(
        "--sandbox-search-review-web-search",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Enable or disable OpenAI hosted web_search in the sandbox second-pass review. "
            "Defaults to enabled when the sandbox review ladder step is activated."
        ),
    )
    parser.add_argument(
        "--sandbox-search-review-web-search-context",
        choices=["low", "medium", "high"],
        default=None,
        help="Context size for sandbox hosted web_search when enabled.",
    )
    parser.add_argument(
        "--sandbox-search-review-web-search-cached-only",
        action="store_true",
        help=(
            "When sandbox hosted web_search is enabled, request cached/indexed-only behavior "
            "instead of live external web access."
        ),
    )
    add_orchestrator_review_arguments(parser)
    return parser


def _apply_live_test_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Promote env-backed live test defaults for SDK-only scouting paths."""

    if (
        args.improvement_case == OS1_IMPROVEMENT_CASE_ID
        and not args.sdk
        and not sdk_execution_requested(args)
        and cli_default_live_sdk()
    ):
        args.live_sdk = True
    return args


def _opportunity_context(result: OpportunityScoutResult) -> str:
    payload = {
        "topic": result.topic,
        "records": result.records,
        "source_bundles": result.source_bundles,
        "filtered_candidates": result.filtered_candidates,
        "review_candidates": result.review_candidates,
        "source_quality_summary": result.source_quality_summary,
        "constraint_relaxation_suggestion": result.constraint_relaxation_suggestion,
        "audit_notes": result.audit_notes,
        "outreach_generated": result.outreach_generated,
    }
    return (
        "Approved local opportunity scout context:\n"
        f"{json.dumps(jsonable(payload), ensure_ascii=True, sort_keys=True)}"
    )


def _search_plan_for_run(args: argparse.Namespace) -> Any:
    return resolve_opportunity_search_plan(
        args.topic,
        desired_count=args.max_results,
        live=bool(getattr(args, "live_search_plan", False)),
        planner_context=orchestrator_preflight_context_text(args),
    )


def _existing_state_for_run(args: argparse.Namespace) -> Any:
    if args.existing_state:
        return args.existing_state
    if args.save:
        return StorageTool(args.database_url).list_records("opportunities")
    return None


def _load_role_source_fixture(path_value: str | None) -> list[dict[str, Any]]:
    if not path_value:
        return []
    data = json.loads(Path(path_value).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("roles", "records", "opportunities", "items", "hits"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
        return [data]
    raise ValueError("OS-1 role source fixture must be a JSON object or list.")


def _os1_role_search_queries() -> list[str]:
    return [
        (
            '"physician-scientist" remote behavioral health AI clinical research role '
            "posted last 7 days -tutor"
        ),
        (
            '"medical director" remote behavioral health AI clinical research '
            "United States posted last week -tutor"
        ),
        ('"clinical research" "behavioral health" AI remote physician role United States -tutor'),
        ('"psychiatry" "clinical AI" remote advisory medical role United States -tutor'),
    ]


def _scout_request_text(args: argparse.Namespace, *, os1_case: bool = False) -> str:
    if os1_case:
        return OS1_IMPROVEMENT_PROMPT
    topic = (args.topic or "Keystone-relevant business opportunities").strip()
    return (
        f"Find high-fit live opportunities for Keystone. Topic: {topic}. "
        "Prioritize current, well-sourced results and avoid padding weak matches."
    )


def _orchestrator_review_request_summary(
    args: argparse.Namespace,
    *,
    fallback: str,
) -> str:
    preflight = getattr(args, "orchestrator_preflight", None)
    if isinstance(preflight, dict):
        request_text = str(preflight.get("request_text") or "").strip()
        if request_text:
            return request_text
        memo = preflight.get("preflight_memo")
        if isinstance(memo, dict):
            raw_request = str(memo.get("raw_request") or "").strip()
            if raw_request:
                return raw_request
    plan = getattr(args, "manual_request_plan", None)
    if isinstance(plan, dict):
        objective = str(plan.get("objective") or "").strip()
        if objective:
            return objective
    return fallback


def _explicit_retrieval_hint(args: argparse.Namespace) -> RetrievalHint | None:
    raw_value = getattr(args, "retrieval_hint_json", None)
    if not raw_value:
        return None
    try:
        return RetrievalHint.model_validate_json(raw_value)
    except ValueError as exc:
        raise SystemExit(f"Invalid --retrieval-hint-json: {exc}") from exc


def _search_provider_label(metadata: dict[str, Any]) -> str:
    providers_used = [str(item) for item in (metadata.get("search_providers_used") or []) if item]
    if len(providers_used) > 1:
        return "+".join(providers_used)
    if providers_used:
        return providers_used[0]
    return str(metadata.get("primary_search_provider") or "")


def _build_hybrid_search_provider(
    args: argparse.Namespace,
    *,
    request_text: str,
    desired_results: int | None = None,
) -> HybridSearchProvider:
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="opportunity_scout",
        request_text=request_text,
        agent_hint=_explicit_retrieval_hint(args),
    )
    search_config = build_shared_search_provider_config(
        requested_provider=args.search_provider,
        configured_provider=load_settings().search_provider,
        fallback_provider=args.fallback_search_provider,
    )
    provider = HybridSearchProvider(
        provider_sequence=search_config.provider_sequence,
        deepening_provider_sequence=search_config.deepening_provider_sequence,
        autonomy_hint=autonomy_hint,
        quality_assessor=lambda results, _query: assess_role_search_quality(
            results=results,
            desired_results=desired_results or args.max_results,
            request_text=request_text,
            autonomy_hint=autonomy_hint,
        ),
        provider_factory=lambda provider_name: build_search_provider(
            provider=provider_name,
            live=True,
        ),
        parallel_provider_fanout=search_config.parallel_provider_fanout,
        provider_request_budget=search_config.provider_request_budget,
    )
    if len(search_config.provider_sequence) == 1:
        provider.validate_configuration()
    return provider


def _role_search_quality(
    args: argparse.Namespace,
    *,
    results: list[Any],
    request_text: str,
) -> dict[str, Any]:
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="opportunity_scout",
        request_text=request_text,
        agent_hint=_explicit_retrieval_hint(args),
    )
    return assess_role_search_quality(
        results=results,
        desired_results=args.max_results,
        request_text=request_text,
        autonomy_hint=autonomy_hint,
    ).to_dict()


def _retrieval_audit_notes(metadata: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    providers_used = metadata.get("search_providers_used") or []
    if providers_used:
        notes.append(f"Retrieval ladder used: {' -> '.join(str(item) for item in providers_used)}.")
    if metadata.get("precision_search_escalated"):
        notes.append("Precision search escalation was triggered after initial quality checks.")
    if metadata.get("provider_error_fallback_used"):
        notes.append("Retrieval recovered from a provider error by falling back to the next stage.")
    quality = metadata.get("search_quality") or {}
    reasons = quality.get("reasons") or metadata.get("quality_reason_hints") or []
    if reasons:
        notes.append(f"Retrieval quality notes: {', '.join(str(item) for item in reasons[:4])}.")
    if metadata.get("structured_enrichment_recommended"):
        notes.append(
            "Structured enrichment may help next: "
            f"{', '.join(metadata.get('structured_enrichment_candidates') or [])}."
        )
    if metadata.get("search_review_recommended"):
        notes.append("Sandbox second-pass review is recommended for the staged search packet.")
    return notes


def _search_role_sources_live(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    require_cli_live_confirmation(
        dry_run=args.dry_run,
        live_flag=args.live_search,
        flag_name="--live-search",
        live_action="live role posting search for Opportunity Scout OS-1",
    )
    request_text = _scout_request_text(args, os1_case=True)
    provider = _build_hybrid_search_provider(
        args,
        request_text=request_text,
        desired_results=max(2, args.max_results),
    )
    hits: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for query in _os1_role_search_queries():
        results = provider.search_web(
            query,
            num_results=max(1, args.max_results * 2),
        )
        for result in results:
            dumped = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            url = str(dumped.get("url") or dumped.get("link") or "").strip()
            dedupe_key = url.rstrip("/") or str(dumped.get("title") or "")
            if not dedupe_key or dedupe_key in seen_urls:
                continue
            seen_urls.add(dedupe_key)
            hits.append(
                {
                    "title": dumped.get("title", ""),
                    "url": url,
                    "snippet": dumped.get("snippet", ""),
                    "source": dumped.get("source", "search"),
                    "source_type": dumped.get("source_type", "job_posting"),
                    "query": query,
                }
            )

    metadata = provider.telemetry()
    metadata.update(
        {
            "search_provider": _search_provider_label(metadata),
            "fallback_search_provider": args.fallback_search_provider,
            "search_provider_fallback_used": metadata.get("provider_error_fallback_used", False),
            "search_quality": _role_search_quality(
                args,
                results=hits,
                request_text=request_text,
            ),
        }
    )
    return hits, metadata


def _retrieve_os1_role_context(args: argparse.Namespace) -> dict[str, Any]:
    role_sources = _load_role_source_fixture(args.fixture)
    retrieval_mode = "fixture"
    search_metadata: dict[str, Any] = {}
    founder_profile = load_founder_fit_profile(args.founder_fit_profile)
    founder_context = founder_search_context(founder_profile) if founder_profile else ""
    if not role_sources and args.live_search:
        role_sources, search_metadata = _search_role_sources_live(args)
        retrieval_mode = "live_search"
    return {
        "case_id": OS1_IMPROVEMENT_CASE_ID,
        "source": "docs/AGENT_IMPROVEMENT_TEST_PACK.md",
        "prompt": OS1_IMPROVEMENT_PROMPT,
        "acceptance_criteria": list(OS1_ACCEPTANCE_CRITERIA),
        "retrieval_mode": retrieval_mode if role_sources else "no_source_context",
        "search_metadata": search_metadata,
        "founder_fit_profile_context": founder_context,
        "candidate_role_sources": role_sources,
        "instructions": (
            "Use the LLM to synthesize the result from source context. Return no records "
            "when source evidence is insufficient. Do not rely on deterministic fixture "
            "ranking, and do not pad weak matches."
        ),
        "outbound_side_effects_enabled": False,
    }


def _run_sdk_synthesis(args: argparse.Namespace) -> dict[str, Any]:
    if args.live_search:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-search",
            live_action="read-only opportunity scouting retrieval before SDK synthesis",
        )
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    retrieval_metadata: dict[str, Any] = {}
    founder_profile = load_founder_fit_profile(args.founder_fit_profile)
    founder_context = founder_search_context(founder_profile) if founder_profile else ""
    preflight_context = orchestrator_preflight_context_text(args)

    def retrieve() -> Any:
        if args.improvement_case == OS1_IMPROVEMENT_CASE_ID:
            context = _retrieve_os1_role_context(args)
            retrieval_metadata.update(context.get("search_metadata") or {})
            return context
        if args.live_search:
            result, metadata = run_opportunity_scout_live(
                topic=args.topic,
                max_results=args.max_results,
                requested_provider=args.search_provider,
                fallback_provider=args.fallback_search_provider,
                retrieval_hint=_explicit_retrieval_hint(args),
                search_plan=_search_plan_for_run(args),
                save=False,
                existing_state=_existing_state_for_run(args),
                settings_loader=load_settings,
                search_provider_builder=build_search_provider,
                sandbox_search_review=args.sandbox_search_review,
                force_sandbox_search_review=args.force_sandbox_search_review,
                sandbox_search_review_artifact_root=args.sandbox_search_review_artifact_root,
                sandbox_search_review_hosted_web_search=args.sandbox_search_review_web_search,
                sandbox_search_review_hosted_web_search_external_web_access=(
                    not args.sandbox_search_review_web_search_cached_only
                ),
                sandbox_search_review_hosted_web_search_context_size=(
                    args.sandbox_search_review_web_search_context
                ),
            )
            retrieval_metadata.update(metadata or {})
            return result
        return scout_opportunities_fixture(
            fixture=Path(args.fixture) if args.fixture else None,
            topic=args.topic,
            max_results=args.max_results,
            dry_run=True,
            save=False,
        )

    def normalize(result: Any) -> OpportunityScoutSDKInput:
        if args.improvement_case == OS1_IMPROVEMENT_CASE_ID:
            context = (
                "Opportunity Scout OS-1 acceptance case context:\n"
                f"{json.dumps(jsonable(result), ensure_ascii=True, indent=2, sort_keys=True)}"
            )
            return OpportunityScoutSDKInput(
                topic=OS1_IMPROVEMENT_PROMPT,
                max_results=5,
                context="\n\n".join(item for item in (context, preflight_context) if item),
                retrieval_hint=_explicit_retrieval_hint(args),
            )
        live_retrieval_context = ""
        if args.live_search and retrieval_metadata:
            ladder = retrieval_metadata.get("retrieval_ladder") or [{}]
            first_ladder = ladder[0] if isinstance(ladder, list) and ladder else {}
            live_retrieval_context = (
                "Live retrieval stage data checks and source candidates:\n"
                + json.dumps(
                    jsonable(
                        {
                            "raw_result_count": (
                                retrieval_metadata.get("raw_search_result_count")
                                or first_ladder.get("raw_result_count")
                            ),
                            "accepted_record_count_before_sdk": (
                                len(result.records)
                                if isinstance(result, OpportunityScoutResult)
                                else None
                            ),
                            "filtered_candidate_count_before_sdk": (
                                len(result.filtered_candidates)
                                if isinstance(result, OpportunityScoutResult)
                                else None
                            ),
                            "review_candidate_count_before_sdk": (
                                len(result.review_candidates)
                                if isinstance(result, OpportunityScoutResult)
                                else None
                            ),
                            "retrieval_ladder": retrieval_metadata.get("retrieval_ladder"),
                            "search_quality": retrieval_metadata.get("search_quality"),
                            "search_plan": retrieval_metadata.get("search_plan"),
                            "retrieved_source_candidates": retrieval_metadata.get(
                                "retrieved_source_candidates",
                                [],
                            ),
                            "synthesis_instruction": (
                                "If accepted records are empty but retrieved source candidates "
                                "contain relevant active opportunities, reason over those "
                                "candidates and return up to max_results source-backed records. "
                                "When search_plan.strict_targeting is true and target_entity_types "
                                "is only company, final records must be companies; keep agencies, "
                                "institutes, programs, projects, grants, trials, researchers, "
                                "conferences, and publication calls out of final records. "
                                "If review_candidates are present, treat them as borderline "
                                "active opportunities for orchestrator or Business Research "
                                "review, not as outreach-ready records. "
                                "If the candidates are too weak, return no records and explain "
                                "the missing evidence."
                            ),
                        }
                    ),
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
            )
        return OpportunityScoutSDKInput(
            topic=args.topic,
            max_results=args.max_results,
            context="\n\n".join(
                item
                for item in (
                    _opportunity_context(result),
                    live_retrieval_context,
                    founder_context,
                    preflight_context,
                )
                if item
            ),
            retrieval_hint=_explicit_retrieval_hint(args),
        )

    storage = StorageTool(args.database_url) if args.save else None
    prefetched_live_result = retrieve() if args.live_search and not args.improvement_case else None
    if _skip_synthesis_for_empty_live_retrieval(prefetched_live_result):
        assert isinstance(prefetched_live_result, OpportunityScoutResult)
        payload = _empty_live_retrieval_payload(
            result=prefetched_live_result,
            retrieval_metadata=retrieval_metadata,
            founder_profile=founder_profile,
            founder_profile_path=args.founder_fit_profile,
        )
        attach_orchestrator_preflight_payload(payload, args)
        return payload

    def retrieve_for_synthesis() -> Any:
        if prefetched_live_result is not None:
            return prefetched_live_result
        return retrieve()

    legacy_os1_mode = args.improvement_case == OS1_IMPROVEMENT_CASE_ID
    outcome = run_retrieved_sdk_synthesis(
        agent=(
            build_opportunity_scout_agent(
                attach_tools=False,
                compact_instructions=args.compact_instructions,
            )
            if legacy_os1_mode
            else build_opportunity_scout_synthesis_agent(max_results=args.max_results)
        ),
        output_type=OpportunityScoutResult if legacy_os1_mode else OpportunityScoutSynthesis,
        retrieve=retrieve_for_synthesis,
        normalize=normalize,
        finalize_output=None if legacy_os1_mode else apply_opportunity_scout_synthesis,
        input_summary=args.topic or "opportunity scout SDK synthesis",
        input_audit_payload={
            "fixture": args.fixture,
            "topic": args.topic,
            "max_results": args.max_results,
            "sdk_synthesis": True,
            "improvement_case": args.improvement_case,
            "live_search": args.live_search,
            "search_provider": args.search_provider,
            "fallback_search_provider": args.fallback_search_provider,
            "retrieval_hint": (
                _explicit_retrieval_hint(args).model_dump(mode="json")
                if _explicit_retrieval_hint(args) is not None
                else None
            ),
            "founder_fit_profile": founder_profile_audit_payload(
                args.founder_fit_profile,
                founder_profile,
            ),
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
    payload["founder_fit_profile"] = founder_profile_audit_payload(
        args.founder_fit_profile,
        founder_profile,
    )
    if retrieval_metadata:
        payload["live_search_metadata"] = retrieval_metadata
        payload["retrieval_diagnostics"] = retrieval_metadata.get(
            "retrieval_diagnostics",
            retrieval_diagnostics_from_metadata(retrieval_metadata),
        )
    attach_orchestrator_preflight_payload(payload, args)
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="opportunity_scout",
            output=outcome.final_output,
            request_summary=_orchestrator_review_request_summary(
                args,
                fallback=args.topic or OS1_IMPROVEMENT_PROMPT,
            ),
            run_type="live SDK" if live else "local SDK",
        )
    return payload


def _skip_synthesis_for_empty_live_retrieval(result: Any) -> bool:
    return bool(
        isinstance(result, OpportunityScoutResult)
        and result.raw_search_result_count == 0
        and not result.records
        and not result.review_candidates
    )


def _empty_live_retrieval_payload(
    *,
    result: OpportunityScoutResult,
    retrieval_metadata: dict[str, Any],
    founder_profile: Any,
    founder_profile_path: str,
) -> dict[str, Any]:
    return {
        "mode": "sdk-synthesis",
        "agent_name": "opportunity_scout",
        "dry_run": False,
        "live_sdk": False,
        "live_sdk_requested": True,
        "sdk_run_invoked": False,
        "synthesis_skipped_reason": "live_retrieval_returned_zero_raw_results",
        "model": {"provider": "", "name": "", "run_mode": "not_invoked"},
        "usage": {
            "available": True,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        },
        "cost": {
            "source": "not_incurred",
            "estimated_usd": 0.0,
            "actual_usd": 0.0,
            "currency": "USD",
            "note": "Synthesis was skipped because live retrieval returned no raw evidence.",
        },
        "request_cache": {},
        "budget_guard": {
            "status": "not_needed",
            "estimated_usd": 0.0,
            "exceeded": False,
        },
        "output_type": "OpportunityScoutResult",
        "output": jsonable(result),
        "storage": {},
        "audit_notes": [
            "Live retrieval completed with zero raw results.",
            "SDK synthesis was skipped to avoid spending on an empty evidence packet.",
            "No outbound side effects were invoked.",
        ],
        "founder_fit_profile": founder_profile_audit_payload(
            founder_profile_path,
            founder_profile,
        ),
        "live_search_metadata": retrieval_metadata,
        "retrieval_diagnostics": retrieval_metadata.get(
            "retrieval_diagnostics",
            retrieval_diagnostics_from_metadata(retrieval_metadata),
        ),
    }


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
    args = apply_orchestrator_preflight_to_args(
        _apply_live_test_defaults(build_parser().parse_args())
    )

    if args.improvement_case and not sdk_execution_requested(args):
        raise SystemExit("--improvement-case requires --run-sdk or --live-sdk.")
    if (args.sandbox_search_review or args.force_sandbox_search_review) and not args.live_search:
        raise SystemExit(
            "Sandbox search review requires --live-search because it stages live retrieval packets."
        )
    if args.sandbox_search_review_web_search and not (
        args.sandbox_search_review or args.force_sandbox_search_review
    ):
        raise SystemExit(
            "--sandbox-search-review-web-search requires --sandbox-search-review "
            "or --force-sandbox-search-review."
        )
    if (
        args.sandbox_search_review_web_search_cached_only
        and args.sandbox_search_review_web_search is False
    ):
        raise SystemExit(
            "--sandbox-search-review-web-search-cached-only cannot be used when "
            "--no-sandbox-search-review-web-search is set."
        )

    if sdk_execution_requested(args):
        try:
            payload = _run_sdk_synthesis(args)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        _persist_requested_result(args, payload)
        if args.markdown and not args.json:
            lines = [
                "# Opportunity Scout SDK Synthesis",
                "",
                f"Agent: {payload['agent_name']}",
                f"Live SDK: {str(payload['live_sdk']).lower()}",
                "SDK run invoked: true",
            ]
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            if review_markdown:
                lines.extend(["", review_markdown])
            print("\n".join(lines))
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    agent_descriptor = sdk_agent_description(build_opportunity_scout_agent()) if args.sdk else None

    try:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=args.live_search,
            flag_name="--live-search",
            live_action="live opportunity scouting network search",
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    existing_state = _existing_state_for_run(args)
    retrieval_metadata: dict[str, Any] = {}
    if args.live_search:
        try:
            result, retrieval_metadata = run_opportunity_scout_live(
                topic=args.topic,
                max_results=args.max_results,
                requested_provider=args.search_provider,
                fallback_provider=args.fallback_search_provider,
                retrieval_hint=_explicit_retrieval_hint(args),
                search_plan=_search_plan_for_run(args),
                save=args.save,
                existing_state=existing_state,
                settings_loader=load_settings,
                search_provider_builder=build_search_provider,
                sandbox_search_review=args.sandbox_search_review,
                force_sandbox_search_review=args.force_sandbox_search_review,
                sandbox_search_review_artifact_root=args.sandbox_search_review_artifact_root,
                sandbox_search_review_hosted_web_search=args.sandbox_search_review_web_search,
                sandbox_search_review_hosted_web_search_external_web_access=(
                    not args.sandbox_search_review_web_search_cached_only
                ),
                sandbox_search_review_hosted_web_search_context_size=(
                    args.sandbox_search_review_web_search_context
                ),
            )
        except (SearchProviderConfigurationError, SearchProviderError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
    else:
        result = scout_opportunities_fixture(
            fixture=Path(args.fixture) if args.fixture else None,
            topic=args.topic,
            max_results=args.max_results,
            dry_run=args.dry_run,
            save=args.save,
            existing_state=existing_state,
        )
    payload = result.model_dump()
    if retrieval_metadata:
        payload["retrieval"] = retrieval_metadata
        payload["retrieval_diagnostics"] = retrieval_metadata.get(
            "retrieval_diagnostics",
            retrieval_diagnostics_from_metadata(retrieval_metadata),
        )
    attach_orchestrator_preflight_payload(payload, args)
    if agent_descriptor is not None:
        payload["agent"] = agent_descriptor
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="opportunity_scout",
            output=result,
            request_summary=_orchestrator_review_request_summary(
                args,
                fallback=args.topic or "opportunity scout fixture run",
            ),
            run_type="live search" if args.live_search else "deterministic fixture",
        )
    if args.save:
        storage = StorageTool(args.database_url)
        payload["storage"] = {
            "opportunities": storage.save_opportunity_scout_result(result),
            "agent_run": storage.save_agent_run(
                agent_name="opportunity_scout",
                input_payload={
                    "fixture": args.fixture,
                    "topic": args.topic,
                    "max_results": args.max_results,
                    "live_search": args.live_search,
                    "search_provider": args.search_provider,
                    "sandbox_search_review": args.sandbox_search_review,
                    "force_sandbox_search_review": args.force_sandbox_search_review,
                    "sandbox_search_review_web_search": args.sandbox_search_review_web_search,
                    "sandbox_search_review_web_search_context": (
                        args.sandbox_search_review_web_search_context
                    ),
                    "sandbox_search_review_web_search_cached_only": (
                        args.sandbox_search_review_web_search_cached_only
                    ),
                    "existing_state_supplied": existing_state is not None,
                },
                input_summary=args.topic or "opportunity scout fixture run",
                output=payload,
                model="fixture" if not args.live_search else "live-search",
                dry_run=not args.live_search,
                status="success",
            ),
        }
        if retrieval_metadata:
            item = retrieval_tool_performance_memory_item(
                retrieval_metadata,
                object_id=f"opportunity_scout:{args.topic or 'default'}",
            )
            if item is not None:
                saved = storage.save_memory_item(item.model_dump(mode="json"))
                payload["storage"]["retrieval_tool_performance_memory_id"] = saved["id"]
    if args.markdown and not args.json:
        _persist_requested_result(args, payload)
        report = render_opportunity_scout_report(result)
        review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
        print("\n\n".join(item for item in (report, review_markdown) if item))
        if args.save:
            print(f"\nSaved: {payload['storage']}")
    else:
        _persist_requested_result(args, payload)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
