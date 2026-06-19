"""Reusable type compatibility metadata for Keystone handoffs."""

from __future__ import annotations

from typing import Literal

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
