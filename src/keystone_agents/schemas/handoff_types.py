"""Reusable type compatibility metadata for Keystone handoffs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

HANDOFF_TYPE_CONTRACT_VERSION = "keystone.handoff_types.v1"

HandoffPayloadMode = Literal["native", "adapted", "summarized", "malformed", "unknown"]
HandoffParsedOutputStatus = Literal[
    "parsed",
    "text",
    "missing",
    "malformed",
    "not_applicable",
]
HandoffCompatibilityStatus = Literal["compatible", "warning", "incompatible", "unknown"]
HandoffAdaptationStatus = Literal[
    "compatible",
    "compatible_with_warnings",
    "needs_clarification",
    "blocked",
]

_ASK_SHAPE_FIELDS: tuple[str, ...] = (
    "ask_breadth",
    "evidence_depth",
    "source_type_preference",
    "strict_filter_mode",
    "output_form",
    "prior_context_dependency",
    "permission_state",
    "cost_mode",
    "stop_condition",
)
_SAFETY_CRITICAL_ASK_SHAPE_FIELDS = frozenset(
    {
        "strict_filter_mode",
        "prior_context_dependency",
        "permission_state",
        "stop_condition",
    }
)


class HandoffTypeContract(BaseModel):
    """Compact input/output compatibility metadata for one handoff boundary."""

    contract_version: str = HANDOFF_TYPE_CONTRACT_VERSION
    source_agent: str = ""
    target_agent: str = ""
    source_output_type: str = ""
    target_input_type: str = ""
    target_output_type: str = ""
    payload_mode: HandoffPayloadMode = "unknown"
    parsed_output_status: HandoffParsedOutputStatus = "not_applicable"
    compatibility_status: HandoffCompatibilityStatus = "unknown"
    compatibility_notes: list[str] = Field(default_factory=list)

    @field_validator(
        "contract_version",
        "source_agent",
        "target_agent",
        "source_output_type",
        "target_input_type",
        "target_output_type",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("compatibility_notes", mode="before")
    @classmethod
    def _clean_notes(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]


class HandoffAdaptationAssessment(BaseModel):
    """Whether an adapted payload preserved explicit operator ask shape."""

    source_agent: str = ""
    target_agent: str = ""
    status: HandoffAdaptationStatus = "compatible"
    required_fields: list[str] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    lossy_fields: list[str] = Field(default_factory=list)
    defaulted_policy_fields: list[str] = Field(default_factory=list)
    safe_next_action: str = ""


def assess_handoff_adaptation(
    *,
    source_agent: str,
    target_agent: str,
    source_plan: Any,
    target_payload: Any,
) -> HandoffAdaptationAssessment:
    """Compare explicit source ask shape with the adapted target payload."""

    source = _mapping(source_plan)
    target = _mapping(target_payload)
    source_shape = _mapping(source.get("ask_shape"))
    target_shape = _mapping(target.get("ask_shape"))
    required = [
        field_name
        for field_name in _ASK_SHAPE_FIELDS
        if _ask_shape_value_is_explicit(field_name, source_shape.get(field_name))
    ]
    missing = [
        field_name
        for field_name in required
        if not _ask_shape_value_is_explicit(field_name, target_shape.get(field_name))
    ]
    lossy = [
        field_name
        for field_name in required
        if field_name not in missing
        and _normalized_ask_shape_value(source_shape.get(field_name))
        != _normalized_ask_shape_value(target_shape.get(field_name))
    ]
    defaulted = [
        field_name
        for field_name in _ASK_SHAPE_FIELDS
        if field_name not in required
        and _ask_shape_value_is_explicit(field_name, target_shape.get(field_name))
    ]
    unsafe_loss = any(
        field_name in _SAFETY_CRITICAL_ASK_SHAPE_FIELDS
        for field_name in [*missing, *lossy]
    )
    if unsafe_loss:
        status: HandoffAdaptationStatus = "blocked"
        next_action = "Restore the lost safety or stop boundary before specialist execution."
    elif missing or lossy:
        status = "needs_clarification"
        next_action = "Restore or clarify the missing ask-shape fields before execution."
    elif defaulted:
        status = "compatible_with_warnings"
        next_action = "Review adapter-defaulted policy fields before execution."
    else:
        status = "compatible"
        next_action = "Continue with the adapted specialist input."
    return HandoffAdaptationAssessment(
        source_agent=str(source_agent or "").strip(),
        target_agent=str(target_agent or "").strip(),
        status=status,
        required_fields=required,
        missing_required_fields=missing,
        lossy_fields=lossy,
        defaulted_policy_fields=defaulted,
        safe_next_action=next_action,
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return value if isinstance(value, dict) else {}


def _ask_shape_value_is_explicit(field_name: str, value: Any) -> bool:
    if field_name == "source_type_preference":
        return bool(value) if isinstance(value, list | tuple | set) else False
    if field_name == "stop_condition":
        return bool(str(value or "").strip())
    return str(value or "").strip() not in {"", "unspecified"}


def _normalized_ask_shape_value(value: Any) -> Any:
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip().lower() for item in value if str(item or "").strip())
    return str(value or "").strip().lower()


def build_handoff_type_contract(
    *,
    source_agent: str,
    target_agent: str,
    source_output_type: str,
    target_input_type: str,
    target_output_type: str = "",
    payload_mode: HandoffPayloadMode = "native",
    parsed_output_status: HandoffParsedOutputStatus = "parsed",
    compatibility_status: HandoffCompatibilityStatus | None = None,
    compatibility_notes: list[str] | None = None,
) -> HandoffTypeContract:
    """Build a standard compatibility record with conservative status defaults."""

    status = compatibility_status
    if status is None:
        if parsed_output_status == "malformed" or payload_mode == "malformed":
            status = "incompatible"
        elif not source_output_type or not target_input_type:
            status = "unknown"
        elif parsed_output_status in {"missing", "text"}:
            status = "warning"
        else:
            status = "compatible"
    return HandoffTypeContract(
        source_agent=source_agent,
        target_agent=target_agent,
        source_output_type=source_output_type,
        target_input_type=target_input_type,
        target_output_type=target_output_type,
        payload_mode=payload_mode,
        parsed_output_status=parsed_output_status,
        compatibility_status=status,
        compatibility_notes=compatibility_notes or [],
    )
