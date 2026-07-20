"""ANU-120 target-action scorecard for manual planner coverage."""

from __future__ import annotations

from dataclasses import dataclass

from keystone_agents.schemas.manual_request_plan import (
    ManualExpectedArtifactType,
    ManualRequestIntent,
    ManualTargetAgent,
    ManualTargetType,
    ManualTaskObjective,
)


@dataclass(frozen=True)
class TargetActionCase:
    """Representative natural ask mapped to owner, target system, and safe proof."""

    case_id: str
    request: str
    category: str
    owner_agent: ManualTargetAgent
    source_system: str
    target_system: str
    target_action: str
    required_context: str
    allowed_tool_tier: str
    approval_gate: str
    expected_proof: str
    fallback_blocker: str
    artifact_backed: bool
    expected_intent: ManualRequestIntent
    expected_target_type: ManualTargetType
    expected_task_objective: ManualTaskObjective
    expected_artifact_type: ManualExpectedArtifactType
    expected_side_effect_policy: str = "draft_or_read_only"
    expected_warning_substrings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextualTargetActionCase:
    """Thread-local ask whose source identity comes from prior bounded context."""

    case_id: str
    prior_context: str
    follow_up: str
    owner_agent: ManualTargetAgent
    expected_intent: ManualRequestIntent
    target_action: str
    tool_contract_status: str
    required_tool_change: str
    expected_safety_boundary: str


