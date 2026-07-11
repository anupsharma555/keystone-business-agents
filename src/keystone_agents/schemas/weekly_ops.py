"""Typed bounded inputs for the Chief of Staff weekly operations packet."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WeeklyOpsWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_min: str
    time_max: str
    timezone: str = "America/New_York"
    packet_date: str


class WeeklySlackSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_ts: str
    summary: str = Field(min_length=1, max_length=500)
    action_owner: str = Field(default="", max_length=120)
    source_link: str = Field(default="", max_length=500)


class WeeklyGmailThreadSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    subject: str = Field(min_length=1, max_length=240)
    sender_label: str = Field(default="", max_length=160)
    latest_at: str = ""
    summary: str = Field(min_length=1, max_length=500)
    follow_up: str = Field(default="", max_length=300)


class WeeklyCompletedAgentRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_name: str = Field(min_length=1, max_length=120)
    completed_at: str
    outcome_summary: str = Field(min_length=1, max_length=500)
    route: str = ""
    work_item_id: str = ""
    usage_summary: str = Field(default="", max_length=240)
    receipt_reference: str = Field(default="", max_length=500)
    carry_forward: str = Field(default="", max_length=300)
    packet_role: Literal["primary", "supporting", "operational_health_only"]
    relevance_reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def run_identity_is_explicit(self) -> WeeklyCompletedAgentRunSummary:
        if not self.run_id.strip() or not self.completed_at.strip():
            raise ValueError("Completed agent runs require run_id and completed_at.")
        return self


class WeeklyCalendarEventSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    title: str = Field(min_length=1, max_length=240)
    start: str
    end: str = ""
    is_recurring: bool = False
    recurring_event_id: str = ""

    @model_validator(mode="after")
    def recurring_identity_is_explicit(self) -> WeeklyCalendarEventSummary:
        if self.is_recurring and not self.recurring_event_id.strip():
            raise ValueError("Recurring Calendar items require recurring_event_id.")
        return self


class WeeklyOpsAssemblyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: WeeklyOpsWindow
    slack: list[WeeklySlackSummary] = Field(default_factory=list, max_length=40)
    gmail: list[WeeklyGmailThreadSummary] = Field(default_factory=list, max_length=30)
    completed_runs: list[WeeklyCompletedAgentRunSummary] = Field(
        default_factory=list,
        max_length=40,
    )
    calendar: list[WeeklyCalendarEventSummary] = Field(default_factory=list, max_length=100)
