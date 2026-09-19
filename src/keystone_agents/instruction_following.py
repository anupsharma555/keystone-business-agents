"""LLM-first response constraint validation and bounded repair."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ValidationError, field_validator

from keystone_agents.execution_telemetry import compact_execution_telemetry
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.output_constraints import (
    InterpretedOutputConstraints,
    OutputConstraintValidation,
)
from keystone_agents.sdk import build_model_settings, build_sdk_agent, compose_instructions

_WORD_RE = re.compile(r"\b[\w]+(?:['\u2019-][\w]+)*\b", re.UNICODE)
_URL_RE = re.compile(r"https?://[^\s)>|]+", re.I)
_MARKDOWN_CITATION_RE = re.compile(r"\[[^\]\n]+\]\(\s*https?://[^\s)]+\s*\)", re.I)
_SLACK_CITATION_RE = re.compile(r"<https?://[^>\n]+>", re.I)
_BARE_CITATION_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_CITATION_LABEL_RE = re.compile(
    r"^\*{0,2}(?:sources?|references?|gmail(?:\s+(?:source|link))?)"
    r"\s*:\*{0,2}\s*",
    re.I,
)
_LIST_ITEM_PREFIX_RE = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
    }
)
_ANSWER_SECTION_BOUNDARY_LABELS = (
    "detailed summary",
    "useful reference",
    "useful references",
    "source evidence",
    "terms",
    "recommended action",
    "recommended actions",
    "run notes",
    "metadata",
    "next step",
    "review notes",
    "what i need",
    "reply with",
    "subject",
    "body",
    "email draft",
    "source",
    "sources",
)
_DRAFT_BODY_BOUNDARY_LABELS = (
    "contact gap",
    "contact limitation",
    "recipient gap",
    "missing contact",
    "limitation",
    "limitations",
    "source",
    "sources",
    "safety",
    "unresolved point",
    "unresolved item",
    "unresolved gap",
)
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_EXACT_SENTENCE_REQUEST_RE = re.compile(
    r"\b(?:exactly|in)\s+"
    r"(?P<count>[1-9]\d?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:(?:short|concise|brief|complete|paste-ready|copy-ready)\s+){0,3}"
    r"sentences?\b",
    re.I,
)
_ADJECTIVAL_SENTENCE_REQUEST_RE = re.compile(
    r"\b(?P<count>[1-9]\d?|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"[- ]sentence\b"
    r"(?:\s+(?:answer|reply|response|note|message|draft|summary|explanation))?",
    re.I,
)
_CONDITIONAL_BRANCH_RE = re.compile(
    r"\b(?:"
    r"(?:if|when)\s+(?P<condition>[^,;:.!?\n]{1,80})\s*[,;:]"
    r"|(?P<otherwise>otherwise)\s*[,;:]"
    r")\s*",
    re.I,
)
_PASTE_READY_REQUEST_RE = re.compile(
    r"\b(?:paste|copy|paste[- ]ready|copy[- ]ready|use\s+verbatim|"
    r"send\s+as\s+written)\b",
    re.I,
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

    def length_target_guidance(self) -> str:
        """Give the repair model a safe interior target for measurable length."""

        constraints = self.interpreted_constraints
        if constraints.minimum_words is not None and constraints.maximum_words is not None:
            target = (constraints.minimum_words + constraints.maximum_words) // 2
            return (
                f"Count only the {constraints.scope} text and aim for {target} words, "
                f"comfortably inside the allowed {constraints.minimum_words}-"
                f"{constraints.maximum_words} range rather than at either boundary."
            )
        if constraints.word_count_mode == "exact" and constraints.word_count is not None:
            return (
                f"Count only the {constraints.scope} text and return exactly "
                f"{constraints.word_count} words."
            )
        return ""

    def to_prompt(self) -> str:
        return "\n\n".join(
            item
            for item in [
                "Repair this candidate response after objective constraint validation failed.",
                "Original operator request:\n" + self.original_request,
                "Interpreted output constraints:\n"
                + self.interpreted_constraints.model_dump_json(indent=2),
                self.length_target_guidance(),
                "Candidate response:\n" + self.candidate_response,
                (
                    "Bounded evidence already available to the response:\n" + self.bounded_evidence
                    if self.bounded_evidence
                    else "Bounded evidence: use only the candidate response."
                ),
                "Return only a valid InstructionFollowingRepairOutput.",
            ]
            if item
        )


@dataclass(frozen=True)
class OutputConstraintAdmission:
    """One safely admitted constraint set plus privacy-safe planner diagnostics."""

    constraints: InterpretedOutputConstraints
    warning_codes: tuple[str, ...] = ()

    @property
    def has_unresolved_warnings(self) -> bool:
        return constraint_admission_has_unresolved(self.warning_codes)


@dataclass(frozen=True)
class InstructionFollowingResolution:
    response_text: str
    validation: OutputConstraintValidation
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repair_usage: dict[str, Any] | None = None
    repair_cost: dict[str, Any] | None = None
    repair_request_cache: dict[str, Any] | None = None
    repair_execution_telemetry: dict[str, Any] | None = None
    repair_skipped_reason: str = ""
    constraint_admission_warnings: tuple[str, ...] = ()
    error: str = ""

    def metadata(self) -> dict[str, Any]:
        validation_payload = self.validation.model_dump(
            mode="json",
            exclude={"checked_text"},
        )
        if validation_payload.get("source_url_count") is None:
            validation_payload.pop("source_url_count", None)
        payload: dict[str, Any] = {
            "schema": "keystone.instruction_following.v1",
            "validation": validation_payload,
            "repair_attempted": self.repair_attempted,
            "repair_succeeded": self.repair_succeeded,
        }
        if self.error:
            payload["error"] = self.error
        if self.repair_skipped_reason:
            payload["repair_skipped_reason"] = self.repair_skipped_reason
        if self.constraint_admission_warnings:
            payload["constraint_admission_warnings"] = list(self.constraint_admission_warnings)
        if self.repair_usage:
            payload["repair_usage"] = self.repair_usage
        if self.repair_cost:
            payload["repair_cost"] = self.repair_cost
        if self.repair_request_cache:
            payload["repair_request_cache"] = self.repair_request_cache
        repair_timing = compact_execution_telemetry(self.repair_execution_telemetry)
        if repair_timing:
            payload["repair_execution_telemetry"] = repair_timing
        return payload


@dataclass(frozen=True)
class RawRequestOutputContract:
    """Measurable response requirements read from the complete operator turn.

    This is intentionally independent of planner output.  A planner may add
    advisory structure, but it cannot erase an explicit operator instruction
    such as an exact sentence count or paste-ready copy request.
    """

    constraints: InterpretedOutputConstraints
    paste_ready_copy: bool = False
    conditional: bool = False
    branch_resolved: bool = True
    exact_output_requested: bool = False


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


_COUNT_MODES = {"unspecified", "exact", "maximum", "under", "minimum"}


def _constraint_mapping(
    plan: ManualRequestPlan | Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if isinstance(plan, ManualRequestPlan):
        return plan.ask_shape.output_constraints.model_dump(mode="python")
    if not isinstance(plan, Mapping):
        return {}
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, Mapping):
        return {}
    constraints = ask_shape.get("output_constraints")
    return constraints if isinstance(constraints, Mapping) else {}


def _coerced_constraint_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _normalize_count_relationships(
    payload: dict[str, Any],
    warnings: list[str],
) -> None:
    for group, mode_field, target_field in (
        ("word", "word_count_mode", "word_count"),
        ("sentence", "sentence_count_mode", "sentence_count"),
        ("source_url", "source_url_count_mode", "source_url_count"),
    ):
        if mode_field not in payload:
            continue
        mode = str(payload.get(mode_field) or "").strip().lower()
        if mode not in _COUNT_MODES:
            payload.pop(mode_field, None)
            warnings.append(f"planner_{mode_field}_invalid")
        elif mode != "unspecified" and payload.get(target_field) in (None, ""):
            payload.pop(mode_field, None)
            payload.pop(target_field, None)
            warnings.append(f"planner_{group}_count_metadata_missing_target")
        elif mode == "unspecified" and payload.get(target_field) not in (None, ""):
            payload.pop(target_field, None)
            warnings.append(f"planner_{group}_count_target_without_mode")

    for group, minimum_field, maximum_field in (
        ("word", "minimum_words", "maximum_words"),
        ("item", "minimum_items", "maximum_items"),
    ):
        minimum = _coerced_constraint_int(payload.get(minimum_field))
        maximum = _coerced_constraint_int(payload.get(maximum_field))
        if minimum is not None and maximum is not None and minimum > maximum:
            payload.pop(minimum_field, None)
            payload.pop(maximum_field, None)
            warnings.append(f"planner_{group}_range_invalid")

    item_mode = str(payload.get("item_count_mode") or "unspecified").strip().lower()
    if "item_count_mode" in payload and item_mode not in _COUNT_MODES:
        payload.pop("item_count_mode", None)
        warnings.append("planner_item_count_mode_invalid")
        item_mode = "unspecified"
    has_minimum = payload.get("minimum_items") not in (None, "")
    has_maximum = payload.get("maximum_items") not in (None, "")
    if item_mode == "exact":
        if has_minimum and has_maximum:
            minimum = _coerced_constraint_int(payload["minimum_items"])
            maximum = _coerced_constraint_int(payload["maximum_items"])
            if minimum is not None and maximum is not None and minimum != maximum:
                payload.pop("item_count_mode", None)
                warnings.append("planner_item_count_exact_conflicting_bounds")
        elif has_minimum:
            payload["maximum_items"] = payload["minimum_items"]
            warnings.append("item_count_exact_normalized_from_minimum")
        elif has_maximum:
            payload["minimum_items"] = payload["maximum_items"]
            warnings.append("item_count_exact_normalized_from_maximum")
        else:
            payload.pop("item_count_mode", None)
            warnings.append("planner_item_count_exact_missing_bounds")
    elif item_mode == "maximum" and not has_maximum:
        payload.pop("item_count_mode", None)
        warnings.append("planner_item_count_maximum_missing_bound")
    elif item_mode == "minimum" and not has_minimum:
        payload.pop("item_count_mode", None)
        warnings.append("planner_item_count_minimum_missing_bound")
    elif item_mode == "under" and not has_maximum:
        payload.pop("item_count_mode", None)
        warnings.append("planner_item_count_under_missing_bound")


def _admit_output_constraint_mapping(
    raw: Mapping[str, Any],
) -> OutputConstraintAdmission:
    """Keep independent valid fields when one optional planner group is malformed."""

    fields = InterpretedOutputConstraints.model_fields
    payload = {key: value for key, value in raw.items() if key in fields}
    warnings: list[str] = []
    for _ in range(len(fields) + 1):
        _normalize_count_relationships(payload, warnings)
        try:
            constraints = InterpretedOutputConstraints.model_validate(payload)
            return OutputConstraintAdmission(
                constraints=constraints,
                warning_codes=tuple(dict.fromkeys(warnings)),
            )
        except ValidationError as exc:
            invalid_fields = {
                str(error["loc"][0])
                for error in exc.errors(include_url=False)
                if error.get("loc") and str(error["loc"][0]) in payload
            }
            if not invalid_fields:
                warnings.append("planner_output_constraint_combination_invalid")
                break
            for field in sorted(invalid_fields):
                payload.pop(field, None)
                warnings.append(f"planner_{field}_invalid")
    return OutputConstraintAdmission(
        constraints=InterpretedOutputConstraints(),
        warning_codes=tuple(dict.fromkeys(warnings)),
    )


def output_constraint_admission_from_plan(
    plan: ManualRequestPlan | Mapping[str, Any] | None,
) -> OutputConstraintAdmission:
    """Admit valid planner fields without letting one bad group erase the rest."""

    return _admit_output_constraint_mapping(_constraint_mapping(plan))


def output_constraints_from_plan(
    plan: ManualRequestPlan | dict[str, Any] | None,
) -> InterpretedOutputConstraints:
    """Read the safely admitted constraint contract from a manual plan."""

    return output_constraint_admission_from_plan(plan).constraints


def raw_request_output_contract(
    original_request: str,
    *,
    condition_outcome: bool | None = None,
) -> RawRequestOutputContract:
    """Interpret exact sentence and copy-ready requirements from raw prose.

    Conditional branches are resolved only from the specialist's explicit
    boolean decision.  When no decision is available, the contract records that
    exact output was requested but does not guess which branch count applies.
    """

    normalized = " ".join(str(original_request or "").split())
    if not normalized:
        return RawRequestOutputContract(InterpretedOutputConstraints())

    selected_text, branch_matches, branch_resolved = _selected_output_branch_text(
        normalized,
        condition_outcome=condition_outcome,
    )

    selected_matches = _exact_sentence_request_matches(selected_text)
    selected_counts = {_parse_request_count(match.group("count")) for match in selected_matches}
    selected_counts.discard(None)
    sentence_count = next(iter(selected_counts)) if len(selected_counts) == 1 else None
    all_exact_matches = _exact_sentence_request_matches(normalized)
    exact_output_requested = bool(all_exact_matches)
    paste_ready_copy = bool(
        _PASTE_READY_REQUEST_RE.search(selected_text)
        or (not branch_matches and _PASTE_READY_REQUEST_RE.search(normalized))
    )
    constraints = InterpretedOutputConstraints()
    if sentence_count is not None:
        constraints = InterpretedOutputConstraints(
            interpretation=(
                f"raw operator request requires exactly {sentence_count} sentence"
                f"{'s' if sentence_count != 1 else ''}"
            ),
            scope="entire_response",
            sentence_count_mode="exact",
            sentence_count=sentence_count,
        )
    return RawRequestOutputContract(
        constraints=constraints,
        paste_ready_copy=paste_ready_copy,
        conditional=bool(branch_matches),
        branch_resolved=branch_resolved,
        exact_output_requested=exact_output_requested,
    )


def _canonical_raw_request_constraints(
    original_request: str,
    *,
    condition_outcome: bool | None,
) -> InterpretedOutputConstraints:
    # Import lazily so the low-level validator can reuse the planner's one
    # canonical grammar without creating an import cycle at module load time.
    from keystone_agents.planning.compatibility import (
        interpreted_output_constraints_from_request,
    )

    normalized = " ".join(str(original_request or "").split())
    selected_text, branch_matches, branch_resolved = _selected_output_branch_text(
        normalized,
        condition_outcome=condition_outcome,
    )
    # Parse only the selected conditional branch plus requirements that precede
    # the branch set. Parsing the whole request would promote an inactive count;
    # when the specialist has not resolved the condition, the common prefix is
    # the only raw text with count authority.
    parse_text = selected_text if branch_matches else normalized
    constraints = interpreted_output_constraints_from_request(parse_text)
    conditional_sentence = raw_request_output_contract(
        original_request,
        condition_outcome=condition_outcome,
    ).constraints
    if branch_resolved and conditional_sentence.sentence_count_mode != "unspecified":
        constraints = constraints.model_copy(
            update={
                "scope": conditional_sentence.scope,
                "sentence_count_mode": conditional_sentence.sentence_count_mode,
                "sentence_count": conditional_sentence.sentence_count,
            }
        )
    return constraints


def _merge_authoritative_raw_constraints(
    planned: InterpretedOutputConstraints,
    raw: InterpretedOutputConstraints,
) -> InterpretedOutputConstraints:
    """Overlay only explicit raw requirements onto safely admitted planner fields."""

    updates: dict[str, Any] = {}
    raw_has_word_contract = bool(
        raw.word_count_mode != "unspecified"
        or raw.minimum_words is not None
        or raw.maximum_words is not None
    )
    raw_has_sentence_contract = raw.sentence_count_mode != "unspecified"
    raw_has_item_contract = bool(
        raw.item_count_mode != "unspecified"
        or raw.minimum_items is not None
        or raw.maximum_items is not None
    )
    raw_has_source_contract = bool(
        raw.include_source_urls
        or raw.source_url_count_mode != "unspecified"
        or raw.source_url_count is not None
    )
    if raw_has_word_contract:
        updates.update(
            word_count_mode=raw.word_count_mode,
            word_count=raw.word_count,
            minimum_words=raw.minimum_words,
            maximum_words=raw.maximum_words,
        )
        if raw.word_scope != "unspecified":
            updates["word_scope"] = raw.word_scope
    if raw_has_sentence_contract:
        updates.update(
            sentence_count_mode=raw.sentence_count_mode,
            sentence_count=raw.sentence_count,
        )
        if raw.sentence_scope != "unspecified":
            updates["sentence_scope"] = raw.sentence_scope
    if raw_has_item_contract:
        updates.update(
            item_count_mode=raw.item_count_mode,
            minimum_items=raw.minimum_items,
            maximum_items=raw.maximum_items,
        )
        if raw.item_scope != "unspecified":
            updates["item_scope"] = raw.item_scope
    if raw_has_source_contract:
        updates.update(
            source_url_count_mode=raw.source_url_count_mode,
            source_url_count=raw.source_url_count,
            include_source_urls=raw.include_source_urls,
        )
        if raw.source_scope != "unspecified":
            updates["source_scope"] = raw.source_scope
    if raw.required_sections:
        updates["required_sections"] = list(
            dict.fromkeys([*planned.required_sections, *raw.required_sections])
        )
        updates["require_section_headings"] = True
    if raw.forbidden_phrases:
        updates["forbidden_phrases"] = list(
            dict.fromkeys([*planned.forbidden_phrases, *raw.forbidden_phrases])
        )
    if raw.forbid_em_dash:
        updates["forbid_em_dash"] = True
    if raw.style_requirements:
        updates["style_requirements"] = list(
            dict.fromkeys([*planned.style_requirements, *raw.style_requirements])
        )
    if (
        planned.scope == "unspecified"
        and raw.scope != "unspecified"
        and raw.has_deterministic_requirements()
    ):
        updates["scope"] = raw.scope
    interpretations = [value for value in (planned.interpretation, raw.interpretation) if value]
    if interpretations:
        updates["interpretation"] = "; ".join(dict.fromkeys(interpretations))
    return planned.model_copy(update=updates)


def _resolved_constraint_warning_codes(
    warnings: tuple[str, ...],
    raw: InterpretedOutputConstraints,
) -> tuple[str, ...]:
    output: list[str] = []
    raw_groups = {
        "word": bool(
            raw.word_count_mode != "unspecified"
            or raw.minimum_words is not None
            or raw.maximum_words is not None
        ),
        "sentence": raw.sentence_count_mode != "unspecified",
        "item": bool(
            raw.item_count_mode != "unspecified"
            or raw.minimum_items is not None
            or raw.maximum_items is not None
        ),
        "source_url": bool(
            raw.include_source_urls
            or raw.source_url_count_mode != "unspecified"
            or raw.source_url_count is not None
        ),
    }
    for warning in warnings:
        if warning.startswith("item_count_exact_normalized_"):
            output.append(warning)
            continue
        group = (
            "source_url"
            if "source_url" in warning
            else "sentence"
            if "sentence_count" in warning
            else "word"
            if any(
                token in warning
                for token in ("word_count", "minimum_words", "maximum_words", "word_range")
            )
            else "item"
            if any(
                token in warning
                for token in (
                    "item_count",
                    "minimum_items",
                    "maximum_items",
                    "item_range",
                )
            )
            else ""
        )
        if group and raw_groups[group]:
            output.append(f"{group}_count_metadata_recovered_from_raw_request")
        elif group == "item":
            detail = warning.removeprefix("planner_item_count_").removeprefix("planner_item_")
            output.append(f"unresolved_item_count_{detail}")
        elif group:
            output.append(f"nonbinding_planner_{group}_count_metadata_ignored")
        else:
            output.append(f"nonbinding_{warning}")
    return tuple(dict.fromkeys(output))


def resolved_output_constraint_admission(
    plan: ManualRequestPlan | dict[str, Any] | None,
    *,
    original_request: str,
    condition_outcome: bool | None = None,
) -> OutputConstraintAdmission:
    """Reconcile admitted planner metadata with authoritative raw requirements."""

    planned = output_constraint_admission_from_plan(plan)
    raw = _canonical_raw_request_constraints(
        original_request,
        condition_outcome=condition_outcome,
    )
    return OutputConstraintAdmission(
        constraints=_merge_authoritative_raw_constraints(planned.constraints, raw),
        warning_codes=_resolved_constraint_warning_codes(
            planned.warning_codes,
            raw,
        ),
    )


def resolved_output_constraints(
    plan: ManualRequestPlan | dict[str, Any] | None,
    *,
    original_request: str,
    condition_outcome: bool | None = None,
) -> InterpretedOutputConstraints:
    """Return constraints after planner admission and raw-request reconciliation."""

    return resolved_output_constraint_admission(
        plan,
        original_request=original_request,
        condition_outcome=condition_outcome,
    ).constraints


def constraint_admission_has_unresolved(warnings: object) -> bool:
    """Return whether admission metadata contains an unresolved hard-count shape."""

    values = warnings if isinstance(warnings, list | tuple | set) else [warnings]
    return any(str(value or "").startswith("unresolved_") for value in values)


def exact_output_validation_required(
    plan: ManualRequestPlan | dict[str, Any] | None,
    *,
    original_request: str,
    condition_outcome: bool | None = None,
) -> bool:
    """Return whether completion depends on measured exact user-visible output."""

    raw = raw_request_output_contract(
        original_request,
        condition_outcome=condition_outcome,
    )
    constraints = resolved_output_constraints(
        plan,
        original_request=original_request,
        condition_outcome=condition_outcome,
    )
    return bool(
        raw.exact_output_requested
        or constraints.word_count_mode == "exact"
        or constraints.sentence_count_mode == "exact"
        or constraints.item_count_mode == "exact"
        or constraints.source_url_count_mode == "exact"
    )


def _parse_request_count(raw: str) -> int | None:
    cleaned = str(raw or "").strip().lower()
    return int(cleaned) if cleaned.isdigit() else _COUNT_WORDS.get(cleaned)


def _request_condition_polarity(match: re.Match[str]) -> bool | None:
    """Map only explicit affirmative/negative output branches onto a decision."""

    if match.group("otherwise"):
        return False
    condition = " ".join(str(match.group("condition") or "").lower().split())
    if re.search(
        r"\b(?:no|not|negative|isn't|wasn't|doesn't|didn't|cannot|can't)\b",
        condition,
    ):
        return False
    if re.search(r"\b(?:yes|so|affirmative)\b", condition):
        return True
    if re.fullmatch(r"(?:it|that|the answer)\s+(?:does|is)", condition) or re.search(
        r"\b(?:reply|response|action|answer|decision|follow[- ]?up)\b"
        r"[^,;:.!?]{0,40}\b(?:needed|warranted|appropriate|useful|recommended|"
        r"required|merited|merits)\b",
        condition,
    ):
        return True
    return None


def _selected_output_branch_text(
    normalized: str,
    *,
    condition_outcome: bool | None,
) -> tuple[str, list[tuple[re.Match[str], bool]], bool]:
    """Keep common prefix instructions and at most one model-selected branch."""

    branch_matches = [
        (match, polarity)
        for match in _CONDITIONAL_BRANCH_RE.finditer(normalized)
        if (polarity := _request_condition_polarity(match)) is not None
    ]
    if not branch_matches:
        return normalized, [], True

    common_prefix = normalized[: branch_matches[0][0].start()].strip()
    if condition_outcome is None:
        return common_prefix, branch_matches, False

    selected_branch = ""
    for index, (match, polarity) in enumerate(branch_matches):
        end = (
            branch_matches[index + 1][0].start()
            if index + 1 < len(branch_matches)
            else len(normalized)
        )
        if polarity is condition_outcome:
            selected_branch = normalized[match.end() : end].strip()
            break
    return (
        " ".join(part for part in (common_prefix, selected_branch) if part),
        branch_matches,
        True,
    )


def _exact_sentence_request_matches(text: str) -> list[re.Match[str]]:
    """Keep sentence counts that grammatically shape the requested response."""

    normalized = " ".join(str(text or "").split())
    matches: list[re.Match[str]] = []
    candidate_matches = sorted(
        [
            *_EXACT_SENTENCE_REQUEST_RE.finditer(normalized),
            *_ADJECTIVAL_SENTENCE_REQUEST_RE.finditer(normalized),
        ],
        key=lambda match: match.start(),
    )
    for match in candidate_matches:
        prefix = normalized[max(0, match.start() - 120) : match.start()]
        prefix_clause = re.split(r"[.;:!?\n]", prefix)[-1]
        suffix = normalized[match.end() : match.end() + 80]
        if re.search(
            r"\b(?:answer|respond|reply|return|give(?:\s+me)?|provide|write|"
            r"draft|compose|prepare|summari[sz]e|explain|describe|keep|limit|"
            r"shorten|condense|need|want)\b[^.;:!?\n]{0,100}$",
            prefix_clause,
            re.I,
        ) or _PASTE_READY_REQUEST_RE.search(suffix):
            matches.append(match)
    return matches


def interpreted_output_constraints_text(
    plan: ManualRequestPlan | dict[str, Any] | None,
) -> str:
    """Render specialist-readable LLM interpretation without adding authority."""

    constraints = output_constraints_from_plan(plan)
    if not constraints.is_explicit():
        return ""
    return (
        "Canonical response constraints reconciled against the authoritative current "
        "operator turn; the raw operator request remains authoritative. Reason about "
        "and satisfy these constraints in the user-facing fields; deterministic helpers "
        "will only validate the grounded hard fields:\n" + constraints.model_dump_json(indent=2)
    )


def validate_output_constraints(
    response_text: str,
    constraints: InterpretedOutputConstraints,
    *,
    source_urls_applicable: bool = True,
) -> OutputConstraintValidation:
    """Measure objective requirements without rewriting the LLM response."""

    if not constraints.has_deterministic_requirements():
        return OutputConstraintValidation()
    checked = _constraint_scope_text(response_text, constraints.scope)
    violations: list[str] = []
    satisfied: list[str] = []

    def scoped_text(binding: str) -> str:
        if binding != "unspecified":
            return _constraint_scope_text(response_text, binding)
        return checked

    effective_word_scope = (
        constraints.word_scope if constraints.word_scope != "unspecified" else constraints.scope
    )
    word_count = _word_count(
        scoped_text(constraints.word_scope),
        exclude_trailing_citation=effective_word_scope == "answer",
    )
    sentence_count = _sentence_count(scoped_text(constraints.sentence_scope))
    item_count = _item_count(scoped_text(constraints.item_scope))
    source_text = (
        scoped_text(constraints.source_scope)
        if constraints.source_scope != "unspecified"
        else checked
    )
    source_urls = list(
        dict.fromkeys(
            canonical
            for raw_url in _URL_RE.findall(source_text)
            if (canonical := canonical_source_url(raw_url))
        )
    )

    _validate_count(
        label="word count",
        actual=word_count,
        mode=constraints.word_count_mode,
        expected=constraints.word_count,
        violations=violations,
        satisfied=satisfied,
    )
    if constraints.minimum_words is not None:
        if word_count < constraints.minimum_words:
            violations.append(
                f"word count {word_count} is below minimum {constraints.minimum_words}"
            )
        else:
            satisfied.append(f"minimum word count {constraints.minimum_words}")
    if constraints.maximum_words is not None:
        if word_count > constraints.maximum_words:
            violations.append(
                f"word count {word_count} exceeds maximum {constraints.maximum_words}"
            )
        else:
            satisfied.append(f"maximum word count {constraints.maximum_words}")
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
    _validate_count(
        label="source URL count",
        actual=len(source_urls),
        mode=constraints.source_url_count_mode,
        expected=constraints.source_url_count,
        violations=violations,
        satisfied=satisfied,
    )

    response_lower = response_text.lower()
    for section in constraints.required_sections if constraints.require_section_headings else []:
        if not any(
            _section_start_tail(line, (section,)) is not None for line in response_text.splitlines()
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
    if constraints.include_source_urls and source_urls_applicable:
        visible_sources = (
            source_text if constraints.source_scope != "unspecified" else response_text
        )
        if any(canonical_source_url(raw_url) for raw_url in _URL_RE.findall(visible_sources)):
            satisfied.append("visible source URL")
        else:
            violations.append("visible source URL missing")
    elif constraints.include_source_urls:
        satisfied.append("visible source URL conditional on matched results; no matches")

    return OutputConstraintValidation(
        applicable=True,
        passed=not violations,
        scope=constraints.scope,
        checked_text=checked,
        word_count=(
            word_count
            if constraints.word_count_mode != "unspecified"
            or constraints.minimum_words is not None
            or constraints.maximum_words is not None
            else None
        ),
        sentence_count=(
            sentence_count if constraints.sentence_count_mode != "unspecified" else None
        ),
        item_count=(
            item_count
            if constraints.minimum_items is not None or constraints.maximum_items is not None
            else None
        ),
        source_url_count=(
            len(source_urls) if constraints.source_url_count_mode != "unspecified" else None
        ),
        satisfied_constraints=satisfied,
        violations=violations,
    )


def canonical_source_url(raw_url: str) -> str:
    """Canonicalize one visible URL for source-identity counting."""

    cleaned = str(raw_url or "").rstrip(".,;:!?]}>'\"")
    if not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return ""
    original_scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if original_scheme not in {"http", "https"} or not hostname:
        return ""
    scheme = "https"
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = hostname
    if port and not (
        (original_scheme == "http" and port == 80) or (original_scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    fragment = ""
    if hostname == "mail.google.com" and (path == "/mail" or path.startswith("/mail/u/")):
        gmail_fragment = parsed.fragment.strip("/")
        if gmail_fragment:
            # Gmail uses the URL fragment as the selected message/thread identity.
            # Normalize away mailbox/search views while keeping the final identity.
            fragment = "message/" + gmail_fragment.rsplit("/", 1)[-1]
    return urlunsplit(
        (
            scheme,
            netloc,
            path,
            urlencode(sorted(query_items)),
            fragment,
        )
    )


def canonical_source_urls(text: str) -> tuple[str, ...]:
    """Return distinct canonical bare, Markdown, or Slack-visible source URLs."""

    return tuple(
        dict.fromkeys(
            canonical
            for raw_url in _URL_RE.findall(str(text or ""))
            if (canonical := canonical_source_url(raw_url))
        )
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
    source_urls_applicable: bool = True,
    allow_length_expansion_repair: bool = True,
    condition_outcome: bool | None = None,
) -> InstructionFollowingResolution:
    """Validate an LLM response and run at most one tool-free LLM repair."""

    admission = resolved_output_constraint_admission(
        manual_plan,
        original_request=original_request,
        condition_outcome=condition_outcome,
    )
    constraints = admission.constraints
    initial = validate_output_constraints(
        candidate_response,
        constraints,
        source_urls_applicable=source_urls_applicable,
    )
    if not initial.applicable or initial.passed:
        return InstructionFollowingResolution(
            candidate_response,
            initial,
            constraint_admission_warnings=admission.warning_codes,
        )
    if not live and run_config is None:
        return InstructionFollowingResolution(
            candidate_response,
            initial,
            constraint_admission_warnings=admission.warning_codes,
        )
    if not allow_length_expansion_repair and _requires_length_expansion(
        initial,
        constraints,
    ):
        return InstructionFollowingResolution(
            candidate_response,
            initial,
            repair_skipped_reason="strict_evidence_length_expansion_disabled",
            constraint_admission_warnings=admission.warning_codes,
        )
    repair_constraints = constraints
    if (
        constraints.include_source_urls
        and not source_urls_applicable
        and constraints.source_url_count_mode == "unspecified"
    ):
        repair_constraints = constraints.model_copy(update={"include_source_urls": False})
    try:
        result = run_typed_sdk_agent(
            agent=build_instruction_following_repair_agent(model=model),
            typed_input=InstructionFollowingRepairInput(
                original_request=original_request,
                interpreted_constraints=repair_constraints,
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
            constraint_admission_warnings=admission.warning_codes,
            error=f"{type(exc).__name__}: {exc}",
        )
    repaired = result.output.response_text.strip()
    repaired_validation = validate_output_constraints(
        repaired,
        constraints,
        source_urls_applicable=source_urls_applicable,
    )
    return InstructionFollowingResolution(
        repaired if repaired_validation.passed else candidate_response,
        repaired_validation,
        repair_attempted=True,
        repair_succeeded=repaired_validation.passed,
        repair_usage=getattr(result, "usage", None),
        repair_cost=getattr(result, "cost", None),
        repair_request_cache=getattr(result, "request_cache", None),
        repair_execution_telemetry=getattr(result, "execution_telemetry", None),
        constraint_admission_warnings=admission.warning_codes,
    )


def _requires_length_expansion(
    validation: OutputConstraintValidation,
    constraints: InterpretedOutputConstraints,
) -> bool:
    """Return whether satisfying the measured contract requires adding words."""

    actual = validation.word_count
    if actual is None:
        return False
    if constraints.minimum_words is not None and actual < constraints.minimum_words:
        return True
    return bool(
        constraints.word_count_mode in {"exact", "minimum"}
        and constraints.word_count is not None
        and actual < constraints.word_count
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
        body = _named_section_text(
            text,
            (
                "body",
                "draft reply",
                "email draft",
                "linkedin draft",
                "outreach draft",
                "introduction",
                "organization introduction",
                "organization-level introduction",
            ),
        )
        body = body if body is not None else text
        return _truncate_before_named_section(body, _DRAFT_BODY_BOUNDARY_LABELS)
    answer = _named_section_text(text, ("answer",))
    if answer is None:
        return text
    return _truncate_before_named_section(answer, _ANSWER_SECTION_BOUNDARY_LABELS)


def _section_start_tail(line: str, labels: tuple[str, ...]) -> str | None:
    """Return inline content for an exact plain, Markdown, or Slack heading."""

    content = str(line or "").strip()
    content = re.sub(r"^>\s*", "", content)
    content = re.sub(r"^#{1,6}\s+", "", content)
    for marker in ("**", "__", "*", "_"):
        if not content.startswith(marker):
            continue
        closing = content.find(marker, len(marker))
        if closing < 0:
            continue
        heading = content[len(marker) : closing].strip().rstrip(":").strip()
        if any(heading.casefold() == label.casefold() for label in labels):
            return content[closing + len(marker) :].lstrip(" :\t")
    for label in labels:
        if content.casefold() == label.casefold():
            return ""
        prefix = f"{label}:"
        if content[: len(prefix)].casefold() == prefix.casefold():
            return content[len(prefix) :].strip()
    return None


def _named_section_text(text: str, labels: tuple[str, ...]) -> str | None:
    """Extract a labeled section while retaining any inline first content."""

    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        tail = _section_start_tail(line, labels)
        if tail is None:
            continue
        return "\n".join([tail, *lines[index + 1 :]]).strip()
    return None


def _truncate_before_named_section(text: str, labels: tuple[str, ...]) -> str:
    """Stop at the next exact supported section label, not prose with that prefix."""

    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if _section_start_tail(line, labels) is not None:
            return "\n".join(lines[:index]).strip()
    return str(text or "").strip()


def _strip_trailing_citation_only(text: str) -> str:
    """Remove citation-only suffixes for prose counts without changing output."""

    lines = str(text or "").splitlines()
    removed_citation = False
    while lines:
        line = lines[-1].strip()
        if not line:
            lines.pop()
            continue
        if _citation_only_fragment(line):
            removed_citation = True
            lines.pop()
        elif removed_citation and _CITATION_LABEL_RE.fullmatch(line):
            lines.pop()
        else:
            break
    parts = [
        part
        for part in re.split(r"(?<=[.!?])\s+", " ".join("\n".join(lines).split()))
        if part.strip()
    ]
    while parts and _citation_only_fragment(parts[-1]):
        parts.pop()
    return " ".join(parts)


def _word_count(text: str, *, exclude_trailing_citation: bool = False) -> int:
    """Count each visible URL as one word for human-facing output contracts."""

    count_text = (
        _strip_trailing_citation_only(text) if exclude_trailing_citation else str(text or "")
    )
    without_urls = _SLACK_CITATION_RE.sub(" URL ", count_text)
    without_urls = _URL_RE.sub(" URL ", without_urls)
    return len(_WORD_RE.findall(without_urls))


def _citation_only_fragment(text: str) -> bool:
    """Return whether a trailing fragment contains links and citation syntax only."""

    content = _LIST_ITEM_PREFIX_RE.sub("", str(text or "").strip())
    content = _CITATION_LABEL_RE.sub("", content)
    link_count = 0
    for pattern in (
        _MARKDOWN_CITATION_RE,
        _SLACK_CITATION_RE,
        _BARE_CITATION_URL_RE,
    ):
        content, removed = pattern.subn("", content)
        link_count += removed
    content = re.sub(r"(?i)\b(?:and|or)\b", "", content)
    content = re.sub(r"(?:^|\s)(?:[-*+•]|\d+[.)])(?=\s|$)", " ", content)
    return link_count > 0 and not content.strip(" \t\r\n,;:.&")


def _sentence_count(text: str) -> int:
    # Count prose independently of trailing citation-only lines or an inline
    # citation suffix. Keep the original response intact for source validation.
    lines = [re.sub(r"^\s*\d+[.)]\s+", "", line) for line in str(text or "").splitlines()]
    compact = _strip_trailing_citation_only("\n".join(lines))
    if not compact:
        return 0
    protected = re.sub(
        r"\b(?:Mr|Mrs|Ms|Mx|Dr|Prof)\.(?=\s+[A-Z][\w'-]+)",
        lambda match: match.group(0)[:-1] + "\u2024",
        compact,
        flags=re.I,
    )
    protected = re.sub(
        r"\b(?:Sr|Jr|SR|JR|sr|jr)\.(?=\s+[a-z])",
        lambda match: match.group(0)[:-1] + "\u2024",
        protected,
    )
    protected = re.sub(
        r"\b(?:[A-Za-z]\.){2,}(?=\s+[a-z])",
        lambda match: match.group(0).replace(".", "\u2024"),
        protected,
    )
    parts = [part for part in re.split(r"(?<=[.!?])\s+", protected) if part.strip()]
    return len(parts)


def _item_count(text: str) -> int:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    bullets = [line for line in lines if _LIST_ITEM_PREFIX_RE.match(line)]
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
    "OutputConstraintAdmission",
    "RawRequestOutputContract",
    "build_instruction_following_repair_agent",
    "canonical_source_url",
    "canonical_source_urls",
    "constraint_admission_has_unresolved",
    "exact_output_validation_required",
    "instruction_following_blocker_text",
    "interpreted_output_constraints_text",
    "output_constraint_admission_from_plan",
    "output_constraints_from_plan",
    "raw_request_output_contract",
    "resolved_output_constraint_admission",
    "resolved_output_constraints",
    "resolve_instruction_following_response",
    "validate_output_constraints",
]