TARGET_ACTION_SCORECARD: tuple[TargetActionCase, ...] = (
    TargetActionCase(
        case_id="research_company",
        request="Research Lindus Health and recommend the next step.",
        category="research",
        owner_agent="business_research_analyst",
        source_system="live or fixture search context",
        target_system="business research",
        target_action="research company",
        required_context="company name and source constraints",
        allowed_tool_tier="read-only retrieval and synthesis",
        approval_gate="none",
        expected_proof="source-backed research brief route",
        fallback_blocker="missing company or source basis",
        artifact_backed=False,
        expected_intent="company_research",
        expected_target_type="company",
        expected_task_objective="source_research",
        expected_artifact_type="research_brief",
    ),
    TargetActionCase(
        case_id="compare_companies",
        request="Compare Lindus Health vs Holmusk for Keystone advisory fit.",
        category="compare",
        owner_agent="business_research_analyst",
        source_system="live or fixture search context",
        target_system="business research",
        target_action="compare companies",
        required_context="two named companies and comparison basis",
        allowed_tool_tier="read-only retrieval and comparison synthesis",
        approval_gate="none",
        expected_proof="company comparison route with both targets preserved",
        fallback_blocker="missing comparison target",
        artifact_backed=False,
        expected_intent="company_research",
        expected_target_type="company",
        expected_task_objective="source_research",
        expected_artifact_type="research_brief",
    ),
    TargetActionCase(
        case_id="summarize_source_notes",
        request="Summarize source-provided vendor notes about NeuroFlow in a table.",
        category="summarize",
        owner_agent="business_research_analyst",
        source_system="operator-provided notes",
        target_system="business research",
        target_action="summarize provided evidence",
        required_context="provided evidence and requested table format",
        allowed_tool_tier="bounded synthesis over provided context",
        approval_gate="none",
        expected_proof="source summary route, not a write route",
        fallback_blocker="missing source notes",
        artifact_backed=False,
        expected_intent="company_research",
        expected_target_type="company",
        expected_task_objective="source_research",
        expected_artifact_type="source_summary",
    ),
    TargetActionCase(
        case_id="find_opportunities",
        request="Find 5 behavioral health AI grant opportunities.",
        category="research",
        owner_agent="opportunity_scout",
        source_system="live or fixture opportunity sources",
        target_system="opportunity scout",
        target_action="discover opportunities",
        required_context="topic, count, and eligibility constraints",
        allowed_tool_tier="read-only retrieval and scoring",
        approval_gate="none",
        expected_proof="opportunity record route with desired count",
        fallback_blocker="missing opportunity topic",
        artifact_backed=False,
        expected_intent="opportunity_search",
        expected_target_type="topic",
        expected_task_objective="opportunity_discovery",
        expected_artifact_type="opportunity_record",
    ),
    TargetActionCase(
        case_id="draft_outreach",
        request="Draft a short email to Lindus Health. Do not send.",
        category="draft",
        owner_agent="outreach_composer",
        source_system="approved context",
        target_system="outreach composer",
        target_action="draft outreach",
        required_context="recipient target and approved claims",
        allowed_tool_tier="draft-only synthesis",
        approval_gate="approved context required before use",
        expected_proof="outreach draft route with no send permission",
        fallback_blocker="missing approved claims or recipient",
        artifact_backed=False,
        expected_intent="outreach_draft",
        expected_target_type="company",
        expected_task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
    ),
    TargetActionCase(
        case_id="gmail_triage",
        request=(
            "Summarize unread Gmail from the last 7 days and draft replies only "
            "where needed."
        ),
        category="summarize",
        owner_agent="gmail_triage",
        source_system="Gmail",
        target_system="gmail triage",
        target_action="triage inbox",
        required_context="Gmail query scope and draft-only policy",
        allowed_tool_tier="Gmail read plus draft planning only",
        approval_gate="no send without scoped approval",
        expected_proof="Gmail triage report route",
        fallback_blocker="missing Gmail scope",
        artifact_backed=False,
        expected_intent="gmail_triage",
        expected_target_type="gmail_thread",
        expected_task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
    ),
    TargetActionCase(
        case_id="gmail_label",
        request="Label selected Gmail thread as follow-up candidate.",
        category="label",
        owner_agent="gmail_triage",
        source_system="Gmail",
        target_system="gmail triage",
        target_action="label thread",
        required_context="selected thread identity and label name",
        allowed_tool_tier="Gmail schema/read context before any label write",
        approval_gate="scoped Gmail label approval required",
        expected_proof="Gmail route, not generic research",
        fallback_blocker="missing selected thread or label identity",
        artifact_backed=False,
        expected_intent="gmail_triage",
        expected_target_type="gmail_thread",
        expected_task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
    ),
    TargetActionCase(
        case_id="workspace_read",
        request="Read the Google Drive folder for KNI Ops and summarize the relevant docs.",
        category="summarize",
        owner_agent="google_workspace_context_agent",
        source_system="Google Drive",
        target_system="Google Workspace context",
        target_action="read folder context",
        required_context="folder identifier or operator-selected Drive context",
        allowed_tool_tier="Workspace schema/read tools",
        approval_gate="none for read-only context",
        expected_proof="Workspace context lookup route",
        fallback_blocker="missing folder identity or access",
        artifact_backed=False,
        expected_intent="context_lookup",
        expected_target_type="business_system_context",
        expected_task_objective="context_lookup",
        expected_artifact_type="context_summary",
    ),
    TargetActionCase(
        case_id="workspace_save_plan",
        request=(
            "Prepare a save plan for the NeuroFlow research brief in Google Drive. "
            "Do not create the file."
        ),
        category="save-plan",
        owner_agent="google_workspace_context_agent",
        source_system="research artifact",
        target_system="Google Workspace context",
        target_action="plan internal artifact save",
        required_context="artifact, Drive destination, and no-create constraint",
        allowed_tool_tier="Workspace context and write-plan only",
        approval_gate="scoped Workspace create approval required for execution",
        expected_proof="Workspace context route remains read-only",
        fallback_blocker="missing artifact or destination",
        artifact_backed=True,
        expected_intent="context_lookup",
        expected_target_type="business_system_context",
        expected_task_objective="context_lookup",
        expected_artifact_type="context_summary",
    ),
    TargetActionCase(
        case_id="workspace_create_blocked",
        request="Create a Google Doc summary for the KNI Ops folder.",
        category="save-plan",
        owner_agent="google_workspace_context_agent",
        source_system="Google Drive",
        target_system="Google Workspace context",
        target_action="create document",
        required_context="folder identity, document content, and approval reference",
        allowed_tool_tier="schema-first approved internal write path",
        approval_gate="scoped Workspace create approval required",
        expected_proof="typed create plan with exact destination and read-back requirement",
        fallback_blocker="missing folder identity, content, provider flag, or approval reference",
        artifact_backed=False,
        expected_intent="business_system_write",
        expected_target_type="business_system_context",
        expected_task_objective="business_system_write",
        expected_artifact_type="business_system_write_plan",
        expected_side_effect_policy="internal_write_approval_required",
    ),
    TargetActionCase(
        case_id="airtable_schema_read",
        request="Inspect Airtable Business Expenses schema and summarize required fields.",
        category="summarize",
        owner_agent="airtable_context_agent",
        source_system="Airtable",
        target_system="Airtable context",
        target_action="inspect schema",
        required_context="base/table name or selected Airtable context",
        allowed_tool_tier="Airtable schema/read tools",
        approval_gate="none for schema read",
        expected_proof="Airtable context lookup route",
        fallback_blocker="missing table identity or access",
        artifact_backed=False,
        expected_intent="context_lookup",
        expected_target_type="business_system_context",
        expected_task_objective="context_lookup",
        expected_artifact_type="context_summary",
    ),
    TargetActionCase(
        case_id="airtable_update_blocked",
        request="Update the Airtable CRM row for Lindus Health with this note.",
        category="crm",
        owner_agent="airtable_context_agent",
        source_system="Airtable",
        target_system="Airtable context",
        target_action="update record",
        required_context="base, table, row identity, field map, source basis, approval",
        allowed_tool_tier="schema-first approved internal write path",
        approval_gate="scoped Airtable update approval required",
        expected_proof="typed update plan with exact record and field mapping",
        fallback_blocker="missing row identity, field map, or approval",
        artifact_backed=False,
        expected_intent="business_system_write",
        expected_target_type="business_system_context",
        expected_task_objective="business_system_write",
        expected_artifact_type="business_system_write_plan",
        expected_side_effect_policy="internal_write_approval_required",
    ),
    TargetActionCase(
        case_id="airtable_receipt_plan",
        request=(
            "Add a business expense to Airtable business expenses from "
            "/tmp/example-business-cards-receipt.pdf"
        ),
        category="artifact-backed",
        owner_agent="chief_of_staff",
        source_system="local receipt artifact",
        target_system="Airtable Business Expenses",
        target_action="create expense write plan",
        required_context="receipt evidence, table schema, mapped fields, approval",
        allowed_tool_tier="bounded business-system write plan",
        approval_gate="scoped Airtable create approval required",
        expected_proof="Chief of Staff business-system write plan",
        fallback_blocker="missing receipt evidence, schema, or approval",
        artifact_backed=True,
        expected_intent="business_system_write",
        expected_target_type="business_system_context",
        expected_task_objective="business_system_write",
        expected_artifact_type="business_system_write_plan",
        expected_side_effect_policy="internal_write_approval_required",
    ),
    TargetActionCase(
        case_id="zotero_context_summary",
        request="Use Zotero context to summarize the KNI AI collection.",
        category="summarize",
        owner_agent="zotero_context_agent",
        source_system="Zotero",
        target_system="Zotero context",
        target_action="summarize collection",
        required_context="collection name or selected Zotero context",
        allowed_tool_tier="Zotero read/context tools",
        approval_gate="none for read-only context",
        expected_proof="Zotero context lookup route",
        fallback_blocker="missing collection identity or access",
        artifact_backed=False,
        expected_intent="context_lookup",
        expected_target_type="business_system_context",
        expected_task_objective="context_lookup",
        expected_artifact_type="context_summary",
    ),
    TargetActionCase(
        case_id="rss_context_summary",
        request="Use RSS context to summarize the #announcements feed. Do not post.",
        category="summarize",
        owner_agent="rss_context_agent",
        source_system="RSS announcements",
        target_system="RSS context",
        target_action="summarize feed",
        required_context="feed identity and no-post constraint",
        allowed_tool_tier="read-only feed context tools",
        approval_gate="no Slack post without scoped approval",
        expected_proof="RSS context lookup route",
        fallback_blocker="missing feed identity",
        artifact_backed=False,
        expected_intent="context_lookup",
        expected_target_type="article_collection",
        expected_task_objective="context_lookup",
        expected_artifact_type="context_summary",
    ),
    TargetActionCase(
        case_id="preprints_context_summary",
        request="Use preprints context to summarize recent knowledge hub preprints.",
        category="summarize",
        owner_agent="preprints_context_agent",
        source_system="preprints history",
        target_system="Preprints context",
        target_action="summarize preprints",
        required_context="preprint source scope and recency constraint",
        allowed_tool_tier="read-only preprints context tools",
        approval_gate="none for read-only context",
        expected_proof="Preprints context lookup route",
        fallback_blocker="missing preprint source scope",
        artifact_backed=False,
        expected_intent="context_lookup",
        expected_target_type="article_collection",
        expected_task_objective="context_lookup",
        expected_artifact_type="context_summary",
    ),
    TargetActionCase(
        case_id="reference_capture",
        request="Save this URL for future reference: https://example.com/research",
        category="save-plan",
        owner_agent="chief_of_staff",
        source_system="operator URL",
        target_system="operator reference",
        target_action="capture reference",
        required_context="URL and reason to retain",
        allowed_tool_tier="reference note draft only",
        approval_gate="repository/memory write approval required for persistence",
        expected_proof="reference capture route",
        fallback_blocker="missing URL or durable destination",
        artifact_backed=True,
        expected_intent="reference_capture",
        expected_target_type="operator_reference",
        expected_task_objective="reference_capture",
        expected_artifact_type="reference_note",
    ),
    TargetActionCase(
        case_id="slack_post_blocked",
        request="Post the summary to Slack.",
        category="post",
        owner_agent="chief_of_staff",
        source_system="operator summary",
        target_system="Slack",
        target_action="post message",
        required_context="channel/thread identity, summary content, and approval",
        allowed_tool_tier="blocked side-effect plan",
        approval_gate="scoped Slack post approval required",
        expected_proof="post request blocked before Slack write",
        fallback_blocker="missing channel/thread, content, or approval",
        artifact_backed=False,
        expected_intent="blocked_send",
        expected_target_type="company",
        expected_task_objective="blocked_side_effect",
        expected_artifact_type="none",
        expected_warning_substrings=("external send/write requests remain draft/read-only",),
    ),
    TargetActionCase(
        case_id="schedule_blocked",
        request="Schedule a follow-up meeting with Lindus Health.",
        category="schedule",
        owner_agent="chief_of_staff",
        source_system="operator request",
        target_system="Calendar",
        target_action="schedule event",
        required_context="event identity, date/time, attendees when needed, and approval",
        allowed_tool_tier="Orchestrator preflight then typed Calendar action gates",
        approval_gate="scoped Calendar event approval required",
        expected_proof="Calendar intent is established before missing fields can block",
        fallback_blocker="missing event date/time or ambiguous write scope",
        artifact_backed=False,
        expected_intent="business_system_write",
        expected_target_type="business_system_context",
        expected_task_objective="business_system_write",
        expected_artifact_type="business_system_write_plan",
        expected_side_effect_policy="internal_write_approval_required",
    ),
    TargetActionCase(
        case_id="browser_diagnostics",
        request="Run browser diagnostics on http://127.0.0.1:8765/status.",
        category="read",
        owner_agent="orchestrator",
        source_system="rendered page",
        target_system="browser diagnostics",
        target_action="inspect page health",
        required_context="URL and read-only diagnostic scope",
        allowed_tool_tier="read-only backend browser diagnostics",
        approval_gate="no mutation or authenticated browser action",
        expected_proof="browser diagnostics report route",
        fallback_blocker="missing URL",
        artifact_backed=False,
        expected_intent="browser_diagnostics",
        expected_target_type="url",
        expected_task_objective="browser_diagnostics",
        expected_artifact_type="browser_diagnostics_report",
    ),
    TargetActionCase(
        case_id="local_artifact_summary",
        request="Summarize the source packet in /tmp/example-research-packet.pdf for NeuroFlow.",
        category="artifact-backed",
        owner_agent="business_research_analyst",
        source_system="local source packet",
        target_system="business research",
        target_action="summarize artifact evidence",
        required_context="artifact path, target company, and source-use constraint",
        allowed_tool_tier="local artifact read plus bounded synthesis",
        approval_gate="none for local read-only artifact summary",
        expected_proof="source summary route over artifact evidence",
        fallback_blocker="missing artifact path or source basis",
        artifact_backed=True,
        expected_intent="company_research",
        expected_target_type="company",
        expected_task_objective="source_research",
        expected_artifact_type="source_summary",
    ),
    TargetActionCase(
        case_id="orchestrated_research_then_draft",
        request=(
            "Which agents should handle company research then draft-only outreach "
            "for Lindus Health? Do not send."
        ),
        category="workflow",
        owner_agent="business_research_analyst",
        source_system="operator workflow request",
        target_system="Orchestrator to specialist",
        target_action="research then draft-only planning",
        required_context="raw request, company target, approval/no-send constraint",
        allowed_tool_tier="Orchestrator preflight plus specialist read/draft gates",
        approval_gate="no send without scoped outreach approval",
        expected_proof="research owner selected as first safe specialist",
        fallback_blocker="missing company target or approved claims",
        artifact_backed=False,
        expected_intent="company_research",
        expected_target_type="company",
        expected_task_objective="source_research",
        expected_artifact_type="research_brief",
    ),
)


