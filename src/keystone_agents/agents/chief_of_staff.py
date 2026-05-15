"""KNI Chief of Staff agent builder and deterministic routing fallback."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from keystone_agents.automation_inventory import build_automation_inventory_report
from keystone_agents.file_search import append_configured_file_search_tools
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.memory import operator_reference_memory_item
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.automation import (
    AutomationArtifactRef,
    AutomationWriteDestination,
    ChiefOfStaffWriteRequest,
)
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
    ChiefOfStaffSourceRef,
)
from keystone_agents.sdk import Agent, build_model_settings, build_sdk_agent, compose_instructions
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.automation_inventory_tool import (
    inspect_active_work_items,
    list_automation_specs,
    list_channel_automation_bindings,
    list_pending_automation_approvals,
    list_recent_automation_runs,
    summarize_automation_health,
)
from keystone_agents.tools.chief_of_staff_tool import (
    OFFICIAL_OPERATIONS_DOCS,
    _capability_for_topic,
    list_chief_of_staff_context_sources,
    lookup_slack_workflow_capability,
    read_slack_repo_context_file,
    search_official_operations_docs,
    search_slack_repo_context,
    summarize_slack_runtime_config,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.operations_publisher_tool import (
    publish_document_report,
    publish_slack_summary,
    publish_table_mirror,
)

CHIEF_OF_STAFF_REASONING_EFFORT = "low"
CHIEF_OF_STAFF_VERBOSITY = "low"
CHIEF_OF_STAFF_MAX_TOKENS = 2_500


def _chief_of_staff_tools() -> list[Any]:
    return append_configured_file_search_tools(
        "chief_of_staff",
        [
            list_chief_of_staff_context_sources,
            summarize_slack_runtime_config,
            search_slack_repo_context,
            read_slack_repo_context_file,
            lookup_slack_workflow_capability,
            search_official_operations_docs,
            list_automation_specs,
            list_recent_automation_runs,
            list_channel_automation_bindings,
            summarize_automation_health,
            list_pending_automation_approvals,
            inspect_active_work_items,
            publish_document_report,
            publish_table_mirror,
            publish_slack_summary,
            list_local_context_sources,
            search_local_context,
            read_local_context_file,
        ],
    )


URL_PATTERN = re.compile(r"https?://[^\s<>)]+", flags=re.I)
SLACK_HISTORY_DIGEST_MARKER = "Slack channel history digest:"
SLACK_HISTORY_ITEM_RE = re.compile(
    r"^-\s+ts=(?P<ts>\S+)\s+author=(?P<author>\S+)"
    r"(?:\s+title=(?P<title>.*?))?:\s+(?P<body>.*)$"
)
BLOCKED_SIDE_EFFECTS = [
    "direct_slack_post",
    "gmail_send",
    "calendar_create_or_update",
    "repo_write",
    "linkedin_publish",
    "crm_write",
]


def _docs_for_topic(topic: str) -> list[ChiefOfStaffSourceRef]:
    normalized = topic.lower()
    selected = []
    for doc in OFFICIAL_OPERATIONS_DOCS:
        if any(keyword in normalized for keyword in doc.keywords):
            selected.append(doc)
    if not selected:
        selected = [
            doc
            for doc in OFFICIAL_OPERATIONS_DOCS
            if doc.source_type in {"openai_docs", "slack_docs"}
        ][:3]
    return [
        ChiefOfStaffSourceRef(
            title=doc.title,
            url=doc.url,
            source_type=doc.source_type,
            note=doc.note,
        )
        for doc in selected[:4]
    ]


def _extract_target_channel(text: str, fallback: str) -> str:
    match = re.search(r"#([a-z0-9_-]+)", text, flags=re.I)
    if match:
        return match.group(1)
    return fallback


def _extract_digest_field(text: str, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}:\s*(.+)$", text, flags=re.I | re.M)
    return match.group(1).strip() if match else ""


def _looks_like_slack_history_digest_request(text: str) -> bool:
    return SLACK_HISTORY_DIGEST_MARKER in text


def _slack_history_items_from_digest(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in text.splitlines():
        match = SLACK_HISTORY_ITEM_RE.match(line.strip())
        if not match:
            continue
        body = re.sub(r"https?://\S+", "", match.group("body")).strip()
        title = (match.group("title") or "").strip()
        if title == "Slack message":
            title = ""
        if not title:
            title = body.split(".", 1)[0].strip()[:120]
        topic = _slack_history_topic_from_text(title, body)
        metadata = _slack_history_metadata_from_text(body)
        items.append(
            {
                "ts": match.group("ts").strip(),
                "topic": topic,
                "metadata": metadata,
            }
        )
    return items


def _slack_history_topic_from_text(title: str, body: str) -> str:
    topic = ""
    for pattern in (
        r"watchlist generated for [`'\"]*([^`'\".]+)",
        r"\*Query:\*\s*([^*\n]+)",
        r"\bQuery:\s*([^*\n]+)",
        r"\bTopics?:\s*([^|.\n]+(?:\|[^|.\n]+)*)",
        r"\bSummary:\s*([^*\n]+)",
    ):
        match = re.search(pattern, body, flags=re.I)
        if match:
            topic = match.group(1).strip(" `\"'.:")
            break
    if not topic:
        topic = title.strip(" `\"'.:")
    topic = re.sub(r"\*+", "", topic)
    topic = re.sub(r"\bMatched on:.*", "", topic, flags=re.I)
    topic = re.sub(r"\bLink:.*", "", topic, flags=re.I)
    topic = re.sub(r"\bSummary:.*", "", topic, flags=re.I)
    topic = re.sub(r"\s+", " ", topic).strip()
    topic = re.sub(r"\s+\|", " |", topic)
    return _truncate_sentence(topic or "Slack post", 120)


def _slack_history_metadata_from_text(body: str) -> str:
    metadata: list[str] = []
    for label, pattern in (
        ("workflow", r"\*?Workflow:\*?\s*`?([A-Za-z0-9_-]+)`?"),
        ("status", r"\*?Status:\*?\s*`?([A-Za-z0-9_-]+)`?"),
        ("run", r"\*?Run ID:\*?\s*`?([A-Za-z0-9_-]+)`?"),
    ):
        match = re.search(pattern, body, flags=re.I)
        if match:
            metadata.append(f"{label}={match.group(1).strip()}")
    return "; ".join(metadata)


def _format_slack_history_timestamp(ts: str) -> str:
    try:
        posted_at = datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York"))
    except (OverflowError, ValueError):
        return f"Slack ts {ts}"
    return posted_at.strftime("%Y-%m-%d %H:%M %Z")


def _truncate_sentence(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _slack_history_theme(items: list[dict[str, str]]) -> str:
    combined = " ".join(f"{item['topic']} {item['metadata']}" for item in items).lower()
    if "clinicaltrials" in combined or "clinical trials" in combined:
        return (
            "ClinicalTrials.gov watchlist posts around mood, psychosis, AI/ML, "
            "depression, bipolar, ketamine/esketamine, schizophrenia, and early psychosis."
        )
    if any(marker in combined for marker in ("grant", "funding", "nih", "rfa", "foa")):
        return "Funding and grant opportunity updates with emphasis on the linked source material."
    if any(marker in combined for marker in ("paper", "preprint", "pubmed", "article")):
        return "Research article and publication updates from the linked posts."
    return "Recent linked Slack posts from the supplied channel-history digest."


def _plan_slack_history_digest_request(text: str) -> ChiefOfStaffResult:
    channel = _extract_digest_field(text, "Channel")
    channel_id = _extract_digest_field(text, "Channel id")
    items = _slack_history_items_from_digest(text)
    summary_lines = ["Recent posts:", ""]
    if items:
        for index, item in enumerate(items[:8], start=1):
            if index > 1:
                summary_lines.append("")
            summary_lines.append(f"{index}. {item['topic']}")
            summary_lines.append(f"   Posted: {_format_slack_history_timestamp(item['ts'])}")
    else:
        summary_lines.append("- No candidate Slack history items were present in the supplied digest.")
    summary_lines.append("")
    summary_lines.append(f"Theme: {_slack_history_theme(items)}")
    summary = "\n".join(summary_lines)
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text.split(SLACK_HISTORY_DIGEST_MARKER, 1)[0].strip() or text[:240],
        summary=summary,
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="slack-runtime-review",
            command_text=f"Summarize supplied Slack history digest for {channel_id or channel}",
            target_channel=(channel.lstrip("#") or channel_id or "selected-channel"),
            rationale="A bounded Slack message-history digest was supplied by the KNI Slack runtime.",
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            "Use the supplied Slack history digest as the source of truth.",
            "Keep this as read-only channel analysis; do not post or draft outbound Slack copy.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[
            ChiefOfStaffSourceRef(
                title="Supplied Slack message-history digest",
                source_type="slack_runtime_digest",
                note=f"Channel {channel_id or channel}; bounded read-only context.",
            )
        ],
        context_sources_considered=[
            "supplied_slack_message_history_digest",
            "operator_and_agent_policy",
            "keystone_slack_runtime_repo",
        ],
        repo_context_used=_repo_context_for_capability("slack-runtime-review"),
        audit_notes=[
            "Deterministic Slack history digest renderer used.",
            "No model synthesis was needed for the bounded read-only channel summary.",
            "No Slack, Gmail, Calendar, CRM, or repo write was attempted.",
        ],
    )


def _repo_context_for_capability(workflow_type: str) -> list[str]:
    common = [
        "keystone-slack:AGENTS.md",
        "keystone-slack:kni_integrations/slack_socket_mode.py",
        "keystone-slack:kni_integrations/workflow_runner.py",
        "keystone-slack:kni_integrations/business_agents_bridge.py",
    ]
    if workflow_type == "calendar-read":
        return [*common, "keystone-slack:kni_integrations/workflow_family_calendar.py"]
    if workflow_type in {"gmail-summary", "gmail-triage"}:
        return [*common, "keystone-slack:kni_integrations/workflow_family_gmail.py"]
    return common


def _action_lines(capability: Mapping[str, Any], target_channel: str) -> list[str]:
    command = str(capability.get("command_text") or "").strip()
    workflow = str(capability.get("workflow_type") or "clarification").strip()
    notes = [str(note).strip() for note in capability.get("notes") or [] if str(note).strip()]
    if workflow == "slack-runtime-review":
        return notes or [
            "Plan KNI Slack operations and recommend safe existing commands.",
            "Use read-only context only unless a live gated source is explicitly enabled.",
            "Keep all Slack posts, Gmail sends, calendar writes, and repo writes blocked.",
        ]
    lines = [
        f"Use existing KNI Slack workflow `{command}` as the starting point.",
        f"Treat #{target_channel} as the intended review channel, not as approval to post.",
        "Review the generated Slack copy before any public channel post.",
    ]
    if workflow == "calendar-read":
        lines.append("Use a read-only calendar brief; do not create or update calendar events.")
    elif workflow in {"gmail-summary", "gmail-triage"}:
        lines.append("Use Gmail summary or triage only; do not send replies from this agent.")
    elif workflow == "business-agents-route":
        lines.append(
            "Delegate company, opportunity, triage, or outreach work to Keystone Business Agents."
        )
    else:
        lines.append("Ask for the source, window, and target Slack channel before routing.")
    return lines


def _looks_like_reference_capture_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "keep this for future reference",
        "for future reference",
        "remember this",
        "save this",
        "save for later",
        "bookmark this",
        "note this",
        "store this",
        "add this to memory",
        "keep this",
    )
    if any(marker in lowered for marker in markers):
        return True
    return bool(URL_PATTERN.search(text)) and any(
        marker in lowered for marker in ("remember", "reference", "bookmark", "save")
    )


def _extract_first_url(text: str) -> str:
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,);]") if match else ""


def _reference_title_from_text(text: str, url: str) -> str:
    title = text
    if url:
        title = title.replace(url, " ")
    title = re.sub(r"^@kni\b", " ", title, flags=re.I).strip()
    title = re.sub(r"\bchief\s+of\s+staff\b", " ", title, flags=re.I).strip()
    title = re.sub(
        r"\b(please\s+)?(keep|remember|save|bookmark|note|store|add)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\b(this|for|future|reference|later|to|memory)\b", " ", title, flags=re.I)
    title = re.sub(r"[:\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if title:
        return title[:160]
    if url:
        return url
    return "Operator reference"


def _plan_reference_capture(text: str, *, database_url: str | None = None) -> ChiefOfStaffResult:
    url = _extract_first_url(text)
    title = _reference_title_from_text(text, url)
    summary = f"Operator asked Chief of Staff to keep this reference for future use: {title}."
    store = SQLiteStore(database_url or database_url_from_env())
    memory_item = operator_reference_memory_item(
        title=title,
        summary=summary,
        url=url,
        request_text=text,
        source="chief_of_staff_natural_language",
    )
    memory_id = store.save_memory_item(memory_item)
    artifact = AutomationArtifactRef(
        artifact_id=f"memory:{memory_id}",
        artifact_type="operator_reference_memory",
        title=title,
        url=url,
        provider="sqlite",
        dry_run=False,
        metadata={
            "memory_id": memory_id,
            "memory_type": "operator_reference",
            "operator": "Anup",
            "source": "chief_of_staff_natural_language",
        },
    )
    saved_location = f"Keystone memory `memory:{memory_id}`"
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=f"Saved reference for future use: {title}. Saved to {saved_location}.",
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="reference-capture",
            command_text="@KNI chief of staff keep this for future reference",
            target_channel=_extract_target_channel(text, "current-thread"),
            rationale=(
                "Anup explicitly asked Chief of Staff to retain an internal reference. "
                "The reference was stored as prompt-safe Keystone memory."
            ),
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            f"Saved to {saved_location}.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[
            ChiefOfStaffSourceRef(
                title=title,
                url=url,
                source_type="operator_reference",
                note="Operator-supplied reference captured for internal future use.",
            )
        ],
        context_sources_considered=[
            "operator_identity:Anup",
            "operator_and_agent_policy",
            "keystone_memory",
            "keystone_local_context",
        ],
        artifact_refs=[artifact],
        audit_notes=[
            "Deterministic Chief of Staff natural-language intent matched reference capture.",
            "Reference capture is scoped to founder/operator use by Anup.",
            (
                "No live web fetch was attempted; the operator-supplied URL was stored "
                "as a source reference."
            ),
        ],
    )


def plan_chief_of_staff_request(
    request_text: str,
    *,
    slack_repo_path: str | None = None,
    database_url: str | None = None,
) -> ChiefOfStaffResult:
    """Build a deterministic Chief of Staff routing recommendation without side effects."""

    del slack_repo_path  # Reserved for parity with SDK tools and future deterministic repo checks.
    text = str(request_text or "").strip()
    if _looks_like_slack_history_digest_request(text):
        return _plan_slack_history_digest_request(text)
    if _looks_like_reference_capture_request(text):
        return _plan_reference_capture(text, database_url=database_url)
    if _looks_like_automation_inventory_request(text):
        report = build_automation_inventory_report(database_url=database_url)
        write_requests = _write_requests_from_text(text)
        return ChiefOfStaffResult(
            mode="deterministic",
            intent=text,
            summary=report.summary,
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="slack-runtime-review",
                command_text="@KNI chief of staff audit automations",
                target_channel=_extract_target_channel(text, "ai-agents-workflow"),
                rationale=(
                    "Chief of Staff can audit automation state and coordinate internal "
                    "review writes through typed tools."
                ),
                requires_live_connector=any(request.live_required for request in write_requests),
                requires_human_approval_before_post=True,
            ),
            recommended_actions=[
                *report.recommended_actions,
                *[
                    f"Prepare {request.destination.value} internal write request."
                    for request in write_requests
                ],
            ],
            blocked_side_effects=BLOCKED_SIDE_EFFECTS,
            approval_required=True,
            human_review_required=True,
            send_enabled=False,
            slack_post_allowed=False,
            sources=_docs_for_topic(text),
            context_sources_considered=[
                "business_workflow_state",
                "operator_and_agent_policy",
                "keystone_slack_runtime_repo",
                "keystone_local_context",
            ],
            repo_context_used=_repo_context_for_capability("slack-runtime-review"),
            automation_report=report,
            write_requests=write_requests,
            audit_notes=[
                "Deterministic Chief of Staff automation audit used.",
                (
                    "Internal write requests are represented as typed plans; "
                    "live providers remain gated."
                ),
                "SQLite and WorkItems remain canonical.",
            ],
        )
    capability = _capability_for_topic(text)
    workflow_type = str(capability.get("workflow_type") or "clarification")
    target_channel = _extract_target_channel(text, str(capability.get("target_channel") or ""))
    command = str(capability.get("command_text") or "/kni help")
    if (
        workflow_type == "calendar-read"
        and target_channel == "calendar"
        and "meeting" in text.lower()
    ):
        command = "/kni calendar today"
    if workflow_type in {"gmail-summary", "gmail-triage"} and "onboard" in text.lower():
        command = "/kni gmail summarize onboarding"
    summary = (
        "Explain Chief of Staff scope for KNI Slack operations."
        if workflow_type == "slack-runtime-review"
        else "Recommend a read-only Slack workflow and hold all posting for human approval."
        if workflow_type != "clarification"
        else "Need clarification before selecting a Slack operations workflow."
    )
    route = ChiefOfStaffRouteRecommendation(
        workflow_type=workflow_type,  # type: ignore[arg-type]
        command_text=command,
        target_channel=target_channel,
        rationale=str(capability.get("side_effect_policy") or "").strip()
        or "Chief of Staff v1 recommends routes only.",
        requires_live_connector=bool(capability.get("requires_live_connector")),
        requires_human_approval_before_post=True,
    )
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=summary,
        recommended_route=route,
        recommended_actions=_action_lines(capability, target_channel or "selected channel"),
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=_docs_for_topic(text),
        context_sources_considered=[
            "operator_and_agent_policy",
            "keystone_slack_runtime_repo",
            "official_developer_docs",
            "keystone_local_context",
            "selected_gmail_context" if "mail" in text.lower() or "email" in text.lower() else "",
            "selected_calendar_context"
            if "calendar" in text.lower() or "meeting" in text.lower()
            else "",
            "github_and_local_repos" if "github" in text.lower() or "repo" in text.lower() else "",
        ],
        repo_context_used=_repo_context_for_capability(workflow_type),
        audit_notes=[
            "Deterministic Chief of Staff fallback used.",
            "KNI Slack repo is treated as read-only operational context.",
            "No Slack, Gmail, Calendar, CRM, or repo write was attempted.",
        ],
    )


def _looks_like_automation_inventory_request(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "automation",
            "automations",
            "audit current",
            "current runs",
            "airtable",
            "google doc",
            "google docs",
            "sync",
        )
    )


def _write_requests_from_text(text: str) -> list[ChiefOfStaffWriteRequest]:
    lowered = text.lower()
    requests: list[ChiefOfStaffWriteRequest] = []
    if "google doc" in lowered or "google docs" in lowered or "doc" in lowered:
        requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.GOOGLE_DOC,
                title="Automation Inventory Google Doc",
                summary="Create an internal Google Doc report from the automation inventory.",
                approval_required=False,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason="Live Google Docs adapter is not implemented yet.",
            )
        )
    if "airtable" in lowered:
        requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.AIRTABLE,
                title="Automation Inventory Airtable Mirror",
                summary="Sync automation findings to an Airtable-shaped review table.",
                approval_required=False,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason="Live Airtable adapter is not implemented yet.",
            )
        )
    if "slack" in lowered or "summary" in lowered:
        requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.SLACK,
                title="Automation Inventory Slack Summary",
                summary="Post a short internal Slack review summary.",
                approval_required=True,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason="Public Slack posts remain approval-gated.",
            )
        )
    return requests


def build_chief_of_staff_agent(model: str | None = None) -> Agent:
    """Build the KNI Chief of Staff SDK agent."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "chief_of_staff.md",
    )
    return build_sdk_agent(
        name="chief_of_staff",
        instructions=instructions,
        output_type=ChiefOfStaffResult,
        tools=_chief_of_staff_tools(),
        guardrails=keystone_guardrails(),
        model=model,
        model_settings=build_model_settings(
            reasoning_effort=CHIEF_OF_STAFF_REASONING_EFFORT,
            verbosity=CHIEF_OF_STAFF_VERBOSITY,
            max_tokens=CHIEF_OF_STAFF_MAX_TOKENS,
        ),
        handoff_description=(
            "Use to plan KNI Slack operations routing and bounded internal review writes "
            "across automation, calendar, Gmail, business-agent, and Slack workflows."
        ),
    )


