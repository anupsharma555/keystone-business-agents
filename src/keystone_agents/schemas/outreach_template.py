"""Schema for repo-backed outreach templates."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from keystone_agents.schemas.approval import ApprovalScope, normalize_approval_scope

TemplateChannel = Literal["email", "linkedin", "multi_channel"]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\u2014", "-").split()).strip()


class OutreachTemplateBlock(BaseModel):
    """One structural block in an outreach template."""

    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    guidance: str = Field(min_length=1)
    required: bool = True

    @field_validator("block_id", "label", "guidance", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("block_id", "label", "guidance")
    @classmethod
    def _no_em_dash(cls, value: str) -> str:
        if "\u2014" in value:
            raise ValueError("template text must not contain em dashes")
        return value


class OutreachTemplateRecord(BaseModel):
    """Canonical repo-backed outreach template.

    Templates guide structure and constraints only. They never approve external use, enable
    sending, or allow unsupported claims.
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    channel: TemplateChannel
    use_case: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    required_context: list[str] = Field(min_length=1)
    optional_context: list[str] = Field(default_factory=list)
    structure_blocks: list[OutreachTemplateBlock] = Field(min_length=1)
    cta_style: str = Field(min_length=1)
    tone_constraints: list[str] = Field(min_length=1)
    safety_constraints: list[str] = Field(min_length=1)
    good_for: list[str] = Field(default_factory=list)
    avoid_when: list[str] = Field(default_factory=list)
    max_words: int = Field(ge=20, le=250)
    approval_scope: ApprovalScope = ApprovalScope.EXTERNAL_USE
    send_enabled: bool = False

    @field_validator(
        "template_id",
        "version",
        "name",
        "use_case",
        "stage",
        "cta_style",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator(
        "required_context",
        "optional_context",
        "tone_constraints",
        "safety_constraints",
        "good_for",
        "avoid_when",
        mode="before",
    )
    @classmethod
    def _clean_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("template list fields must be lists")
        return list(dict.fromkeys(item for raw in value if (item := _clean(raw))))

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_approval_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @field_validator("send_enabled")
    @classmethod
    def _send_enabled_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach templates must not enable sending")
        return value

    @model_validator(mode="after")
    def _validate_template_safety(self) -> OutreachTemplateRecord:
        joined = " ".join(
            [
                *self.safety_constraints,
                *self.tone_constraints,
                self.cta_style,
                self.name,
            ]
        ).lower()
        required_safety_terms = {
            "approval": "human approval",
            "send": "no-send constraint",
            "source": "source attribution",
            "phi": "PHI restriction",
            "unsupported": "unsupported-claims restriction",
            "em dash": "no-em-dash rule",
        }
        missing = [label for term, label in required_safety_terms.items() if term not in joined]
        if missing:
            raise ValueError("outreach template missing safety constraints: " + ", ".join(missing))
        if self.approval_scope != ApprovalScope.EXTERNAL_USE:
            raise ValueError("outreach templates must require external-use approval")
        return self

    def prompt_context(self) -> dict[str, Any]:
        """Return compact template context for SDK prompts."""

        return {
            "template_id": self.template_id,
            "version": self.version,
            "name": self.name,
            "channel": self.channel,
            "use_case": self.use_case,
            "stage": self.stage,
            "required_context": self.required_context,
            "optional_context": self.optional_context,
            "structure_blocks": [block.model_dump(mode="json") for block in self.structure_blocks],
            "cta_style": self.cta_style,
            "tone_constraints": self.tone_constraints,
            "safety_constraints": self.safety_constraints,
            "good_for": self.good_for,
            "avoid_when": self.avoid_when,
            "max_words": self.max_words,
            "approval_scope": self.approval_scope.value,
            "send_enabled": False,
        }
