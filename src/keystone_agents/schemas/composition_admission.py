"""Typed admission result for provider-free composition from selected context."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from keystone_agents.schemas.work_item import WorkItemSourceRef

CompositionAdmissionReason = Literal[
    "admitted",
    "admitted_approved_synthetic_context",
    "plan_not_provider_free_composition",
    "selected_context_missing",
    "selected_context_not_same_thread",
    "selected_context_not_completed",
    "selected_context_route_not_supported",
    "selected_context_source_missing",
    "selected_context_source_unverified",
    "selected_context_source_mismatch",
    "selected_context_ambiguous",
    "selected_context_latest_signal_incomplete",
    "selected_context_explicit_source_mismatch",
]

SignalSourceRoute = Literal["rss_context_agent", "preprints_context_agent"]


class SignalSourceInterpretation(BaseModel):
    """Model commentary retained separately from provider-owned source evidence."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    model_summary: str = ""
    detailed_summary: str = ""
    selection_reason: str = ""
    relevance_to_keystone: str = ""
    limitations: list[str] = Field(default_factory=list, max_length=12)
    interpretation_status: Literal["model_generated_unverified"] = (
        "model_generated_unverified"
    )

    @field_validator(
        "source_id",
        "model_summary",
        "detailed_summary",
        "selection_reason",
        "relevance_to_keystone",
        mode="before",
    )
    @classmethod
    def _clean_text_fields(cls, value: object) -> str:
        return str(value or "").strip()[:1400]

    @field_validator("limitations", mode="before")
    @classmethod
    def _clean_interpretation_limitations(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple) else [value]
        return list(
            dict.fromkeys(
                str(item or "").strip()[:700]
                for item in values
                if str(item or "").strip()
            )
        )[:12]


class VerifiedSignalContext(BaseModel):
    """Trusted selected RSS/Preprints evidence rehydrated from one agent run."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.verified_signal_context.v1"] = (
        "keystone.verified_signal_context.v1"
    )
    source_run_id: str
    source_route: SignalSourceRoute
    verification_status: Literal["verified", "unverified"] = "unverified"
    public_result_status: Literal[
        "verified", "completed", "recovered", "partial", "blocked", "failed"
    ] = "failed"
    source_request_ts: str = ""
    selection_basis: Literal[
        "latest_verified_signal", "explicit_older_signal"
    ] = "latest_verified_signal"
    newer_signal_run_id: str = ""
    newer_signal_status: str = ""
    newer_signal_request_ts: str = ""
    selected_sources: list[WorkItemSourceRef] = Field(default_factory=list, max_length=8)
    source_dates: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list, max_length=12)
    interpretations: list[SignalSourceInterpretation] = Field(
        default_factory=list,
        max_length=8,
    )

    @field_validator(
        "source_run_id",
        "source_request_ts",
        "newer_signal_run_id",
        "newer_signal_status",
        "newer_signal_request_ts",
        mode="before",
    )
    @classmethod
    def _clean_identity_fields(cls, value: object) -> str:
        return str(value or "").strip()[:160]

    @field_validator("limitations", mode="before")
    @classmethod
    def _clean_limitations(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple) else [value]
        return list(
            dict.fromkeys(
                str(item or "").strip()[:700]
                for item in values
                if str(item or "").strip()
            )
        )[:12]

    @field_validator("source_dates", mode="before")
    @classmethod
    def _clean_source_dates(cls, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key or "").strip()[:200]: str(item or "").strip()[:80]
            for key, item in value.items()
            if str(key or "").strip() and str(item or "").strip()
        }

    @model_validator(mode="after")
    def _validate_verified_identity(self) -> VerifiedSignalContext:
        if self.verification_status != "verified":
            return self
        if not self.source_run_id:
            raise ValueError("Verified signal context requires a source run ID.")
        if not self.source_request_ts:
            raise ValueError("Verified signal context requires its Slack request timestamp.")
        if self.public_result_status not in {"verified", "completed", "recovered"}:
            raise ValueError("Verified signal context requires a completed public result.")
        if not self.selected_sources:
            raise ValueError("Verified signal context requires selected sources.")
        source_ids: set[str] = set()
        source_urls: set[str] = set()
        for source in self.selected_sources:
            if not source.source_id or not source.url or not source.title:
                raise ValueError(
                    "Verified signal sources require source_id, title, and URL."
                )
            if source.provider != self.source_route:
                raise ValueError("Verified signal source provider must match its route.")
            if source.source_id in source_ids or source.url in source_urls:
                raise ValueError("Verified signal source identities must be unique.")
            source_ids.add(source.source_id)
            source_urls.add(source.url)
        if set(self.source_dates) != source_ids:
            raise ValueError(
                "Verified signal context requires one publication date per source."
            )
        interpretation_ids = [item.source_id for item in self.interpretations]
        if len(interpretation_ids) != len(set(interpretation_ids)):
            raise ValueError("Signal source interpretations must have unique identities.")
        if set(interpretation_ids).difference(source_ids):
            raise ValueError("Signal interpretations must refer to selected sources.")
        if self.selection_basis == "explicit_older_signal" and not all(
            (
                self.newer_signal_run_id,
                self.newer_signal_status,
                self.newer_signal_request_ts,
            )
        ):
            raise ValueError(
                "An explicit older signal requires the newer run identity and status."
            )
        return self


class ProviderFreeCompositionAdmission(BaseModel):
    """Authority and retained evidence for one show-only composition step."""

    schema_name: str = "keystone.provider_free_composition_admission.v1"
    composition_allowed: bool = False
    external_use_approval_required: bool = True
    provider_action_allowed: bool = False
    same_thread_verified: bool = False
    source_route: str = ""
    source_run_id: str = ""
    context_kind: str = ""
    verified_signal_context: VerifiedSignalContext | None = None
    reason: CompositionAdmissionReason = "plan_not_provider_free_composition"
