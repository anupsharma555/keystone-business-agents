"""Zero-model manifest for provider-backed direct Slack acceptance runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from keystone_agents.agent_mentions import parse_agent_mention

ProviderName = Literal["airtable", "gmail", "zotero", "google_workspace"]
ExpectedOutcome = Literal["verified_success", "accurate_blocker"]
CleanupMode = Literal["self_contained", "read_only", "separate_prompt", "none"]


class ProviderSlackAcceptanceCase(BaseModel):
    """One serial natural-language Slack run and its evidence contract."""

    case_id: str
    action_id: str
    thread_group: str
    reply_to_action_id: str = ""
    provider: ProviderName
    prompt: str
    expected_route: str
    expected_owner: str
    expected_outcome: ExpectedOutcome
    expected_operations: list[str] = Field(min_length=1)
    expected_openai_requests: int = Field(ge=0, le=2)
    max_openai_requests: int = Field(ge=0, le=2)
    latency_limit_seconds: int = Field(default=60, ge=1, le=60)
    mutation_policy: str
    cleanup_mode: CleanupMode
    cleanup_prompt: str = ""
    cleanup_verification: str = ""
    provider_link_required: bool = False
    expected_link_host: str = ""
    visual_surface: str
    must_pass_for_merge: bool = False
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
        if self.expected_openai_requests > self.max_openai_requests:
            raise ValueError(
                f"{self.case_id} expected requests exceed the hard per-ask ceiling."
            )
        if self.provider_link_required and not self.expected_link_host:
            raise ValueError(f"{self.case_id} requires a provider-link host.")
        if not self.provider_link_required and self.expected_link_host:
            raise ValueError(
                f"{self.case_id} cannot require a link host when links are optional."
            )
        if self.cleanup_mode == "separate_prompt" and not self.cleanup_prompt:
            raise ValueError(f"{self.case_id} requires a cleanup prompt.")
        if self.cleanup_mode == "self_contained":
            if self.cleanup_prompt:
                raise ValueError(
                    f"{self.case_id} self-contained cleanup must not spend another Slack ask."
                )
            if not self.cleanup_verification:
                raise ValueError(
                    f"{self.case_id} self-contained cleanup requires verification."
                )
        if self.cleanup_mode == "read_only" and self.mutation_policy != "read_only":
            raise ValueError(
                f"{self.case_id} read-only cleanup mode requires a read-only policy."
            )
        if self.reply_to_action_id and self.thread_group == self.action_id:
            raise ValueError(
                f"{self.case_id} follow-up must name a shared thread group, not itself."
            )
        return self


PROVIDER_SLACK_ACCEPTANCE_CASES: tuple[ProviderSlackAcceptanceCase, ...] = (
    ProviderSlackAcceptanceCase(
        case_id="PSA-GW-LIFECYCLE-1",
        action_id="workspace_lifecycle",
        thread_group="workspace_lifecycle",
        provider="google_workspace",
        prompt=(
            "@KNI CoS Please take Google Doc "
            "KBA_TEST_DOC_SLACK_CONTINUITY_20260730 in KNIOps through its full "
            "approved lifecycle. Create it with body “Provider continuity initial.”, "
            "verify it, replace the body with “Provider continuity revised.”, verify "
            "the same document, then move that same document to Drive trash and "
            "confirm it is trashed. Leave no test residue."
        ),
        expected_route="chief_of_staff",
        expected_owner="google_workspace_context_agent",
        expected_outcome="verified_success",
        expected_operations=[
            "create",
            "provider_read_back",
            "same_id_update",
            "trash",
            "trash_read_back",
        ],
        expected_openai_requests=1,
        max_openai_requests=2,
        mutation_policy="one_exact_marked_document_only",
        cleanup_mode="self_contained",
        cleanup_verification="same document_id has trashed=true",
        visual_surface="Google Drive or Docs metadata plus Slack verification",
        must_pass_for_merge=True,
        stop_rule=(
            "Stop on a duplicate, wrong folder, changed document identity, missing "
            "read-back, cleanup failure, or retained test residue."
        ),
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-GMAIL-LIFECYCLE-1",
        action_id="gmail_lifecycle",
        thread_group="gmail_lifecycle",
        provider="gmail",
        prompt=(
            "@KNI CoS Please take Gmail draft "
            "KBA_TEST_DRAFT_SLACK_CONTINUITY_20260730 through its full approved "
            "lifecycle without sending it. Use the configured test account and put "
            "KBA_TEST_DRAFT_SLACK_CONTINUITY_20260730 in both the subject and body. "
            "Create it, verify it, update that same draft, verify it again, then "
            "delete it and confirm absence. Leave no test residue."
        ),
        expected_route="chief_of_staff",
        expected_owner="gmail_triage",
        expected_outcome="verified_success",
        expected_operations=[
            "create_draft",
            "provider_read_back",
            "same_id_update",
            "delete_draft",
            "absence_read_back",
        ],
        expected_openai_requests=1,
        max_openai_requests=2,
        mutation_policy="one_exact_marked_test_draft_only_no_send",
        cleanup_mode="self_contained",
        cleanup_verification="exact draft_id is absent and no message was sent",
        visual_surface="Gmail draft state plus Slack verification",
        must_pass_for_merge=True,
        stop_rule=(
            "Stop on an account mismatch, send attempt, changed draft identity, missing "
            "read-back, cleanup failure, or retained test residue."
        ),
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-AIRTABLE-LIFECYCLE-1",
        action_id="airtable_lifecycle",
        thread_group="airtable_lifecycle",
        provider="airtable",
        prompt=(
            "@KNI CoS In Airtable Business Expenses, take one marked "
            "KBA_TEST_RECORD through its full approved lifecycle. Read the live "
            "schema, create it, verify it, update the same record in place, verify it "
            "again, then delete only that record and confirm absence. Leave no test "
            "residue."
        ),
        expected_route="chief_of_staff",
        expected_owner="airtable_context_agent",
        expected_outcome="verified_success",
        expected_operations=[
            "schema_read",
            "create",
            "provider_read_back",
            "same_id_update",
            "delete",
            "absence_read_back",
        ],
        expected_openai_requests=1,
        max_openai_requests=2,
        mutation_policy="one_exact_marked_record_only",
        cleanup_mode="self_contained",
        cleanup_verification="exact record_id is absent",
        visual_surface="Airtable record state plus Slack verification",
        must_pass_for_merge=True,
        stop_rule=(
            "Stop on a schema mismatch, duplicate, changed record identity, missing "
            "read-back, cleanup failure, or retained test residue."
        ),
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-ZOTERO-READ-1",
        action_id="zotero_ordered_read",
        thread_group="zotero_article_thread",
        provider="zotero",
        prompt=(
            "@KNI ZC Use Zotero to select the most recently added top-level journal "
            "article with a stored abstract. Give me its exact title and summarize "
            "only the stored abstract in no more than 50 words. Do not use web "
            "search, full text, or modify Zotero."
        ),
        expected_route="zotero_context_agent",
        expected_owner="zotero_context_agent",
        expected_outcome="verified_success",
        expected_operations=["ordered_provider_read", "exact_item_receipt"],
        expected_openai_requests=1,
        max_openai_requests=2,
        mutation_policy="read_only",
        cleanup_mode="read_only",
        visual_surface="Zotero item metadata plus Slack answer",
        stop_rule=(
            "Stop on ambiguous ordering, missing stored abstract, web-search use, "
            "full-text use, mutation, or public raw item-key leakage."
        ),
    ),
    ProviderSlackAcceptanceCase(
        case_id="PSA-ZOTERO-FOLLOWUP-1",
        action_id="zotero_agent_switch_followup",
        thread_group="zotero_article_thread",
        reply_to_action_id="zotero_ordered_read",
        provider="zotero",
        prompt=(
            "@KNI BA Using only the Zotero article and stored abstract from this "
            "thread, give me exactly two concise bullets: one reason it may matter to "
            "Keystone and one evidence limitation. Do not search the web, modify "
            "Zotero, create files, or draft or send anything."
        ),
        expected_route="business_research_analyst",
        expected_owner="business_research_analyst",
        expected_outcome="verified_success",
        expected_operations=[
            "same_thread_exact_item_rehydration",
            "agent_switch_synthesis",
        ],
        expected_openai_requests=1,
        max_openai_requests=2,
        mutation_policy="read_only",
        cleanup_mode="read_only",
        visual_surface="Slack thread reply only",
        must_pass_for_merge=True,
        stop_rule=(
            "Stop on a different Zotero item, web search, mutation, file creation, "
            "outbound draft/send, raw item-key leakage, or loss of thread context."
        ),
    ),
)


def build_provider_slack_acceptance_report() -> dict[str, object]:
    """Return the exact pending batch without claiming live or visual proof."""

    cases = list(PROVIDER_SLACK_ACCEPTANCE_CASES)
    action_ids = {case.action_id for case in cases}
    for case in cases:
        if case.reply_to_action_id and case.reply_to_action_id not in action_ids:
            raise ValueError(
                f"{case.case_id} replies to unknown action {case.reply_to_action_id!r}."
            )
    return {
        "schema": "keystone.provider_slack_acceptance.v2",
        "status": "pending_live_approval",
        "case_count": len(cases),
        "preceding_calendar_case_count": 5,
        "combined_case_count": 5 + len(cases),
        "provider_count": len({case.provider for case in cases}),
        "expected_openai_requests": sum(
            case.expected_openai_requests for case in cases
        ),
        "max_openai_requests": sum(case.max_openai_requests for case in cases),
        "max_total_cost_usd": 0.15,
        "serial_execution_required": True,
        "automatic_retries_allowed": False,
        "minimum_pass_count": 4,
        "must_pass_action_ids": [
            case.action_id for case in cases if case.must_pass_for_merge
        ],
        "live_pass_claimed": False,
        "visible_app_pass_claimed": False,
        "cases": [case.model_dump(mode="json") for case in cases],
    }


__all__ = [
    "PROVIDER_SLACK_ACCEPTANCE_CASES",
    "ProviderSlackAcceptanceCase",
    "build_provider_slack_acceptance_report",
]
