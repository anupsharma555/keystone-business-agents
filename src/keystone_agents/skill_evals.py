"""Dry-run skill-selection eval harness for Keystone agents."""

from __future__ import annotations

import argparse
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.skill_sets import (
    AGENT_SKILL_NAMES,
    explain_agent_skill_selection,
    select_agent_skill_names,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILL_TASK_MATRIX = PROJECT_ROOT / "evals" / "local" / "skill_task_matrix.jsonl"
SUPPORTED_SURFACES = frozenset({"slack", "computer", "cli"})


@dataclass(frozen=True)
class SkillTaskEvalCase:
    dataset: str
    line_number: int
    case_id: str
    agent: str
    surface: str
    task: str
    request: str
    expected: Mapping[str, Any]
    context_flags: Mapping[str, bool]


@dataclass(frozen=True)
class SkillTaskEvalResult:
    dataset: str
    case_id: str
    agent: str
    surface: str
    task: str
    passed: bool
    failures: tuple[str, ...]
    selected_skills: tuple[str, ...]
    expected_skills: tuple[str, ...]
    excluded_skills: tuple[str, ...]
    selection_reasons: Mapping[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "id": self.case_id,
            "agent": self.agent,
            "surface": self.surface,
            "task": self.task,
            "passed": self.passed,
            "failures": list(self.failures),
            "selected_skills": list(self.selected_skills),
            "expected_skills": list(self.expected_skills),
            "excluded_skills": list(self.excluded_skills),
            "selection_reasons": {
                skill_name: list(reasons) for skill_name, reasons in self.selection_reasons.items()
            },
        }


@dataclass(frozen=True)
class SkillTaskEvalSummary:
    results: tuple[SkillTaskEvalResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "agents": sorted({result.agent for result in self.results}),
            "surfaces": sorted({result.surface for result in self.results}),
            "results": [result.to_dict() for result in self.results],
        }


def _load_jsonl(path: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL row: {exc}") from exc
        if not isinstance(row, Mapping):
            raise ValueError(f"{path}:{line_number}: row must be a JSON object")
        rows.append({"__line_number": line_number, **row})
    return rows


def load_skill_task_eval_cases(
    path: str | Path = DEFAULT_SKILL_TASK_MATRIX,
    *,
    surfaces: Sequence[str] = (),
) -> tuple[SkillTaskEvalCase, ...]:
    """Load dry-run skill task cases from JSONL."""

    dataset_path = Path(path)
    surface_filter = frozenset(surfaces)
    unsupported_surfaces = sorted(surface_filter - SUPPORTED_SURFACES)
    if unsupported_surfaces:
        raise ValueError(f"unsupported surface filter(s): {unsupported_surfaces!r}")

    cases: list[SkillTaskEvalCase] = []
    for row in _load_jsonl(dataset_path):
        line_number = int(row["__line_number"])
        case_id = row.get("id")
        agent = row.get("agent")
        surface = row.get("surface")
        task = row.get("task")
        request = row.get("request")
        expected = row.get("expected")
        context_flags = row.get("context_flags", {})
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{dataset_path}:{line_number}: id must be a non-empty string")
        if agent not in AGENT_REGISTRY:
            raise ValueError(f"{dataset_path}:{line_number}: unknown agent {agent!r}")
        if surface not in SUPPORTED_SURFACES:
            raise ValueError(f"{dataset_path}:{line_number}: unsupported surface {surface!r}")
        if not isinstance(task, str) or not task:
            raise ValueError(f"{dataset_path}:{line_number}: task must be a non-empty string")
        if not isinstance(request, str) or not request:
            raise ValueError(f"{dataset_path}:{line_number}: request must be a non-empty string")
        if not isinstance(expected, Mapping):
            raise ValueError(f"{dataset_path}:{line_number}: expected must be a JSON object")
        if not isinstance(context_flags, Mapping) or not all(
            isinstance(key, str) and isinstance(value, bool) for key, value in context_flags.items()
        ):
            raise ValueError(
                f"{dataset_path}:{line_number}: context_flags must map strings to booleans"
            )
        if surface_filter and surface not in surface_filter:
            continue
        cases.append(
            SkillTaskEvalCase(
                dataset=str(dataset_path.relative_to(PROJECT_ROOT)),
                line_number=line_number,
                case_id=case_id,
                agent=str(agent),
                surface=str(surface),
                task=task,
                request=request,
                expected=expected,
                context_flags=dict(context_flags),
            )
        )
    return tuple(cases)


def _string_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError(f"Expected a JSON array of strings, got {value!r}")
    return tuple(str(item) for item in value)


def _build_agent_instructions(agent_name: str, request_text: str) -> str:
    builder = AGENT_REGISTRY[agent_name].resolve_builder()
    signature = inspect.signature(builder)
    kwargs: dict[str, Any] = {}
    if "request_text" in signature.parameters:
        kwargs["request_text"] = request_text
    if "include_handoffs" in signature.parameters:
        kwargs["include_handoffs"] = False
    if "include_tools" in signature.parameters:
        kwargs["include_tools"] = False
    agent = builder(**kwargs)
    return str(agent.instructions)


def evaluate_skill_task_case(case: SkillTaskEvalCase) -> SkillTaskEvalResult:
    """Evaluate selector output and composed prompt visibility for one case."""

    expected_skills = _string_sequence(case.expected.get("include_skills"))
    excluded_skills = _string_sequence(case.expected.get("exclude_skills"))
    selected_skills = select_agent_skill_names(
        case.agent,
        request_text=case.request,
        context_flags=case.context_flags,
    )
    selection_reasons = explain_agent_skill_selection(
        case.agent,
        request_text=case.request,
        context_flags=case.context_flags,
    )
    selected = set(selected_skills)
    failures: list[str] = []
    for skill_name in expected_skills:
        if skill_name not in selected:
            failures.append(f"selected_skills missing {skill_name!r}")
    for skill_name in excluded_skills:
        if skill_name in selected:
            failures.append(f"selected_skills unexpectedly included {skill_name!r}")
    if not set(selected_skills) <= set(AGENT_SKILL_NAMES[case.agent]):
        failures.append("selected_skills included a skill outside the agent catalog")

    instructions = _build_agent_instructions(case.agent, case.request)
    for skill_name in expected_skills:
        marker = f"<!-- {skill_name}/SKILL.md -->"
        if marker not in instructions:
            failures.append(f"instructions missing marker {marker!r}")
    for skill_name in excluded_skills:
        marker = f"<!-- {skill_name}/SKILL.md -->"
        if marker in instructions:
            failures.append(f"instructions unexpectedly included marker {marker!r}")
    if "<!-- skills.md -->" in instructions:
        failures.append("instructions still include monolithic skills.md")

    return SkillTaskEvalResult(
        dataset=case.dataset,
        case_id=case.case_id,
        agent=case.agent,
        surface=case.surface,
        task=case.task,
        passed=not failures,
        failures=tuple(failures),
        selected_skills=selected_skills,
        expected_skills=expected_skills,
        excluded_skills=excluded_skills,
        selection_reasons=selection_reasons,
    )


def run_skill_task_eval_suite(
    path: str | Path = DEFAULT_SKILL_TASK_MATRIX,
    *,
    surfaces: Sequence[str] = (),
) -> SkillTaskEvalSummary:
    """Run the dry-run skill task matrix."""

    return SkillTaskEvalSummary(
        results=tuple(
            evaluate_skill_task_case(case)
            for case in load_skill_task_eval_cases(path, surfaces=surfaces)
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Keystone skill task matrix evals.")
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_SKILL_TASK_MATRIX),
        help="Path to skill task matrix JSONL.",
    )
    parser.add_argument(
        "--surface",
        choices=sorted(SUPPORTED_SURFACES),
        action="append",
        default=[],
        help="Limit the matrix to one task surface. Repeat to include multiple surfaces.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args(argv)

    summary = run_skill_task_eval_suite(args.dataset, surfaces=args.surface)
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"Skill task matrix: {summary.passed}/{summary.total} passed "
            f"across {', '.join(summary.to_dict()['agents'])}"
        )
        for result in summary.results:
            status = "PASS" if result.passed else "FAIL"
            print(f"- {status} {result.case_id} [{result.surface}] {result.agent}")
            for failure in result.failures:
                print(f"  - {failure}")
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
