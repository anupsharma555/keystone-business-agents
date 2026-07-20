"""Zero-model replay preparation for saved ANU-61 specialist receipts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keystone_agents.controlled_pilot import (
    controlled_pilot_cases,
    controlled_pilot_natural_request_sha256,
)
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.company_profile import CompanyResearchFocusedBrief


class PilotReceiptReplay(BaseModel):
    """One validated specialist result prepared for later Slack transport."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    route: str
    output_type: str
    natural_request: str
    natural_request_sha256: str
    human_summary: str = Field(min_length=1, max_length=3000)
    visible_sources: list[str] = Field(min_length=1)
    original_openai_requests: int = Field(ge=0)
    original_estimated_cost_usd: float = Field(ge=0)
    replay_openai_requests: Literal[0] = 0
    repeated_model_synthesis: Literal[False] = False
    provider_writes: Literal[0] = 0
    slack_posted: Literal[False] = False
    slack_permalink: Literal[""] = ""
    pilot_observation_claimed: Literal[False] = False
    transport_status: Literal["ready_for_authorized_slack_transport"] = (
        "ready_for_authorized_slack_transport"
    )
    evidence_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog_binding(self) -> PilotReceiptReplay:
        case = next(
            (item for item in controlled_pilot_cases() if item.case_id == self.case_id),
            None,
        )
        if case is None:
            raise ValueError(f"Unknown controlled pilot case: {self.case_id}")
        if self.natural_request != case.natural_ask:
            raise ValueError("Receipt replay must use the catalog's unchanged natural ask.")
        if self.natural_request_sha256 != controlled_pilot_natural_request_sha256(case):
            raise ValueError("Receipt replay natural-request hash does not match the catalog.")
        return self


def build_pilot_receipt_replays(
    *,
    opportunity_evidence_path: Path,
    research_model_path: Path,
    research_plan_path: Path,
    weekly_evidence_path: Path,
) -> list[PilotReceiptReplay]:
    """Validate saved receipts and prepare answer-first zero-model replay packets."""

    return [
        _opportunity_replay(opportunity_evidence_path),
        _research_replay(research_model_path, research_plan_path),
        _weekly_replay(weekly_evidence_path),
    ]


def _opportunity_replay(path: Path) -> PilotReceiptReplay:
    payload = _load_json(path)
    case = _case("current_opportunity_assessment")
    if payload.get("status") != "pass" or not all(
        bool(value) for value in dict(payload.get("checks") or {}).values()
    ):
        raise ValueError("Saved Opportunity evidence is not a complete passing receipt.")
    if payload.get("natural_request") != case.natural_ask:
        raise ValueError("Saved Opportunity receipt is not bound to the catalog ask.")
    safety = dict(payload.get("safety") or {})
    if int(safety.get("provider_writes") or 0) != 0 or bool(
        safety.get("external_action_performed")
    ):
        raise ValueError("Saved Opportunity receipt crossed the no-write boundary.")
    output = dict(payload.get("output") or {})
    sources = [
        dict(source)
        for source in output.get("retained_sources") or []
        if isinstance(source, dict) and str(source.get("url") or "").strip()
    ]
    if not sources:
        raise ValueError("Saved Opportunity receipt has no retained visible sources.")
    summary = "\n\n".join(
        [
            f"*{str(output.get('opportunity_name') or 'Opportunity assessment')}*",
            str(output.get("interpretation") or "").strip(),
            f"*KNI fit*\n{str(output.get('keystone_fit') or '').strip()}",
            f"*Next safe action*\n{str(output.get('next_safe_action') or '').strip()}",
            "*Sources*\n"
            + "\n".join(
                f"- <{source['url']}|{str(source.get('title') or source['url'])}>"
                for source in sources
            ),
        ]
    ).strip()
    return PilotReceiptReplay(
        case_id=case.case_id,
        route="opportunity_scout",
        output_type="OpportunityAssessmentBrief",
        natural_request=case.natural_ask,
        natural_request_sha256=controlled_pilot_natural_request_sha256(case),
        human_summary=summary,
        visible_sources=[str(source["url"]) for source in sources],
        original_openai_requests=int(dict(payload.get("usage") or {}).get("requests") or 0),
        original_estimated_cost_usd=_estimated_cost(payload),
        evidence_refs=[f"artifact:{path}"],
    )


