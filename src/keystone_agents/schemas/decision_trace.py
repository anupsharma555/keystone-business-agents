"""Optional structured decision traces for debugging and eval triage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DecisionTraceComponent(BaseModel):
    """One named component in a structured decision trace."""

    name: str
    value: str = ""
    numeric_value: float | None = None
    rationale: str = ""

    @field_validator("name", "value", "rationale", mode="before")
    @classmethod
    def _clean_component_text(cls, value: Any) -> str:
        return str(value or "").replace("\u2014", "-").strip()


class DecisionTrace(BaseModel):
    """Prompt-safe summary of how a route, score, or handoff decision was made."""

    selected_route: str = ""
    rejected_routes: list[str] = Field(default_factory=list)
    source_quality_reasons: list[str] = Field(default_factory=list)
    scoring_components: list[DecisionTraceComponent] = Field(default_factory=list)
    safety_gates_applied: list[str] = Field(default_factory=list)
    missing_information_blockers: list[str] = Field(default_factory=list)
    handoff_readiness: str = ""
    notes: list[str] = Field(default_factory=list)

    @field_validator(
        "selected_route",
        "handoff_readiness",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator(
        "rejected_routes",
        "source_quality_reasons",
        "safety_gates_applied",
        "missing_information_blockers",
        "notes",
        mode="before",
    )
    @classmethod
    def _clean_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("scoring_components", mode="before")
    @classmethod
    def _coerce_scoring_components(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return list(value)
        if not isinstance(value, Mapping):
            return [
                DecisionTraceComponent(
                    name="score",
                    value=str(value),
                    numeric_value=float(value) if isinstance(value, int | float) else None,
                )
            ]

        components: list[DecisionTraceComponent] = []
        for key, item in value.items():
            if isinstance(item, int | float):
                components.append(
                    DecisionTraceComponent(
                        name=str(key),
                        value=str(item),
                        numeric_value=float(item),
                    )
                )
            elif isinstance(item, str):
                components.append(DecisionTraceComponent(name=str(key), value=item))
            elif isinstance(item, list | tuple):
                components.append(
                    DecisionTraceComponent(
                        name=str(key),
                        rationale="; ".join(str(part) for part in item if str(part).strip()),
                    )
                )
            else:
                components.append(DecisionTraceComponent(name=str(key), value=str(item)))
        return components
