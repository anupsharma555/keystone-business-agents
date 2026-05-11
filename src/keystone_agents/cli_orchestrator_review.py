"""CLI helpers for optional Orchestrator output review."""

from __future__ import annotations

import argparse
from typing import Any

from keystone_agents.agents.orchestrator import (
    review_specialist_output,
    review_specialist_output_llm,
)
from keystone_agents.cli_sdk import SDKRunConfigFactory


def add_orchestrator_review_arguments(parser: argparse.ArgumentParser) -> None:
    """Add shared output-review flags to specialist CLIs."""

    parser.add_argument(
        "--orchestrator-review",
        action="store_true",
        help=(
            "Attach Orchestrator output review for human readability, professional "
            "tone, structure, relevance, Keystone fit, and no-send boundaries."
        ),
    )
    parser.add_argument(
        "--orchestrator-review-mode",
        choices=["deterministic", "llm"],
        default="deterministic",
        help=(
            "Review mode. Deterministic is offline and default. LLM mode requires "
            "an injected fake/local review run_config in tests or --live-orchestrator-review."
        ),
    )
    parser.add_argument(
        "--live-orchestrator-review",
        action="store_true",
        help=(
            "Run live Orchestrator LLM output review. Requires orchestrator model "
            "credentials and does not enable provider side effects."
        ),
    )
    parser.add_argument(
        "--orchestrator-review-model",
        default=None,
        help="Optional model override for live/local Orchestrator LLM review.",
    )


def build_cli_orchestrator_review(
    args: argparse.Namespace,
    *,
    run_config_factory: SDKRunConfigFactory | None,
    agent_name: str,
    output: Any,
    request_summary: str,
    run_type: str,
) -> dict[str, Any] | None:
    """Return a JSON-ready Orchestrator review payload when requested."""

    if not getattr(args, "orchestrator_review", False):
        if getattr(args, "live_orchestrator_review", False):
            raise SystemExit("--live-orchestrator-review requires --orchestrator-review.")
        return None

    mode = str(getattr(args, "orchestrator_review_mode", "deterministic"))
    live_review = bool(getattr(args, "live_orchestrator_review", False))
    model = getattr(args, "orchestrator_review_model", None)

    if mode == "deterministic":
        if live_review:
            raise SystemExit("--live-orchestrator-review requires --orchestrator-review-mode llm.")
        return review_specialist_output(
            agent_name=agent_name,
            output=output,
            request_summary=request_summary,
            run_type=run_type,
        ).model_dump(mode="json")

    run_config = None
    if live_review:
        review_run_type = f"{run_type} + live orchestrator LLM review"
    else:
        if run_config_factory is None:
            raise SystemExit(
                "--orchestrator-review-mode llm requires --live-orchestrator-review "
                "or an injected fake/local Orchestrator review run_config."
            )
        run_config = run_config_factory()
        if run_config is None:
            raise SystemExit("Orchestrator review run_config factory returned no run_config.")
        review_run_type = f"{run_type} + local orchestrator LLM review"

    return review_specialist_output_llm(
        agent_name=agent_name,
        output=output,
        request_summary=request_summary,
        run_type=review_run_type,
        run_config=run_config,
        live=live_review,
        model=model,
        fallback_to_deterministic=False,
    ).model_dump(mode="json")
