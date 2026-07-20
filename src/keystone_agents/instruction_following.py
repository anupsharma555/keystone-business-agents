"""LLM-first response constraint validation and bounded repair."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, field_validator

from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.output_constraints import (
    InterpretedOutputConstraints,
    OutputConstraintValidation,
)
from keystone_agents.sdk import build_model_settings, build_sdk_agent, compose_instructions

_WORD_RE = re.compile(r"\b[\w]+(?:['\u2019-][\w]+)*\b", re.UNICODE)
_URL_RE = re.compile(r"https?://[^\s)>]+", re.I)
_SECTION_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\*{0,2})(?:detailed summary|useful references?|source evidence|"
    r"terms|recommended actions?|run notes|metadata|next step|review notes|"
    r"what i need|reply with)\s*:?(?:\*{0,2})\s*$"
)


class InstructionFollowingRepairOutput(BaseModel):
    """One bounded LLM rewrite after objective validation fails."""

    response_text: str
    reasoning_summary: str = ""

    @field_validator("response_text", "reasoning_summary", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> str:
        return str(value or "").strip()


class InstructionFollowingRepairInput(BaseModel):
    """Bounded facts and constraints for a tool-free repair turn."""

    original_request: str
    interpreted_constraints: InterpretedOutputConstraints
    candidate_response: str
    bounded_evidence: str = ""

    def to_prompt(self) -> str:
        return "\n\n".join(
            [
                "Repair this candidate response after objective constraint validation failed.",
                "Original operator request:\n" + self.original_request,
                "Interpreted output constraints:\n"
                + self.interpreted_constraints.model_dump_json(indent=2),
                "Candidate response:\n" + self.candidate_response,
                (
                    "Bounded evidence already available to the response:\n"
                    + self.bounded_evidence
                    if self.bounded_evidence
                    else "Bounded evidence: use only the candidate response."
                ),
                "Return only a valid InstructionFollowingRepairOutput.",
            ]
        )


@dataclass(frozen=True)
class InstructionFollowingResolution:
    response_text: str
    validation: OutputConstraintValidation
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repair_usage: dict[str, Any] | None = None
    repair_cost: dict[str, Any] | None = None
    repair_request_cache: dict[str, Any] | None = None
    error: str = ""

    def metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "keystone.instruction_following.v1",
            "validation": self.validation.model_dump(mode="json", exclude={"checked_text"}),
            "repair_attempted": self.repair_attempted,
            "repair_succeeded": self.repair_succeeded,
        }
        if self.error:
            payload["error"] = self.error
        if self.repair_usage:
            payload["repair_usage"] = self.repair_usage
        if self.repair_cost:
            payload["repair_cost"] = self.repair_cost
        if self.repair_request_cache:
            payload["repair_request_cache"] = self.repair_request_cache
        return payload


def build_instruction_following_repair_agent(model: str | None = None):
    """Build the tool-free bounded response-repair agent."""

    return build_sdk_agent(
        name="instruction_following_repair",
        instructions=compose_instructions(
            "safety_policy.md",
            "instruction_following_repair.md",
            shared_prompt_files=("writing_style.md",),
        ),
        output_type=InstructionFollowingRepairOutput,
        tools=[],
        model=model,
        model_settings=build_model_settings(reasoning_effort="low", max_tokens=1200),
        enforce_tool_policy=False,
    )


def output_constraints_from_plan(
    plan: ManualRequestPlan | dict[str, Any] | None,
) -> InterpretedOutputConstraints:
    """Read the typed LLM-interpreted constraint contract from a manual plan."""

    if isinstance(plan, ManualRequestPlan):
        return plan.ask_shape.output_constraints
    if not isinstance(plan, dict):
        return InterpretedOutputConstraints()
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, dict):
        return InterpretedOutputConstraints()
    try:
        return InterpretedOutputConstraints.model_validate(
            ask_shape.get("output_constraints") or {}
        )
    except ValueError:
        return InterpretedOutputConstraints()


def interpreted_output_constraints_text(
    plan: ManualRequestPlan | dict[str, Any] | None,
) -> str:
    """Render specialist-readable LLM interpretation without adding authority."""

    constraints = output_constraints_from_plan(plan)
    if not constraints.is_explicit():
        return ""
    return (
        "Interpreted response constraints from Orchestrator planning. The raw operator "
        "request remains authoritative. Reason about and satisfy these constraints in "
        "the user-facing fields; deterministic helpers will only validate the result:\n"
        + constraints.model_dump_json(indent=2)
    )


def validate_output_constraints(
    response_text: str,
    constraints: InterpretedOutputConstraints,
) -> OutputConstraintValidation:
    """Measure objective requirements without rewriting the LLM response."""

    if not constraints.has_deterministic_requirements():
        return OutputConstraintValidation()
    checked = _constraint_scope_text(response_text, constraints.scope)
    violations: list[str] = []
    satisfied: list[str] = []
    word_count = len(_WORD_RE.findall(checked))
    sentence_count = _sentence_count(checked)
    item_count = _item_count(checked)

    _validate_count(
        label="word count",
        actual=word_count,
        mode=constraints.word_count_mode,
        expected=constraints.word_count,
        violations=violations,
        satisfied=satisfied,
    )
    _validate_count(
        label="sentence count",
        actual=sentence_count,
        mode=constraints.sentence_count_mode,
        expected=constraints.sentence_count,
        violations=violations,
        satisfied=satisfied,
    )
    if constraints.minimum_items is not None:
        if item_count < constraints.minimum_items:
            violations.append(
                f"item count {item_count} is below minimum {constraints.minimum_items}"
            )
        else:
            satisfied.append(f"minimum item count {constraints.minimum_items}")
    if constraints.maximum_items is not None:
        if item_count > constraints.maximum_items:
            violations.append(
                f"item count {item_count} exceeds maximum {constraints.maximum_items}"
            )
        else:
            satisfied.append(f"maximum item count {constraints.maximum_items}")

    response_lower = response_text.lower()
    for section in (
        constraints.required_sections if constraints.require_section_headings else []
    ):
        if not re.search(
            rf"(?im)^\s*(?:[#>]{{1,6}}\s*)?(?:\*{{0,2}})"
            rf"{re.escape(section)}\s*:?(?:\*{{0,2}})(?:\s+.+)?$",
            response_text,
        ):
            violations.append(f"missing required section: {section}")
        else:
            satisfied.append(f"required section: {section}")
    for phrase in constraints.forbidden_phrases:
        if phrase.lower() in response_lower:
            violations.append(f"forbidden phrase present: {phrase}")
        else:
            satisfied.append(f"forbidden phrase absent: {phrase}")
    if constraints.forbid_em_dash:
        if "\u2014" in response_text:
            violations.append("em dash present")
        else:
            satisfied.append("no em dash")
    if constraints.include_source_urls:
        if _URL_RE.search(response_text):
            satisfied.append("visible source URL")
        else:
            violations.append("visible source URL missing")

    return OutputConstraintValidation(
        applicable=True,
        passed=not violations,
        scope=constraints.scope,
        checked_text=checked,
        word_count=word_count if constraints.word_count_mode != "unspecified" else None,
        sentence_count=(
            sentence_count if constraints.sentence_count_mode != "unspecified" else None
        ),
        item_count=(
            item_count
            if constraints.minimum_items is not None or constraints.maximum_items is not None
            else None
        ),
        satisfied_constraints=satisfied,
        violations=violations,
    )


def resolve_instruction_following_response(
    candidate_response: str,
    *,
    original_request: str,
    manual_plan: ManualRequestPlan | dict[str, Any] | None,
    bounded_evidence: str = "",
    live: bool,
    run_config: Any | None = None,
    model: str | None = None,
) -> InstructionFollowingResolution:
    """Validate an LLM response and run at most one tool-free LLM repair."""

    constraints = output_constraints_from_plan(manual_plan)
    initial = validate_output_constraints(candidate_response, constraints)
    if not initial.applicable or initial.passed:
        return InstructionFollowingResolution(candidate_response, initial)
    if not live and run_config is None:
        return InstructionFollowingResolution(candidate_response, initial)
    try:
        result = run_typed_sdk_agent(
            agent=build_instruction_following_repair_agent(model=model),
            typed_input=InstructionFollowingRepairInput(
                original_request=original_request,
                interpreted_constraints=constraints,
                candidate_response=candidate_response,
                bounded_evidence=bounded_evidence[:12000],
            ),
            output_type=InstructionFollowingRepairOutput,
            run_config=run_config,
            live=live,
            workflow_name="Keystone instruction-following response repair",
            tracing_disabled=True,
        )
    except Exception as exc:
        return InstructionFollowingResolution(
            candidate_response,
            initial,
            repair_attempted=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    repaired = result.output.response_text.strip()
    repaired_validation = validate_output_constraints(repaired, constraints)
    return InstructionFollowingResolution(
        repaired if repaired_validation.passed else candidate_response,
        repaired_validation,
        repair_attempted=True,
        repair_succeeded=repaired_validation.passed,
        repair_usage=getattr(result, "usage", None),
        repair_cost=getattr(result, "cost", None),
        repair_request_cache=getattr(result, "request_cache", None),
    )


def instruction_following_blocker_text(validation: OutputConstraintValidation) -> str:
    """Return a safe failure message instead of silently emitting invalid output."""

    detail = "; ".join(validation.violations[:4]) or "requested output constraints"
    return (
        "I could not produce a response that passed the requested output contract "
        f"without risking unsupported or misleading content. Validation gap: {detail}."
    )


def _constraint_scope_text(response_text: str, scope: str) -> str:
    text = str(response_text or "").strip()
    if scope == "entire_response" or scope == "unspecified":
        return text
    if scope == "draft_body":
        match = re.search(
            r"(?ims)^\s*(?:\*{0,2})(?:body|draft reply|email draft|linkedin draft)"
            r"\s*:(?:\*{0,2})\s*(?P<body>.+)$",
            text,
        )
        return match.group("body").strip() if match else text
    match = re.search(
        r"(?ims)^\s*(?:\*{0,2})answer\s*:(?:\*{0,2})\s*(?P<answer>.+)$",
        text,
    )
    if not match:
        return text
    answer = match.group("answer")
    boundary = _SECTION_BOUNDARY_RE.search(answer)
    return answer[: boundary.start()].strip() if boundary else answer.strip()


def _sentence_count(text: str) -> int:
    compact = " ".join(str(text or "").split())
    if not compact:
        return 0
    parts = [part for part in re.split(r"(?<=[.!?])\s+", compact) if part.strip()]
    return len(parts)


def _item_count(text: str) -> int:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    bullets = [line for line in lines if re.match(r"^(?:[-*]|\d+[.)])\s+", line)]
    if bullets:
        return len(bullets)
    table_rows = [
        line
        for line in lines
        if line.startswith("|") and line.endswith("|") and not re.search(r"\|\s*:?-{3,}", line)
    ]
    return max(0, len(table_rows) - 1) if table_rows else 0


def _validate_count(
    *,
    label: str,
    actual: int,
    mode: str,
    expected: int | None,
    violations: list[str],
    satisfied: list[str],
) -> None:
    if mode == "unspecified" or expected is None:
        return
    passed = (
        actual == expected
        if mode == "exact"
        else actual <= expected
        if mode == "maximum"
        else actual < expected
        if mode == "under"
        else actual >= expected
    )
    if passed:
        satisfied.append(f"{label} {mode} {expected}")
    else:
        violations.append(f"{label} {actual} does not satisfy {mode} {expected}")


__all__ = [
    "InstructionFollowingRepairInput",
    "InstructionFollowingRepairOutput",
    "InstructionFollowingResolution",
    "build_instruction_following_repair_agent",
    "instruction_following_blocker_text",
    "interpreted_output_constraints_text",
    "output_constraints_from_plan",
    "resolve_instruction_following_response",
    "validate_output_constraints",
]
