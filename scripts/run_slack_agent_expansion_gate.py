"""Run the focused no-live Slack agent expansion gate.

This is a lightweight wrapper around the repo's Promptfoo dry-run provider and
Slack invariant assertion. It intentionally exercises only the 36-case
`slack_agent_expansion_15.yaml` pack unless another test file is explicitly
provided.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_FILE = PROJECT_ROOT / "promptfoo/tests/slack_agent_expansion_15.yaml"
PROVIDER_PATH = PROJECT_ROOT / "promptfoo/providers/keystone_agent_provider.py"
ASSERTION_PATH = PROJECT_ROOT / "promptfoo/assertions/kba_slack_invariants.py"
DEFAULT_ROUTE_MINIMUMS = {
    "airtable_context_agent": 2,
    "business_research_analyst": 5,
    "chief_of_staff": 11,
    "gmail_triage": 4,
    "google_workspace_context_agent": 2,
    "opportunity_scout": 4,
    "outreach_composer": 4,
    "preprints_context_agent": 1,
    "rss_context_agent": 1,
    "zotero_context_agent": 1,
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    test_file = _resolve_path(args.test_file)
    tests = _load_tests(test_file)
    provider = _load_module("kba_promptfoo_provider", PROVIDER_PATH)
    assertions = _load_module("kba_slack_invariants", ASSERTION_PATH)

    config = {
        "python": args.python,
        "agent": "orchestrator",
        "live_sdk": False,
        "live_search": False,
        "max_manager_steps": args.max_manager_steps,
        "timeout_seconds": args.timeout_seconds,
    }
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, case in enumerate(tests, start=1):
        vars_ = dict(case.get("vars") or {})
        case_id = str(vars_.get("case_id") or f"case_{index}")
        prompt = str(vars_.get("user_input") or "")
        result = provider.call_api(prompt, {"config": config}, {"vars": vars_})
        output = str(result.get("output") or "")
        grade = assertions.grade_output(output, {"vars": vars_})
        payload = _json_loads(output)
        row = {
            "case_id": case_id,
            "passed": bool(grade.get("pass")),
            "route": payload.get("route"),
            "status": payload.get("status"),
            "output_type": payload.get("output_type") or payload.get("context_pack_type"),
            "reason": grade.get("reason", ""),
        }
        rows.append(row)
        if not row["passed"]:
            failures.append(row)
        if not args.quiet:
            print(
                "{case_id}: {status_label} route={route} status={status} type={output_type}".format(
                    case_id=case_id,
                    status_label="PASS" if row["passed"] else "FAIL",
                    route=row["route"] or "",
                    status=row["status"] or "",
                    output_type=row["output_type"] or "",
                )
            )

    route_counts = _route_counts(rows)
    required_route_minimums = (
        DEFAULT_ROUTE_MINIMUMS
        if _uses_default_test_file(test_file) and not args.skip_route_coverage
        else {}
    )
    coverage_failures = _route_coverage_failures(route_counts, required_route_minimums)
    summary = {
        "test_file": str(test_file.relative_to(PROJECT_ROOT)),
        "cases": len(rows),
        "passed": len(rows) - len(failures),
        "failed": len(failures),
        "route_coverage_failed": len(coverage_failures),
        "live_sdk": False,
        "live_search": False,
        "route_counts": route_counts,
        "required_route_minimums": required_route_minimums,
        "route_coverage_failures": coverage_failures,
        "rows": rows,
    }
    if args.json_output:
        output_path = _resolve_path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    if coverage_failures:
        print(
            "Slack agent expansion gate: {passed}/{cases} case assertions passed, "
            "{failed} case failures, {route_coverage_failed} route coverage failures "
            "(no live Slack/API/model/search calls).".format(**summary)
        )
    else:
        print(
            "Slack agent expansion gate: {passed}/{cases} passed, {failed} failed "
            "(no live Slack/API/model/search calls).".format(**summary)
        )
    print("Route coverage: " + _format_route_counts(summary["route_counts"]))
    if failures:
        print("Failures:")
        for failure in failures:
            print(f"- {failure['case_id']}: {failure['reason']}")
    if coverage_failures:
        print("Route coverage failures:")
        for failure in coverage_failures:
            print(f"- {failure}")
    if failures or coverage_failures:
        return 1
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the focused no-live Slack agent expansion/readability gate."
    )
    parser.add_argument(
        "--test-file",
        default=str(DEFAULT_TEST_FILE.relative_to(PROJECT_ROOT)),
        help="Promptfoo YAML test file to run. Defaults to the 36-case expansion pack.",
    )
    parser.add_argument(
        "--python",
        default=".venv/bin/python",
        help="Python executable passed to the dry-run Promptfoo provider.",
    )
    parser.add_argument("--max-manager-steps", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--json-output", default="", help="Optional JSON summary output path.")
    parser.add_argument("--quiet", action="store_true", help="Print only the final summary.")
    parser.add_argument(
        "--skip-route-coverage",
        action="store_true",
        help="Do not enforce default route minimums for the built-in 36-case pack.",
    )
    return parser.parse_args(argv)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _load_tests(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"Expected a list of Promptfoo cases in {path}")
    return [case for case in data if isinstance(case, dict)]


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_loads(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _route_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        route = str(row.get("route") or "unknown")
        counts[route] += 1
    return dict(sorted(counts.items()))


def _route_coverage_failures(
    route_counts: dict[str, int], required_route_minimums: dict[str, int]
) -> list[str]:
    failures: list[str] = []
    for route, minimum in required_route_minimums.items():
        observed = int(route_counts.get(route) or 0)
        if observed < minimum:
            failures.append(f"{route}={observed}, expected >= {minimum}")
    return failures


def _format_route_counts(route_counts: dict[str, int]) -> str:
    if not route_counts:
        return "none"
    return ", ".join(f"{route}={count}" for route, count in route_counts.items())


def _uses_default_test_file(test_file: Path) -> bool:
    try:
        return test_file.resolve() == DEFAULT_TEST_FILE.resolve()
    except OSError:
        return test_file == DEFAULT_TEST_FILE


if __name__ == "__main__":
    raise SystemExit(main())
