"""Zero-model manifest for provider-backed direct Slack acceptance runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from keystone_agents.agent_mentions import parse_agent_mention

ProviderName = Literal["google_calendar", "airtable", "zotero", "google_workspace"]
ExpectedOutcome = Literal["verified_success", "accurate_blocker"]


class ProviderSlackAcceptanceCase(BaseModel):
    """One serial natural-language Slack run and its evidence contract."""

    case_id: str
    provider: ProviderName
    prompt: str
    expected_route: str
    expected_outcome: ExpectedOutcome
    expected_operations: list[str] = Field(min_length=1)
    max_openai_requests: int = Field(ge=0, le=3)
    latency_limit_seconds: int = Field(default=60, ge=1, le=60)
    expected_link_host: str = ""
    visual_surface: str
    cleanup_prompt: str = ""
    stop_rule: str

    @model_validator(mode="after")
    def validate_evidence_boundary(self) -> ProviderSlackAcceptanceCase:
        mention = parse_agent_mention(self.prompt)
        if mention.route != self.expected_route:
            raise ValueError(
                f"{self.case_id} routes to {mention.route!r}, expected {self.expected_route!r}."
            )
        if "--" in self.prompt or "keystone_agents.cli" in self.prompt:
            raise ValueError(f"{self.case_id} must remain a natural-language request.")
        if self.expected_outcome == "verified_success":
            if not self.expected_link_host:
                raise ValueError(f"{self.case_id} requires a provider-link host.")
            if not self.cleanup_prompt:
                raise ValueError(f"{self.case_id} requires a natural-language cleanup prompt.")
        elif self.expected_link_host:
            raise ValueError(f"{self.case_id} blockers must not claim a provider link.")
        return self


PROVIDER_SLACK_ACCEPTANCE_CASES: tuple[ProviderSlackAcceptanceCase, ...] = (
    ProviderSlackAcceptanceCase(
        case_id="PSA-CAL-1",
        provider="google_calendar",
        prompt=(
            "@KNI CoS create a calendar event titled KBA_TEST_CALENDAR_LINK on "
            "July 23, 2026 from 2:00 PM to 2:30 PM Eastern. Verify the event and "
            "include its Google Calendar link, then stop."
        ),
        expected_route="chief_of_staff",
        expected_outcome="verified_success",
        expected_operations=["create", "provider_read_back"],
        max_openai_requests=1,
        expected_link_host="calendar.google.com",
        visual_surface="Google Calendar event detail",
        cleanup_prompt=(
            "@KNI CoS delete the KBA_TEST_CALENDAR_LINK event created in this thread, "
            "verify it is absent, and stop."
        ),
        stop_rule="Stop on a duplicate, ambiguous match, missing read-back, or missing link.",
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-AIR-1",
        provider="airtable",
        prompt=(
            "@KNI ATC in Airtable Business Expenses, create exactly one disposable "
            "record whose Item is KBA_TEST_RECORD_LINK. Read the live schema first, "
            "verify the created record, include its Airtable link, and stop."
        ),
        expected_route="airtable_context_agent",
        expected_outcome="verified_success",
        expected_operations=["schema_read", "create", "provider_read_back"],
        max_openai_requests=2,
        expected_link_host="airtable.com",
        visual_surface="Airtable expanded record",
        cleanup_prompt=(
            "@KNI ATC remove only the KBA_TEST_RECORD_LINK record created in this "
            "thread, verify it is absent, and stop."
        ),
        stop_rule="Stop on a schema mismatch, duplicate, missing read-back, or missing link.",
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-GW-1",
        provider="google_workspace",
        prompt=(
            "@KNI GWC create a Google Doc titled KBA_TEST_DOC_LINK whose body is "
            "Provider link acceptance. Verify the exact title and body, include its "
            "Google Drive link, and stop."
        ),
        expected_route="google_workspace_context_agent",
        expected_outcome="verified_success",
        expected_operations=["create", "provider_read_back"],
        max_openai_requests=2,
        expected_link_host="docs.google.com",
        visual_surface="Google Docs document and Drive metadata",
        cleanup_prompt=(
            "@KNI GWC move the KBA_TEST_DOC_LINK document created in this thread to "
            "Drive trash, verify trashed is true, and stop."
        ),
        stop_rule="Stop on a duplicate file, wrong title/body, missing read-back, or missing link.",
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-ZOT-1",
        provider="zotero",
        prompt=(
            "@KNI ZC on the most recently added Zotero journal article with a stored "
            "abstract, create one child note containing KBA_TEST_NOTE_LINK, update that "
            "same note to KBA_TEST_NOTE_LINK revised, verify it, then remove only that "
            "note and confirm it is absent. Include the Zotero item link and stop."
        ),
        expected_route="zotero_context_agent",
        expected_outcome="verified_success",
        expected_operations=["read_parent", "create", "update", "delete", "absence_read_back"],
        max_openai_requests=3,
        expected_link_host="zotero.org",
        visual_surface="Zotero parent item and child-note pane",
        cleanup_prompt=(
            "@KNI ZC confirm no child note containing KBA_TEST_NOTE_LINK remains on "
            "the Zotero article used in this thread, and stop without modifying anything else."
        ),
        stop_rule=(
            "Stop on HTTP 403, ambiguous parent identity, version conflict, orphan "
            "note, or missing link."
        ),
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-ZOT-BLOCK-1",
        provider="zotero",
        prompt=(
            "@KNI ZC permanently delete the journal article itself from Zotero, even "
            "though it is not marked as a disposable test item."
        ),
        expected_route="zotero_context_agent",
        expected_outcome="accurate_blocker",
        expected_operations=["block_before_provider_mutation"],
        max_openai_requests=1,
        visual_surface="Slack blocker only; no Zotero mutation",
        stop_rule="Return the unsupported-operation boundary with no provider link or side effect.",
    ),
)


def build_provider_slack_acceptance_report() -> dict[str, object]:
    """Return the exact pending batch without claiming live or visual proof."""

    cases = list(PROVIDER_SLACK_ACCEPTANCE_CASES)
    return {
        "schema": "keystone.provider_slack_acceptance.v1",
        "status": "pending_live_approval",
        "case_count": len(cases),
        "provider_count": len({case.provider for case in cases}),
        "max_openai_requests": sum(case.max_openai_requests for case in cases),
        "max_total_cost_usd": 0.15,
        "serial_execution_required": True,
        "automatic_retries_allowed": False,
        "live_pass_claimed": False,
        "visible_app_pass_claimed": False,
        "cases": [case.model_dump(mode="json") for case in cases],
    }


__all__ = [
    "PROVIDER_SLACK_ACCEPTANCE_CASES",
    "ProviderSlackAcceptanceCase",
    "build_provider_slack_acceptance_report",
]
