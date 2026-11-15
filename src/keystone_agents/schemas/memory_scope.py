"""Host-owned Slack memory scope and explicit capture provenance.

These bindings authorize local admission, not provider actions. Legacy records
without this versioned scope remain available only through legacy retrieval.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MEMORY_SCOPE_KEY = "slack_memory_scope"
MEMORY_CAPTURE_KEY = "memory_capture_provenance"
SCOPED_FACT_TYPES = frozenset({
    "operator_strategy", "project_goal", "project_constraint", "project_decision",
    "operator_reference", "human_feedback", "avoidance_rule",
})


class SlackMemoryScope(BaseModel):
    """Exact immutable destination and subject, supplied by the authenticated host."""

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    schema_version: Literal["keystone.slack.memory_scope.v1"] = "keystone.slack.memory_scope.v1"
    team_id: str = Field(pattern=r"^T[A-Z0-9_-]{2,79}$")
    channel_id: str = Field(pattern=r"^[CG][A-Z0-9_-]{2,79}$")
    user_id: str = Field(pattern=r"^[UW][A-Z0-9_-]{2,79}$")
    project_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")


class MemoryCaptureProvenance(BaseModel):
    """Current authenticated capture request; never an old message's approval."""

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    thread_ts: str = Field(pattern=r"^[0-9]{10,}\.[0-9]{1,6}$")
    request_ts: str = Field(pattern=r"^[0-9]{10,}\.[0-9]{1,6}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def chronological_request(self):
        if Decimal(self.thread_ts) <= 0 or Decimal(self.request_ts) < Decimal(self.thread_ts):
            raise ValueError("Capture request must belong to the current or a later thread turn.")
        return self
