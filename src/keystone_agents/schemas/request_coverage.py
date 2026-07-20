"""Shared audit envelope for specialist request-shape coverage."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CoverageStatus = Literal["unassessed", "complete", "partial", "blocked"]
_EMPTY_UNMET_DIMENSION_SENTINELS = frozenset(
    {
        "n/a",
        "nil",
        "no unmet boundaries",
        "no unmet dimensions",
        "none",
        "none identified",
        "not applicable",
    }
)
OutputFormStatus = Literal[
    "unassessed",
    "not_requested",
    "satisfied",
    "partial",
    "unmet",
]
StopConditionStatus = Literal[
    "unassessed",
    "not_requested",
    "satisfied",
    "blocked",
    "violated",
]


class RequestCoverage(BaseModel):
    """How a specialist output covered the operator's interpreted ask."""

    interpreted_request: str = ""
    status: CoverageStatus = "unassessed"
    satisfied_dimensions: list[str] = Field(default_factory=list)
    unmet_dimensions: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete unmet request dimensions. Use an empty list when there are none; "
            "do not emit placeholder strings such as 'None' or 'N/A'."
        ),
    )
    output_form_status: OutputFormStatus = "unassessed"
    stop_condition_status: StopConditionStatus = "unassessed"
    broadened_beyond_request: bool = False
    next_safe_action: str = ""

    @field_validator(
        "satisfied_dimensions",
        "unmet_dimensions",
        mode="before",
    )
    @classmethod
    def clean_dimensions(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                str(item).strip() for item in values if str(item or "").strip()
            )
        )

    @field_validator("unmet_dimensions")
    @classmethod
    def remove_empty_unmet_dimension_sentinels(cls, value: list[str]) -> list[str]:
        """Normalize semantically empty model placeholders to the empty list."""

        return [
            item
            for item in value
            if item.lower().rstrip(".:;") not in _EMPTY_UNMET_DIMENSION_SENTINELS
        ]

    @field_validator("interpreted_request", "next_safe_action", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @model_validator(mode="after")
    def normalize_consistency(self) -> RequestCoverage:
        """Keep an advisory coverage inconsistency from discarding the main result.

        Request coverage is a model-authored audit envelope, not a safety,
        provider-identity, approval, or mutation-receipt gate. Preserve concrete
        unmet evidence, downgrade an inconsistent status to ``unassessed`` or
        ``partial``, and let ``requires_attention`` expose the audit gap.
        """

        overlap = set(self.satisfied_dimensions) & set(self.unmet_dimensions)
        if overlap:
            # A model may repeat one dimension in both lists. Treat the unmet
            # claim as the conservative one without rejecting the full output.
            self.satisfied_dimensions = [
                item for item in self.satisfied_dimensions if item not in overlap
            ]
        if self.stop_condition_status == "violated":
            # A violated stop condition is, by definition, broadening beyond
            # the request. This is a logical normalization, not a new claim.
            self.broadened_beyond_request = True
        if self.status == "complete" and (
            self.unmet_dimensions
            or self.broadened_beyond_request
            or self.output_form_status in {"partial", "unmet"}
            or self.stop_condition_status in {"blocked", "violated"}
        ):
            if self.unmet_dimensions and self.next_safe_action:
                self.status = (
                    "blocked" if self.stop_condition_status == "blocked" else "partial"
                )
            else:
                self.status = "unassessed"
        if self.status in {"partial", "blocked"} and (
            not self.unmet_dimensions or not self.next_safe_action
        ):
            self.status = "unassessed"
        return self

    def requires_attention(self) -> bool:
        return self.status in {"unassessed", "partial", "blocked"} or bool(
            self.unmet_dimensions
        )


__all__ = ["RequestCoverage"]
