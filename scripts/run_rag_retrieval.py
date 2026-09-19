#!/usr/bin/env python3
"""Run the RAG Retrieval Specialist in fixture or bounded live SDK mode."""

from __future__ import annotations

import argparse
import json

from keystone_agents.agents.rag_retrieval_specialist import (
    rag_retrieval_fixture,
    run_rag_retrieval_specialist_sdk,
)
from keystone_agents.config import load_settings
from keystone_agents.models import RAGRetrievalSDKInput


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query the configured hosted vector store through the RAG specialist."
    )
    parser.add_argument("query", help="Natural-language corpus query.")
    parser.add_argument(
        "--mode",
        choices=("auto", "single_article", "semantic_search", "hybrid"),
        default="auto",
    )
    parser.add_argument("--max-matches", type=int, default=6)
    parser.add_argument("--model", default=None)
    parser.add_argument("--live-sdk", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.max_matches <= 20:
        raise SystemExit("--max-matches must be between 1 and 20")
    load_settings(force_dotenv=True)
    if args.live_sdk:
        run = run_rag_retrieval_specialist_sdk(
            RAGRetrievalSDKInput(
                query=args.query,
                retrieval_mode=args.mode,
                max_matches=args.max_matches,
            ),
            live=True,
            model=args.model,
        )
        payload = {
            "agent_name": run.agent_name,
            "mode": "live-sdk",
            "output": run.output.model_dump(mode="json"),
            "usage": run.usage,
            "cost": run.cost,
            "budget_guard": run.budget_guard,
            "request_cache": run.request_cache,
            "execution_telemetry": run.execution_telemetry,
        }
    else:
        payload = {
            "agent_name": "rag_retrieval_specialist",
            "mode": "fixture",
            "output": rag_retrieval_fixture(args.query).model_dump(mode="json"),
        }
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str))
    else:
        print(payload["output"].get("answer") or payload["output"]["limitations"][0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