CONTEXTUAL_TARGET_ACTION_SCORECARD: tuple[ContextualTargetActionCase, ...] = (
    ContextualTargetActionCase(
        case_id="gmail_draft_revision",
        prior_context="Gmail draft created and provider verified for the selected thread.",
        follow_up="Make this shorter and add the link.",
        owner_agent="gmail_triage",
        expected_intent="gmail_triage",
        target_action="update the same provider draft",
        tool_contract_status="supported_with_exact_draft_identity",
        required_tool_change="none after contextual admission reaches Gmail draft execution",
        expected_safety_boundary="draft-only, exact draft, approval, and provider read-back",
    ),
    ContextualTargetActionCase(
        case_id="airtable_record_update",
        prior_context="Airtable record created and provider verified in the Projects table.",
        follow_up="Update this record with status Reviewed.",
        owner_agent="airtable_context_agent",
        expected_intent="business_system_write",
        target_action="update exact record fields",
        tool_contract_status="supported_with_unique_record_identity",
        required_tool_change="none after contextual admission reaches the typed record writer",
        expected_safety_boundary="schema validation, exact record, approval, and read-back",
    ),
    ContextualTargetActionCase(
        case_id="google_doc_append",
        prior_context="Google Doc created and provider verified in the scoped KNIOps folder.",
        follow_up="Add this paragraph to the document notes.",
        owner_agent="google_workspace_context_agent",
        expected_intent="business_system_write",
        target_action="append content without replacing the body",
        tool_contract_status="supported_with_content_mode_append",
        required_tool_change="implemented explicit append mode and provider content read-back",
        expected_safety_boundary="exact Doc, scoped folder, approval, and read-back",
    ),
    ContextualTargetActionCase(
        case_id="google_sheet_row_update",
        prior_context="Google Sheet row appended and verified with stable key KBA_TEST_ROW.",
        follow_up="Change this row status to Reviewed.",
        owner_agent="google_workspace_context_agent",
        expected_intent="business_system_write",
        target_action="update one stable-key row",
        tool_contract_status="supported_with_stable_row_key",
        required_tool_change="none after contextual admission reaches the typed row updater",
        expected_safety_boundary="exact Sheet/tab/key, approval, and read-back",
    ),
    ContextualTargetActionCase(
        case_id="zotero_article_note",
        prior_context="Zotero article resolved to one exact library item.",
        follow_up="Add this note to the paper.",
        owner_agent="zotero_context_agent",
        expected_intent="business_system_write",
        target_action="create or append a child note on an article",
        tool_contract_status="unsupported_for_ordinary_items",
        required_tool_change=(
            "reviewed ordinary-note tool with parent item identity, append/replace mode, "
            "version precondition, approval, and read-back"
        ),
        expected_safety_boundary=(
            "ordinary Zotero mutation remains blocked until that contract exists"
        ),
    ),
    ContextualTargetActionCase(
        case_id="zotero_pdf_attachment",
        prior_context="Zotero article resolved to one exact library item.",
        follow_up="Attach this PDF to the article.",
        owner_agent="zotero_context_agent",
        expected_intent="business_system_write",
        target_action="attach a PDF child item to an article",
        tool_contract_status="unsupported",
        required_tool_change=(
            "reviewed attachment tool with local-file validation, MIME/size/checksum, "
            "parent item identity, upload protocol, approval, and read-back"
        ),
        expected_safety_boundary="no ordinary attachment upload through current test-only tools",
    ),
    ContextualTargetActionCase(
        case_id="slack_message_edit",
        prior_context="The selected Slack message is one exact marked test post.",
        follow_up="Edit that message to include the link.",
        owner_agent="chief_of_staff",
        expected_intent="slack_operations",
        target_action="update one exact bot-authored message",
        tool_contract_status="supported_for_marked_test_messages_only",
        required_tool_change=(
            "ordinary bot-authored edit contract if production edits are desired; "
            "keep human posts excluded"
        ),
        expected_safety_boundary="exact channel/timestamp/author marker and provider verification",
    ),
    ContextualTargetActionCase(
        case_id="prior_result_link_summary",
        prior_context="Research result 2 is a source-backed article with a retained URL.",
        follow_up="Summarize link 2 from the prior results.",
        owner_agent="business_research_analyst",
        expected_intent="research_brief",
        target_action="read and summarize the referenced source",
        tool_contract_status="supported_read_only",
        required_tool_change="none after contextual admission preserves the source reference",
        expected_safety_boundary="read-only source extraction with visible attribution",
    ),
)


def target_action_scorecard() -> tuple[TargetActionCase, ...]:
    """Return the immutable ANU-120 representative target-action scorecard."""

    return TARGET_ACTION_SCORECARD


def contextual_target_action_scorecard() -> tuple[ContextualTargetActionCase, ...]:
    """Return ANU-120 thread-local target-action and tool-gap cases."""

    return CONTEXTUAL_TARGET_ACTION_SCORECARD
