"""Shared user-facing response synthesis for WorkItem results."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.work_item import WorkflowRunResult
from keystone_agents.sdk import build_model_settings, build_sdk_agent, compose_instructions


class UserFacingResponseSynthesis(BaseModel):
    """Structured response text synthesized from bounded WorkItem state."""

    title: str = ""
    answer: str
    key_points: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    next_step: str = ""


class UserFacingResponseSynthesisInput(BaseModel):
    """Compact bounded input for final response synthesis."""

    user_request: str
    latest_user_request: str = ""
    agent_name: str
    route: str
    status: str
    advanced: bool
    deterministic_summary: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    next_action: dict[str, Any] | None = None
    manual_plan: dict[str, Any] | None = None
    orchestrator_reviews: list[dict[str, Any]] = Field(default_factory=list)
    audit_notes: list[str] = Field(default_factory=list)

    def to_prompt(self) -> str:
        return "\n".join(
            [
                "Synthesize a clear user-facing response from this bounded WorkItem state.",
                "",
                "Rules:",
                "- Use only the facts in the payload.",
                "- Do not invent sources, counts, amounts, deadlines, or conclusions.",
                "- Treat latest_user_request as the primary objective when present.",
                "- Treat prior thread, WorkItem, artifact, and profile context as background evidence.",
                "- If the latest request is narrower than the artifact set, answer the narrower request first.",
                "- Do not summarize every artifact merely because it is available; select what is relevant.",
                "- Do not present route metadata, artifact ids, or workflow status as the main answer.",
                "- Lead with the substantive answer, not run status or evaluator framing.",
                "- Answer in KNI's operator voice; do not write as an unnamed evaluator.",
                "- Avoid phrases like 'the evidence does answer the core request' or 'the review flagged'.",
                "- State what the current sources support and what remains unknown.",
                "- When the latest request asks for citations, sources, links, URLs, or weblinks, include the available source URLs from payload sources or artifact metadata.",
                "- If requested source URLs are unavailable in the payload, say that explicitly instead of implying they were not found.",
                "- Do not say search was limited when retrieval diagnostics show broad search execution; say independent corroboration or selected evidence is limited instead.",
                "- Put partial, blocked, not decision-ready, and approval/no-send notes in caveats.",
                "- Leave key_points and next_step empty when they would repeat the answer, blockers, or caveats.",
                "- Use Orchestrator review feedback to surface gaps without repeating workflow metadata.",
                "- If the result is blocked or off-target, explain why in caveats instead of opening with it.",
                "- Keep approval/no-send/write safety boundaries intact.",
                "- Do not use 'If you want...' follow-up offers; include only concrete next actions required by the request or safety state.",
                "",
                "Payload JSON:",
                json.dumps(self.model_dump(mode="json"), ensure_ascii=True, sort_keys=True),
            ]
        )


def build_user_response_synthesis_agent(model: str | None = None):
    return build_sdk_agent(
        name="user_response_synthesizer",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "writing_style.md",
            "slack-posting-rules.md",
        ),
        output_type=UserFacingResponseSynthesis,
        tools=[],
        model=model,
        model_settings=build_model_settings(reasoning_effort="low", max_tokens=900),
        enforce_tool_policy=False,
    )


def synthesize_user_facing_work_item_response(
    result: WorkflowRunResult,
    *,
    user_request: str,
    live: bool,
    session: Any | None = None,
) -> UserFacingResponseSynthesis:
    """Run the final live synthesis layer for a deterministic WorkItem result."""

    sdk_result = synthesize_user_facing_work_item_response_sdk_result(
        result,
        user_request=user_request,
        live=live,
        session=session,
    )
    return sdk_result.output


def synthesize_user_facing_work_item_response_sdk_result(
    result: WorkflowRunResult,
    *,
    user_request: str,
    live: bool,
    session: Any | None = None,
) -> Any:
    """Run final response synthesis and return the typed SDK result envelope."""

    synthesis_input = _user_response_synthesis_input(result, user_request=user_request)
    return run_typed_sdk_agent(
        agent=build_user_response_synthesis_agent(),
        typed_input=synthesis_input,
        output_type=UserFacingResponseSynthesis,
        live=live,
        session=session,
        workflow_name="Keystone user-facing response synthesis",
        max_turns=2,
    )


def _user_response_synthesis_input(
    result: WorkflowRunResult,
    *,
    user_request: str,
) -> UserFacingResponseSynthesisInput:
    return UserFacingResponseSynthesisInput(
        user_request=user_request,
        latest_user_request=latest_user_request(user_request),
        agent_name=result.route.value,
        route=result.route.value,
        status=result.status.value,
        advanced=result.advanced,
        deterministic_summary=result.human_summary,
        artifacts=[
            {
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "summary": artifact.summary,
                "approval_state": artifact.approval_state,
                "metadata": _compact_mapping(artifact.metadata),
            }
            for artifact in result.artifact_refs[:8]
        ],
        sources=[
            {
                "title": source.title,
                "url": source.url,
                "source_type": source.source_type,
                "supported_claim": source.supported_claim,
                "key_facts": source.key_facts[:5],
            }
            for source in result.work_item.sources[:8]
        ],
        blockers=[
            {
                "code": blocker.code,
                "message": blocker.message,
                "severity": blocker.severity,
            }
            for blocker in result.blockers[:6]
        ],
        next_action=(
            result.next_action.model_dump(mode="json") if result.next_action is not None else None
        ),
        manual_plan=result.manual_request_plan,
        orchestrator_reviews=_compact_orchestrator_reviews(result),
        audit_notes=result.audit_notes[:8],
    )


def latest_user_request(user_request: str) -> str:
    """Return the newest operator ask from a thread-continuation style request."""

    text = str(user_request or "").strip()
    if not text:
        return ""
    markers = (
        "\nFollow-up:",
        "\nFollow up:",
        "\nLatest request:",
        "\nCurrent request:",
        "\nNew request:",
    )
    lower = text.lower()
    best_index = -1
    best_marker = ""
    for marker in markers:
        index = lower.rfind(marker.lower())
        if index > best_index:
            best_index = index
            best_marker = marker
    if best_index >= 0:
        latest = text[best_index + len(best_marker) :].strip()
        return latest or text
    return text


def format_user_response_synthesis(synthesis: UserFacingResponseSynthesis) -> str:
    """Render synthesized response fields into compact Slack-readable text."""

    answer = _strip_redundant_answer_sections(synthesis.answer.strip())
    answer = _strip_trailing_followup_offer(answer)
    answer, demoted_notes = _demote_leading_status_sentence(answer)
    caveats = [*demoted_notes, *(item.strip() for item in synthesis.caveats if item.strip())]

    lines: list[str] = []
    if synthesis.title.strip():
        lines.append(synthesis.title.strip())
        lines.append("")
    if answer.strip():
        lines.append(answer.strip())
    if caveats:
        lines.extend(["", "Run notes"])
        lines.extend(f"* {item}" for item in caveats)
    return "\n".join(line for line in lines if line is not None).strip()


_REDUNDANT_ANSWER_SECTION_HEADINGS = {
    "key points",
    "next step",
    "next steps",
    "what should happen next",
}
_ANSWER_SECTION_BOUNDARY_HEADINGS = {
    "caveats",
    "limitations",
    "metadata",
    "run metadata",
    "run notes",
    "sources",
    "source links",
    "what remains unknown",
    "what the sources support",
}
_LEADING_STATUS_MARKERS = (
    "partial",
    "not decision-ready",
    "not decision ready",
    "blocked",
    "run is not",
    "run was not",
    "not complete",
)


def _strip_redundant_answer_sections(text: str) -> str:
    """Remove sections duplicated by WorkItem metadata and footer notes."""

    if not text.strip():
        return ""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        heading = _normalized_heading(line)
        if heading in _REDUNDANT_ANSWER_SECTION_HEADINGS:
            skipping = True
            continue
        if skipping and heading in _ANSWER_SECTION_BOUNDARY_HEADINGS:
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def _demote_leading_status_sentence(text: str) -> tuple[str, list[str]]:
    """Move an opening run-status sentence into footer notes when possible."""

    stripped = text.strip()
    if not stripped:
        return "", []
    first_paragraph, separator, rest = stripped.partition("\n\n")
    sentence, remainder = _split_first_sentence(first_paragraph)
    if not sentence or not _looks_like_run_status_sentence(sentence):
        return stripped, []

    remaining_paragraph = remainder.strip()
    remaining_parts = [part for part in (remaining_paragraph, rest.strip()) if part]
    if not remaining_parts:
        return stripped, []
    return "\n\n".join(remaining_parts).strip(), [sentence.strip()]


def _strip_trailing_followup_offer(text: str) -> str:
    """Remove generic trailing offers that distract from the completed answer."""

    stripped = text.strip()
    if not stripped:
        return ""
    paragraphs = stripped.split("\n\n")
    while paragraphs and _looks_like_followup_offer(paragraphs[-1]):
        paragraphs.pop()
    return "\n\n".join(paragraphs).strip()


def _looks_like_followup_offer(paragraph: str) -> bool:
    lower = " ".join(str(paragraph or "").lower().split())
    return lower.startswith(
        (
            "if you want,",
            "if useful,",
            "i can next ",
            "next, i can ",
        )
    )


def _split_first_sentence(text: str) -> tuple[str, str]:
    for index, character in enumerate(text):
        if character in ".!?":
            return text[: index + 1].strip(), text[index + 1 :].strip()
    return text.strip(), ""


def _looks_like_run_status_sentence(sentence: str) -> bool:
    lower = sentence.lower()
    if not any(marker in lower for marker in _LEADING_STATUS_MARKERS):
        return False
    return any(
        marker in lower
        for marker in (
            "run",
            "decision-ready",
            "decision ready",
            "source-backed read",
            "source backed read",
            "blocked",
        )
    )


def _normalized_heading(line: str) -> str:
    stripped = line.strip().strip("*#").strip().rstrip(":").strip()
    if not stripped or stripped.startswith(("-", "*")):
        return ""
    if len(stripped) > 80:
        return ""
    return stripped.lower()


def _compact_mapping(value: dict[str, Any], *, max_items: int = 8) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if len(compact) >= max_items:
            break
        if key in {"retrieval_diagnostics", "source_refs", "sources"}:
            compact[key] = item
            continue
        if isinstance(item, str | int | float | bool) or item is None:
            compact[key] = item
    return compact


def _compact_orchestrator_reviews(result: WorkflowRunResult) -> list[dict[str, Any]]:
    metadata = result.work_item.target.metadata
    raw_reviews = metadata.get("orchestrator_reviews") if isinstance(metadata, dict) else None
    if not isinstance(raw_reviews, list):
        return []
    reviews: list[dict[str, Any]] = []
    for item in raw_reviews[-5:]:
        if not isinstance(item, dict):
            continue
        reviews.append(
            {
                key: item[key]
                for key in (
                    "step",
                    "route",
                    "status",
                    "review_status",
                    "overall_score",
                    "approval_boundary_ok",
                    "observed_gaps",
                    "recommended_next_step",
                )
                if key in item and item[key] not in (None, "", [])
            }
        )
    return reviews