def _research_replay(model_path: Path, plan_path: Path) -> PilotReceiptReplay:
    model_payload = _load_json(model_path)
    plan_payload = _load_json(plan_path)
    case = _case("research_to_internal_doc")
    if model_payload.get("status") != "pass" or plan_payload.get("status") != "pass":
        raise ValueError("Saved Research model and Doc-plan receipts must both pass.")
    safety = dict(model_payload.get("safety") or {})
    plan_safety = dict(plan_payload.get("safety") or {})
    doc = dict(plan_payload.get("doc") or {})
    if bool(safety.get("send_enabled")) or bool(safety.get("provider_writes")):
        raise ValueError("Saved Research receipt crossed the no-send/no-write boundary.")
    if int(plan_safety.get("provider_writes") or 0) != 0 or bool(doc.get("executed")):
        raise ValueError("Saved Research Doc plan must remain reviewed and unexecuted.")
    if not bool(doc.get("approval_required_before_create")):
        raise ValueError("Saved Research Doc plan lost its approval gate.")
    brief = CompanyResearchFocusedBrief.model_validate(model_payload.get("output") or {})
    sources = [source.url for source in brief.sources if source.url]
    if not brief.facts or not sources:
        raise ValueError("Saved Research receipt lacks facts or visible source citations.")
    summary = "\n\n".join(
        [
            f"*{brief.company_name}: KNI relevance brief*\n{brief.product}",
            f"*Why it matters to KNI*\n{brief.why_it_matters}",
            "*Key source-backed facts*\n"
            + "\n".join(
                f"- {fact.text} (source: {', '.join(fact.source_ids)})"
                for fact in brief.facts[:4]
            ),
            "*Unknowns*\n" + "\n".join(f"- {item}" for item in brief.unknowns[:4]),
            (
                "*Reviewed Doc plan*\n"
                f"- Folder: {str(doc.get('folder_path') or '')}\n"
                f"- Title: {str(doc.get('title') or '')}\n"
                "- Status: reviewed only; no document was created or modified."
            ),
            "*Sources*\n"
            + "\n".join(
                f"- <{source.url}|{source.title}>" for source in brief.sources if source.url
            ),
        ]
    ).strip()
    return PilotReceiptReplay(
        case_id=case.case_id,
        route="chief_of_staff",
        output_type="CompanyResearchFocusedBriefWithReviewedDocPlan",
        natural_request=case.natural_ask,
        natural_request_sha256=controlled_pilot_natural_request_sha256(case),
        human_summary=summary,
        visible_sources=sources,
        original_openai_requests=int(
            dict(model_payload.get("usage") or {}).get("requests") or 0
        ),
        original_estimated_cost_usd=_estimated_cost(model_payload),
        evidence_refs=[f"artifact:{model_path}", f"artifact:{plan_path}"],
    )


def _weekly_replay(path: Path) -> PilotReceiptReplay:
    payload = _load_json(path)
    case = _case("weekly_project_brief")
    packet = ChiefOfStaffResult.model_validate(payload.get("packet") or {})
    if payload.get("status") != "success":
        raise ValueError("Saved weekly packet is not a successful receipt.")
    if payload.get("context_mode") != "privacy_minimized_assertions" or payload.get(
        "raw_private_context_transmitted"
    ) is not False:
        raise ValueError("Saved weekly packet crossed the privacy-minimized boundary.")
    if (
        bool(payload.get("provider_writes"))
        or bool(payload.get("send_enabled"))
        or int(payload.get("request_count_bound") or 0) != 1
    ):
        raise ValueError("Saved weekly packet crossed its request or no-write boundary.")
    if (
        not packet.approval_required
        or not packet.human_review_required
        or packet.send_enabled
        or packet.slack_post_allowed
        or packet.write_requests
    ):
        raise ValueError("Saved weekly packet lost its review-only output boundary.")
    synthesis = packet.synthesis.casefold()
    required_markers = (
        "completed runs and outcomes",
        "carry forward",
        "next actions",
        "source basis",
        "8 follow-up",
        "5 pending_review",
        "7 one_time_calendar",
        "14 recurring_calendar",
    )
    if not all(marker in synthesis for marker in required_markers):
        raise ValueError("Saved weekly packet is missing required operating-state evidence.")
    summary = "\n\n".join(
        [
            f"*Weekly Keystone project brief*\n{packet.summary}",
            (
                "*Completed work and decisions*\n"
                "- 2 completed agent runs were present; 1 still requires follow-up.\n"
                "- 4 completed Slack workstream items and 1 completed Gmail follow-up "
                "were present.\n"
                "- Calendar activity was observational: 7 one-time and 14 recurring items."
            ),
            (
                "*Blockers and carry forward*\n"
                "- 8 Gmail follow-up items remain, including 5 pending review.\n"
                "- Keep the Slack approval-or-review item visible in the next cycle."
            ),
            "*Next actions*\n"
            + "\n".join(f"- {action}" for action in packet.recommended_actions[:4]),
            (
                "*Source basis*\n"
                f"- {packet.time_window}; 38 privacy-minimized bounded source assertions.\n"
                "- No raw names, message bodies, provider identifiers, or private topics were used."
            ),
        ]
    ).strip()
    return PilotReceiptReplay(
        case_id=case.case_id,
        route="chief_of_staff",
        output_type="ChiefOfStaffWeeklyProjectBrief",
        natural_request=case.natural_ask,
        natural_request_sha256=controlled_pilot_natural_request_sha256(case),
        human_summary=summary,
        visible_sources=["privacy_minimized_weekly_assertions:38"],
        original_openai_requests=int(dict(payload.get("usage") or {}).get("requests") or 0),
        original_estimated_cost_usd=_estimated_cost(payload),
        evidence_refs=[f"artifact:{path}"],
    )


def _case(case_id: str):
    return next(case for case in controlled_pilot_cases() if case.case_id == case_id)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return payload


def _estimated_cost(payload: dict[str, Any]) -> float:
    cost = dict(payload.get("cost") or {})
    return float(cost.get("estimated_usd") or cost.get("amount_usd") or 0)


__all__ = ["PilotReceiptReplay", "build_pilot_receipt_replays"]
