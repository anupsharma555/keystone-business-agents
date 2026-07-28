"""Typed admission result for provider-free composition from selected context."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CompositionAdmissionReason = Literal[
    "admitted",
    "plan_not_provider_free_composition",
    "selected_context_missing",
    "selected_context_not_same_thread",
    "selected_context_not_completed",
    "selected_context_route_not_supported",
]


class ProviderFreeCompositionAdmission(BaseModel):
    """Content-free authority record for one show-only composition step."""

    schema_name: str = "keystone.provider_free_composition_admission.v1"
    composition_allowed: bool = False
    external_use_approval_required: bool = True
    provider_action_allowed: bool = False
    same_thread_verified: bool = False
    source_route: str = ""
    source_run_id: str = ""
    context_kind: str = ""
    reason: CompositionAdmissionReason = "plan_not_provider_free_composition"

