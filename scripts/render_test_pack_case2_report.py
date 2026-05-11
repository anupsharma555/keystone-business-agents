"""Render XX-2 agent-improvement test-pack reports from saved agent JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from keystone_agents.reporting import (
    TEST_PACK_CASE_SPECS,
    build_test_pack_case_payload,
    render_test_pack_case_report,
    render_test_pack_slack_text,
    to_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render documented Keystone agent-improvement test-pack report artifacts."
    )
    parser.add_argument(
        "--spec-id",
        required=True,
        choices=sorted(TEST_PACK_CASE_SPECS),
        help="Test-pack case id.",
    )
    parser.add_argument(
        "--output-json",
        required=True,
        help="Path to a JSON file containing the agent output or a CLI payload with output.",
    )
    parser.add_argument(
        "--report-dir",
        required=True,
        help="Directory where markdown, JSON, and Slack text artifacts should be written.",
    )
    parser.add_argument(
        "--run-type",
        default="live SDK",
        help="Run type label, for example 'live SDK' or 'live SDK + live search'.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Provider/model label such as openai/gpt-4.1-mini or gemini/gemini-2.5-flash.",
    )
    parser.add_argument(
        "--input-summary",
        default=None,
        help="Optional human-readable request summary. Defaults to the test-pack prompt.",
    )
    parser.add_argument(
        "--input-source",
        default="live agent JSON output",
        help="Short description of fixture, live-search, Gmail, or approved-context source.",
    )
    return parser


def _load_json(path_value: str) -> dict[str, Any]:
    data = json.loads(Path(path_value).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("--output-json must contain a JSON object.")
    return data


def _agent_output(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("output", "result", "specialist_output"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _metadata(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _model_label(payload: dict[str, Any], explicit: str | None) -> Any:
    if explicit:
        return explicit
    return payload.get("model") or payload.get("provider_model") or "unknown"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_payload = _load_json(args.output_json)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    spec = args.spec_id.lower()
    model = _model_label(raw_payload, args.model)
    command_args = raw_payload.get("command_args")
    report_payload = build_test_pack_case_payload(
        args.spec_id,
        _agent_output(raw_payload),
        run_type=args.run_type,
        model=model or "unknown",
        input_summary=args.input_summary,
        input_source=args.input_source,
        command_args=command_args if isinstance(command_args, list) else [],
        usage=_metadata(raw_payload, "usage"),
        cost=_metadata(raw_payload, "cost"),
        gemini_free_tier_usage=_metadata(raw_payload, "gemini_free_tier_usage"),
        orchestrator_review=_metadata(raw_payload, "orchestrator_review"),
    )
    json_path = report_dir / f"{spec}.json"
    metadata_path = report_dir / f"{spec}.metadata.json"
    markdown_path = report_dir / f"{spec}.md"
    slack_path = report_dir / f"{spec}.slack.txt"
    artifact_outputs = {
        "metadata_json": {
            "tier": "metadata.json",
            "label": "metadata JSON",
            "path": str(metadata_path),
        },
        "human_markdown": {
            "tier": "human markdown",
            "label": "human-readable markdown",
            "path": str(markdown_path),
        },
        "slack_summary": {
            "tier": "slack summary",
            "label": "compact Slack summary",
            "path": str(slack_path),
        },
    }
    report_payload["artifact_outputs"] = artifact_outputs
    json_path.write_text(to_json(report_payload) + "\n", encoding="utf-8")
    metadata_path.write_text(
        to_json(
            {
                "spec_id": report_payload["spec_id"],
                "status": report_payload["status"],
                "review_state": report_payload.get("review_state", {}),
                "artifact_outputs": artifact_outputs,
                "payload_json_path": str(json_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    markdown = render_test_pack_case_report(report_payload)
    markdown_path.write_text(markdown + "\n", encoding="utf-8")
    slack_text = render_test_pack_slack_text(report_payload)
    slack_path.write_text(slack_text + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "spec_id": args.spec_id,
                "status": report_payload["status"],
                "json_path": str(json_path),
                "metadata_path": str(metadata_path),
                "markdown_path": str(markdown_path),
                "slack_path": str(slack_path),
                "human_markdown_path": str(markdown_path),
                "slack_summary_path": str(slack_path),
                "artifact_outputs": artifact_outputs,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
