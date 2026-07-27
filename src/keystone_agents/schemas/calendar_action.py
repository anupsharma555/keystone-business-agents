"""Structured model interpretation for one bounded Calendar action."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CalendarActionInterpretation(BaseModel):
    """Source-grounded fields proposed by the Calendar interpretation agent."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["none", "read", "create", "update", "delete"]
    read_scope: Literal["single_event", "time_window", "filtered_window"] = (
        "single_event"
    )
    read_selection: Literal["all", "next"] = "all"
    read_selection_source_text: str = ""
    calendar_scope: Literal[
        "configured",
        "selected_readable",
        "all_readable",
    ] = "configured"
    calendar_scope_source_text: str = ""
    query: str = ""
    query_source_text: str = ""
    date_scope: Literal["unspecified", "today", "tomorrow", "specific_date"] = (
        "unspecified"
    )
    operation_source_text: str = ""
    title: str = ""
    title_source_text: str = ""
    start_date: str = ""
    date_source_text: str = ""
    end_date: str = ""
    end_date_source_text: str = ""
    start_time: str = ""
    time_source_text: str = ""
    end_time: str = ""
    repeat_each_day: bool | None = None
    timezone: str = ""
    timezone_source_text: str = ""
    description: str = ""
    description_source_text: str = ""
    description_from_payload: bool = False
    description_mode: Literal["replace", "append"] = "replace"
    all_day: bool | None = None
    event_id: str = ""
    event_id_source_text: str = ""
    event_reference: str = ""
    event_reference_source_text: str = ""
    ambiguities: list[str] = Field(default_factory=list)

    @field_validator(
        "title",
        "title_source_text",
        "operation_source_text",
        "read_selection_source_text",
        "calendar_scope_source_text",
        "query",
        "query_source_text",
        "start_date",
        "date_source_text",
        "end_date",
        "end_date_source_text",
        "start_time",
        "time_source_text",
        "end_time",
        "timezone",
        "timezone_source_text",
        "description",
        "description_source_text",
        "event_id",
        "event_id_source_text",
        "event_reference",
        "event_reference_source_text",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @field_validator("ambiguities", mode="before")
    @classmethod
    def _clean_ambiguities(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            " ".join(str(item or "").split()).strip()
            for item in values
            if " ".join(str(item or "").split()).strip()
        ]


class CalendarActionInterpretationInput(BaseModel):
    """Bounded prompt input containing the raw request and deterministic plan."""

    request_text: str
    thread_context: str = ""
    event_fact_text: str = ""
    description_payload_present: bool = False
    description_payload_kind: str = ""
    description_payload_length: int = 0
    description_payload_sha256: str = ""
    deterministic_plan: dict[str, object]

    def to_prompt(self) -> str:
        import json

        return "\n".join(
            [
                "Interpret one Calendar action from the exact operator request.",
                "",
                f"Operator directive: {self.request_text}",
                f"Thread context: {self.thread_context or '(none)'}",
                f"Event fact evidence: {self.event_fact_text or '(none)'}",
                (
                    "Event-description payload: "
                    + (
                        f"present; kind={self.description_payload_kind or 'text'}; "
                        f"characters={self.description_payload_length}; "
                        f"sha256={self.description_payload_sha256}"
                        if self.description_payload_present
                        else "not present"
                    )
                ),
                "",
                "Deterministic plan JSON:",
                json.dumps(self.deterministic_plan, ensure_ascii=True, sort_keys=True),
                "",
                "Return only a CalendarActionInterpretation.",
            ]
        )


class CalendarLookupSynthesis(BaseModel):
    """Semantic selection over one bounded, provider-verified event set."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["matched", "ambiguous", "no_match"]
    selected_event_indexes: list[int] = Field(default_factory=list, max_length=3)
    related_event_groups: list[list[int]] = Field(default_factory=list, max_length=3)
    selection_reason: str = ""
    limitations: list[str] = Field(default_factory=list)

    @field_validator("selected_event_indexes", mode="before")
    @classmethod
    def _clean_indexes(cls, value: object) -> list[int]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(dict.fromkeys(int(item) for item in values if item is not None))

    @field_validator("related_event_groups", mode="before")
    @classmethod
    def _clean_related_event_groups(cls, value: object) -> list[list[int]]:
        groups = value if isinstance(value, list | tuple) else []
        cleaned: list[list[int]] = []
        for group in groups:
            values = group if isinstance(group, list | tuple | set) else [group]
            indexes = list(
                dict.fromkeys(int(item) for item in values if item is not None)
            )
            if len(indexes) >= 2 and indexes not in cleaned:
                cleaned.append(indexes)
        return cleaned

    @field_validator("selection_reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @field_validator("limitations", mode="before")
    @classmethod
    def _clean_limitations(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            " ".join(str(item or "").split()).strip()
            for item in values
            if " ".join(str(item or "").split()).strip()
        ]


class CalendarLookupSynthesisInput(BaseModel):
    """Prompt input containing only the request and bounded Calendar metadata."""

    request_text: str
    lookup_target: str = ""
    response_scope: Literal[
        "focused",
        "full_window",
        "full_window_with_focus",
    ] = "focused"
    events: list[dict[str, object]]

    def to_prompt(self) -> str:
        import json

        return "\n".join(
            [
                "Select the Calendar event or events that answer the operator request.",
                "",
                f"Operator request: {self.request_text}",
                f"Authoritative lookup target: {self.lookup_target or '(none)'}",
                f"Authoritative response scope: {self.response_scope}",
                "",
                "Provider-verified candidate events JSON:",
                json.dumps(self.events, ensure_ascii=True, sort_keys=True),
                "",
                "Return only a CalendarLookupSynthesis.",
            ]
        )
