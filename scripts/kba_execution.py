"""Inspect or resume a local KBA execution; start fixture-only diagnostic graphs."""

from __future__ import annotations

import argparse
import json

from keystone_agents.orchestration.checkpoints import (
    fork_graph_checkpoint,
    inspect_graph_execution,
    resume_graph_execution,
)
from keystone_agents.runtime.durable_execution import (
    ExecutionConflict,
    ExecutionStore,
    execution_database_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", required=True, help="Exact local business SQLite database."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    journal = commands.add_parser(
        "journal", help="Read budget and operation evidence without a graph."
    )
    journal.add_argument("execution_id")
    start = commands.add_parser("start", help="Start a no-model/no-provider fixture graph.")
    start.add_argument("--request", required=True)
    start.add_argument("--execution-id", default="")
    start.add_argument("--origin-event-id", default="")
    start.add_argument("--manager-loop", action="store_true")
    start.add_argument("--max-steps", type=int, default=3)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("execution_id")
    inspect.add_argument(
        "--include-state", action="store_true",
        help="Include stored state; may contain private evidence.",
    )
    resume = commands.add_parser("resume")
    resume.add_argument("execution_id")
    resume.add_argument(
        "--approved", action="store_true", help="Require persisted approval; does not grant it."
    )
    resume.add_argument("--live-sdk", action="store_true")
    resume.add_argument("--live-search", action="store_true")
    fork = commands.add_parser(
        "fork", help="Export a no-write diagnostic branch for an isolated experiment."
    )
    fork.add_argument("execution_id")
    fork.add_argument("checkpoint_id")
    fork.add_argument("--request", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            output = ExecutionStore(
                execution_database_path(args.database_url), initialize=False
            ).list_executions()
        elif args.command == "journal":
            store = ExecutionStore(execution_database_path(args.database_url), initialize=False)
            row = store.get(args.execution_id)
            output = {
                "schema": row["schema_name"],
                "execution_id": row["id"],
                "work_item_id": row["work_item_id"],
                "status": row["status"],
                "budget_limit": row["budget_limit"],
                "consumed": row["consumed"],
                "operations": store.operations(row["id"]),
            }
        elif args.command == "inspect":
            output = inspect_graph_execution(
                args.execution_id, database_url=args.database_url, include_state=args.include_state
            )
        elif args.command == "resume":
            output = resume_graph_execution(
                args.execution_id,
                database_url=args.database_url,
                approved=args.approved,
                live_sdk=args.live_sdk,
                live_search=args.live_search,
            ).model_dump(mode="json")
        elif args.command == "fork":
            output = fork_graph_checkpoint(
                args.execution_id,
                args.checkpoint_id,
                database_url=args.database_url,
                changes={"request_text": args.request} if args.request else {},
            )
        else:
            from keystone_agents.langgraph_workflow import run_work_item_langgraph
            from keystone_agents.schemas.work_item import WorkflowRunRequest

            output = run_work_item_langgraph(
                WorkflowRunRequest(
                    request_text=args.request,
                    database_url=args.database_url,
                    execution_id=args.execution_id,
                    origin_event_id=args.origin_event_id,
                    model_request_limit=0,
                ),
                require_langgraph=True,
                enable_interrupts=True,
                manager_loop=args.manager_loop,
                max_manager_steps=args.max_steps,
            ).model_dump(mode="json")
        print(json.dumps(output, ensure_ascii=True, indent=2))
        return 0
    except (ExecutionConflict, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