def run_chief_of_staff_sdk(
    typed_input: str | Mapping[str, Any],
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
) -> TypedAgentRunResult[ChiefOfStaffResult]:
    """Run Chief of Staff through the shared typed SDK harness."""

    if isinstance(typed_input, Mapping):
        request_text = str(typed_input.get("request") or "")
    else:
        request_text = str(typed_input or "")
    if _looks_like_slack_history_digest_request(request_text):
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=_plan_slack_history_digest_request(request_text),
            raw_result={"deterministic": "slack_history_digest"},
            live=live,
        )

    return run_typed_sdk_agent(
        agent=build_chief_of_staff_agent(model=model),
        typed_input=typed_input,
        output_type=ChiefOfStaffResult,
        run_config=run_config,
        live=live,
        session=session,
    )


def render_chief_of_staff_result(result: ChiefOfStaffResult) -> str:
    """Render a compact operator-readable Chief of Staff result."""

    route = result.recommended_route
    lines = [
        "Agent: KNI Chief of Staff Agent",
        f"Workflow: {route.workflow_type}",
        f"Command: {route.command_text or '(clarify)'}",
        (
            f"Target channel: #{route.target_channel}"
            if route.target_channel
            else "Target channel: clarify"
        ),
        f"Send enabled: {result.send_enabled}",
        f"Slack post allowed: {result.slack_post_allowed}",
        f"Summary: {result.summary}",
    ]
    if result.recommended_actions:
        lines.append("Actions:")
        lines.extend(f"- {action}" for action in result.recommended_actions)
    return "\n".join(lines)
