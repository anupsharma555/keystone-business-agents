"""Route a request and execute the routed search specialist in fixture or live-search mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.cli_orchestrator_review import (
    add_orchestrator_review_arguments,
    build_cli_orchestrator_review,
)
from keystone_agents.cli_sdk import SDKRunConfigFactory
from keystone_agents.config import (
    cli_default_dry_run,
    cli_default_live_research,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.reporting import (
    render_orchestrated_search_handoff_report,
    render_orchestrator_output_review,
)
from keystone_agents.tools.serper_tool import SearchProviderName
from keystone_agents.workflows import run_orchestrated_search_handoff

ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route a request and execute the search-oriented specialist path."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Request text or a local fixture path containing the request.",
    )
    parser.add_argument("--company-name", default=None, help="Optional company name override.")
    parser.add_argument("--company-url", default=None, help="Optional company URL override.")
    parser.add_argument("--topic", default=None, help="Optional scouting topic override.")
    parser.add_argument(
        "--live-search",
        action="store_true",
        default=cli_default_live_research(),
        help="Use live search for routed company research or opportunity scouting.",
    )
    parser.add_argument(
        "--search-provider",
        choices=[provider.value for provider in SearchProviderName],
        default=None,
        help="Live search provider override.",
    )
    parser.add_argument(
        "--fallback-search-provider",
        choices=[
            provider.value
            for provider in SearchProviderName
            if provider != SearchProviderName.DRY_RUN
        ],
        default=None,
        help="Optional fallback provider for routed live opportunity scouting.",
    )
    parser.add_argument("--max-results", type=int, default=5, help="Maximum returned results.")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=cli_default_dry_run(),
        help=(
            "Use deterministic fixture mode by default. Pass --no-dry-run with --live-search "
            "to allow live network retrieval."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    add_orchestrator_review_arguments(parser)
    return parser


def _read_input(value: str) -> str:
    if not value:
        return ""
    if len(value) < 240 and "\n" not in value:
        try:
            path = Path(value)
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            pass
    return value


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    input_text = _read_input(args.input)
    if args.live_search:
        try:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-search",
                live_action="routed live search handoff",
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-search.")

    result = run_orchestrated_search_handoff(
        input_text,
        company_name=args.company_name,
        company_url=args.company_url,
        topic=args.topic,
        live_search=args.live_search,
        max_results=args.max_results,
        search_provider=args.search_provider,
        fallback_search_provider=args.fallback_search_provider,
    )
    payload = result.model_dump(mode="json")
    if (
        args.orchestrator_review
        and result.specialist_executed
        and result.specialist_output is not None
    ):
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name=result.specialist_route or "search_handoff",
            output=result.specialist_output,
            request_summary=input_text,
            run_type="live search handoff" if args.live_search else "fixture search handoff",
        )

    if args.markdown and not args.json:
        report = render_orchestrated_search_handoff_report(payload)
        review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
        print("\n\n".join(item for item in (report, review_markdown) if item))
    else:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
