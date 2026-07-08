"""Contracts for context-agent read/write/modify boundaries and handoffs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ContextMutationLevel(StrEnum):
    """Mutation boundary for a context agent contract."""

    READ_ONLY = "read_only"
    WRITE_PLAN_ONLY = "write_plan_only"
    APPROVED_INTERNAL_WRITE = "approved_internal_write"
    GUARDED_EXTERNAL_IMPORTER = "guarded_external_importer"


class ContextGraphRole(StrEnum):
    """Current or future orchestration role for a context agent."""

    TOOL_CONTEXT_NODE = "tool_context_node"
    GRAPH_STAGE_CANDIDATE = "graph_stage_candidate"
    SUBGRAPH_LATER = "subgraph_later"


class ContextPackHandoffContract(BaseModel):
    """Evidence that a context agent may pass into downstream specialists."""

    context_packs: list[str] = Field(default_factory=list)
    evidence_to_business_research: list[str] = Field(default_factory=list)
    evidence_to_opportunity_scout: list[str] = Field(default_factory=list)
    evidence_to_outreach_composer: list[str] = Field(default_factory=list)
    required_source_fields: list[str] = Field(default_factory=list)
    uncertainty_fields: list[str] = Field(default_factory=list)


class ContextAgentContract(BaseModel):
    """Durable contract for one context agent family."""

    issue_id: str
    agent_name: str
    current_role: ContextGraphRole = ContextGraphRole.TOOL_CONTEXT_NODE
    future_graph_role: ContextGraphRole = ContextGraphRole.GRAPH_STAGE_CANDIDATE
    mutation_level: ContextMutationLevel
    read_boundary: list[str] = Field(default_factory=list)
    write_boundary: list[str] = Field(default_factory=list)
    modify_boundary: list[str] = Field(default_factory=list)
    blocked_actions: list[str] = Field(default_factory=list)
    approval_requirements: list[str] = Field(default_factory=list)
    handoff_contract: ContextPackHandoffContract
    graph_candidate_edges: list[str] = Field(default_factory=list)
    subgraph_promotion_requirements: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    milestone_notes: list[str] = Field(default_factory=list)


def context_agent_contracts() -> tuple[ContextAgentContract, ...]:
    """Return context-agent contracts for ANU-198 through ANU-202."""

    return _CONTEXT_AGENT_CONTRACTS


def get_context_agent_contract(agent_name: str) -> ContextAgentContract | None:
    """Return one context-agent contract by agent name."""

    normalized = str(agent_name or "").strip()
    for contract in _CONTEXT_AGENT_CONTRACTS:
        if contract.agent_name == normalized:
            return contract
    return None


_CONTEXT_AGENT_CONTRACTS: tuple[ContextAgentContract, ...] = (
    ContextAgentContract(
        issue_id="ANU-198",
        agent_name="airtable_context_agent",
        mutation_level=ContextMutationLevel.APPROVED_INTERNAL_WRITE,
        read_boundary=[
            "configured base aliases and base ids",
            "base schemas, tables, fields, linked-record fields, views, and capped record sets",
            "candidate records for exact identity resolution and dedupe review",
        ],
        write_boundary=[
            "create scoped records only through typed Airtable tools",
            "attach files only through a reviewed attachment contract",
            "write only with exact table, field mapping, value basis, approval reference, "
            "and live flag",
        ],
        modify_boundary=[
            "update exact existing records only with record id, old value, new value, "
            "field mapping, and scoped approval",
            "return a write plan when nested under Chief of Staff or another specialist",
        ],
        blocked_actions=[
            "delete records",
            "schema changes",
            "silent bulk overwrites",
            "ambiguous record writes",
            "hidden nested writes",
        ],
        approval_requirements=[
            "approval reference tied to the exact table and operation",
            "record identity and field mapping must be explicit before modify",
            "nested calls may return plans but must not execute writes",
        ],
        handoff_contract=ContextPackHandoffContract(
            context_packs=["ResearchContextPack", "OpportunityContextPack"],
            evidence_to_business_research=[
                "base/table context relevant to the target company, contact, or project",
                "record summaries with field names, candidate ids, missing data, and source notes",
                "record-identity blockers and field-mapping uncertainty",
            ],
            evidence_to_opportunity_scout=[
                "existing opportunity/contact record ids for dedupe",
                "pipeline/status fields and missing opportunity metadata",
                "write-plan recommendations for tracking updates after human review",
            ],
            required_source_fields=[
                "base_alias",
                "base_id",
                "table_name",
                "field_names",
                "record_id",
                "view_or_filter",
            ],
            uncertainty_fields=["ambiguous_record_identity", "missing_field_mapping"],
        ),
        graph_candidate_edges=[
            "Opportunity Scout -> Airtable Context -> Chief review",
            "Business Research -> Airtable Context -> Workspace Context",
        ],
        subgraph_promotion_requirements=[
            "fixture-backed create and update checkpoints",
            "approval queue integration for exact record writes",
            "retry-safe audit events for every proposed mutation",
        ],
        validation_requirements=[
            "base-specific read fixture",
            "approved create fixture",
            "approved exact-record update fixture",
            "ambiguous record blocker",
            "unsupported delete/schema-change blocker",
        ],
        milestone_notes=[
            "remains a context node now",
            "graph candidate only for documentation refresh or approval-gated write-plan loops",
        ],
    ),
    ContextAgentContract(
        issue_id="ANU-199",
        agent_name="google_workspace_context_agent",
        mutation_level=ContextMutationLevel.APPROVED_INTERNAL_WRITE,
        read_boundary=[
            "scoped Drive folders and file metadata",
            "Google Docs content and bounded Google Sheets tabs/ranges",
            "internal opportunity, contact, company, source-note, and follow-up artifacts",
        ],
        write_boundary=[
            "create folders, docs, sheets, tabs, rows, and internal artifacts only with exact "
            "destination and approval scope",
            "preserve source refs and audit refs for every proposed artifact mutation",
        ],
        modify_boundary=[
            "rename folders, update docs, update rows/tabs, remove tabs/rows, or trash files only "
            "with exact target identity and approval",
            "broad delete/share remains outside this contract",
        ],
        blocked_actions=[
            "external sharing",
            "broad deletion",
            "sensitive-data publication",
            "unscoped Drive/Docs/Sheets mutation",
            "hidden nested writes",
        ],
        approval_requirements=[
            "exact folder/file/doc/sheet/tab identity",
            "approval reference for every create/update/remove/trash request",
            "sensitivity flags checked before any internal artifact creation",
        ],
        handoff_contract=ContextPackHandoffContract(
            context_packs=["ResearchContextPack", "OpportunityContextPack"],
            evidence_to_business_research=[
                "existing document/table facts and stale-fact notes",
                "Workspace artifact refs that define the source basis",
                "missing artifact gaps and suggested source-backed updates",
            ],
            evidence_to_opportunity_scout=[
                "opportunity/contact/company log rows and missing tracking fields",
                "source-backed follow-up plans and artifact destinations",
                "duplicate or stale opportunity documentation signals",
            ],
            required_source_fields=[
                "drive_folder_id",
                "file_id",
                "doc_id",
                "sheet_id",
                "tab_name",
                "row_key_or_range",
            ],
            uncertainty_fields=["missing_destination", "ambiguous_file_identity"],
        ),
        graph_candidate_edges=[
            "Business Research -> Google Workspace Context -> Chief review",
            "Opportunity Scout -> Google Workspace Context -> Airtable Context",
        ],
        subgraph_promotion_requirements=[
            "fixture-backed artifact creation/update plans",
            "approval checkpoints for exact Docs/Sheets mutations",
            "artifact sensitivity checks in WorkItem context",
        ],
        validation_requirements=[
            "document synthesis fixture",
            "opportunity/contact artifact creation plan fixture",
            "sheet-row update fixture",
            "folder/file identity blocker",
            "blocked broad delete/share request",
        ],
        milestone_notes=[
            "remains a context node now",
            "graph candidate for internal documentation refresh and approved artifact loops",
        ],
    ),
    ContextAgentContract(
        issue_id="ANU-200",
        agent_name="zotero_context_agent",
        mutation_level=ContextMutationLevel.GUARDED_EXTERNAL_IMPORTER,
        read_boundary=[
            "local/API Zotero library metadata, collections, items, abstracts, notes, "
            "and importer state",
            "approved local evidence connected to article or collection context",
            "full text only when an approved local/full-text source is available",
        ],
        write_boundary=[
            "create internal article summaries, evidence packets, Workspace notes, "
            "and importer plans",
            "Zotero library mutation stays behind the guarded importer path with dry-run proof",
        ],
        modify_boundary=[
            "revise article summaries, collection guidance, relevance tags, and artifact plans",
            "block direct collection/item mutation without exact identity and importer approval",
        ],
        blocked_actions=[
            "direct Zotero library mutation outside importer",
            "ambiguous collection or item update",
            "unsupported article claims without citation",
            "private evidence leakage",
        ],
        approval_requirements=[
            "exact collection/item/article identity for importer plans",
            "dry-run importer proof before live Zotero write",
            "approval reference before Workspace artifact writes",
        ],
        handoff_contract=ContextPackHandoffContract(
            context_packs=["ResearchContextPack", "OpportunityContextPack", "OutreachContextPack"],
            evidence_to_business_research=[
                "article metadata, abstracts/full-text basis, collection summaries, "
                "and citation ids",
                "source-backed evidence packets and uncertainty about source maturity",
                "research implications tied to Keystone questions",
            ],
            evidence_to_opportunity_scout=[
                "article-derived opportunity signals and target domains",
                "collection-level evidence gaps that need current public verification",
                "source ids that support opportunity rationale",
            ],
            evidence_to_outreach_composer=[
                "approved article facts and citations for draft rationale only",
                "unsupported or preliminary claims that must be excluded from outreach",
            ],
            required_source_fields=[
                "collection_key",
                "zotero_item_key",
                "source_id",
                "doi_or_url",
                "evidence_basis",
            ],
            uncertainty_fields=[
                "metadata_only",
                "full_text_missing",
                "preliminary_or_uncertain_claim",
            ],
        ),
        graph_candidate_edges=[
            "RSS/Preprints Context -> Zotero Context -> Business Research",
            "Zotero Context -> Opportunity Scout -> Business Research",
        ],
        subgraph_promotion_requirements=[
            "read-only article/collection fixture coverage",
            "guarded importer dry-run and approval checkpoint",
            "source id preservation through downstream research/opportunity artifacts",
        ],
        validation_requirements=[
            "key article read fixture",
            "collection comparison fixture",
            "importer plan fixture",
            "ambiguous collection/item mutation blocker",
            "provenance preservation assertion",
        ],
        milestone_notes=[
            "remains a context node now",
            "subgraph candidate only when digest/importer planning needs resumable checkpoints",
        ],
    ),
    ContextAgentContract(
        issue_id="ANU-201",
        agent_name="rss_context_agent",
        mutation_level=ContextMutationLevel.READ_ONLY,
        read_boundary=[
            "canonical RSS/#announcements history",
            "feed item metadata, dates, article clusters, topics, and prior feed-derived context",
            "historical trend context that still needs current-source verification for live claims",
        ],
        write_boundary=[
            "internal insight summaries, trend notes, source bundles, and downstream "
            "context packets only",
            "no feed mutation or Slack posting from this context agent",
        ],
        modify_boundary=[
            "revise retained/rejected insight clusters and follow-up recommendations "
            "as internal artifacts",
            "feed history remains read-only until a separate feed-management contract exists",
        ],
        blocked_actions=[
            "feed mutation",
            "Slack scraping or posting",
            "unsupported current factual claims",
            "article recaps without synthesis when deeper insight is requested",
        ],
        approval_requirements=[
            "approval required before publishing or posting any digest",
            "current public-source verification required before using feed history as "
            "a current claim",
        ],
        handoff_contract=ContextPackHandoffContract(
            context_packs=["ResearchContextPack", "OpportunityContextPack"],
            evidence_to_business_research=[
                "theme clusters with feed item ids, dates, URLs, and source notes",
                "historical trend summaries and gaps needing current verification",
                "candidate queries for current public-source follow-up",
            ],
            evidence_to_opportunity_scout=[
                "opportunity hooks from recurring feed themes",
                "company/research/funding signals and stale-current caveats",
                "monitoring queries and rejected/low-confidence items",
            ],
            required_source_fields=[
                "feed_item_id",
                "title",
                "url",
                "published_at",
                "source",
                "selection_reason",
            ],
            uncertainty_fields=["stale_context", "snippet_only", "needs_current_verification"],
        ),
        graph_candidate_edges=[
            "RSS Context -> Business Research",
            "RSS Context -> Opportunity Scout",
            "RSS Context -> Preprints Context -> Zotero Context",
        ],
        subgraph_promotion_requirements=[
            "persisted insight-cluster state",
            "validation that digest synthesis is deeper than article listing",
            "source freshness repair edge before external factual claims",
        ],
        validation_requirements=[
            "detailed feed insight fixture",
            "theme clustering fixture",
            "opportunity signal extraction fixture",
            "stale/current-source caveat assertion",
            "blocked feed mutation assertion",
        ],
        milestone_notes=[
            "strict read-only context node now",
            "graph candidate for recurring intelligence digest staging",
        ],
    ),
    ContextAgentContract(
        issue_id="ANU-202",
        agent_name="preprints_context_agent",
        mutation_level=ContextMutationLevel.READ_ONLY,
        read_boundary=[
            "canonical preprint/#knowledge-hub history",
            "article metadata, abstracts, source links, dates, topics, and prior article context",
            "full text only when source text is already available through approved context",
        ],
        write_boundary=[
            "internal article summaries, evidence packets, opportunity signals, source bundles, "
            "and downstream context packets only",
            "no source record mutation from this context agent",
        ],
        modify_boundary=[
            "revise article relevance, summary claims, retained/rejected source notes, "
            "and follow-up recommendations as internal artifacts",
            "preprint/source records remain read-only until a separate library-management "
            "contract exists",
        ],
        blocked_actions=[
            "source mutation",
            "overstating preliminary findings",
            "unsupported article claims",
            "outreach-ready claims without approval and current verification",
        ],
        approval_requirements=[
            "preliminary-claim caveats required in every downstream packet",
            "current or peer-reviewed source verification required before external-use claims",
        ],
        handoff_contract=ContextPackHandoffContract(
            context_packs=["ResearchContextPack", "OpportunityContextPack", "OutreachContextPack"],
            evidence_to_business_research=[
                "article metadata, abstract/full-text basis, source URL, date, and uncertainty",
                "paper-set comparisons and preliminary-claim caveats",
                "research implications and current-verification gaps",
            ],
            evidence_to_opportunity_scout=[
                "opportunity implications from article findings",
                "clinical translation or market signals with preliminary-evidence caveats",
                "monitoring queries for follow-up verification",
            ],
            evidence_to_outreach_composer=[
                "approved, caveated article facts only when explicitly allowed",
                "claims to avoid because evidence is preliminary or unsupported",
            ],
            required_source_fields=[
                "feed_item_id",
                "article_title",
                "url_or_doi",
                "published_at",
                "evidence_status",
                "source_basis",
            ],
            uncertainty_fields=["preprint_status", "abstract_only", "needs_replication"],
        ),
        graph_candidate_edges=[
            "Preprints Context -> Business Research",
            "Preprints Context -> Opportunity Scout",
            "Preprints Context -> Zotero Context -> Business Research",
        ],
        subgraph_promotion_requirements=[
            "paper-set synthesis fixtures",
            "preliminary-claim caveat checks",
            "source freshness/verification repair edge before external use",
        ],
        validation_requirements=[
            "key article summary fixture",
            "paper-set synthesis fixture",
            "opportunity implication extraction fixture",
            "preliminary-claim caveat assertion",
            "blocked source mutation assertion",
        ],
        milestone_notes=[
            "strict read-only context node now",
            "graph candidate for article-set synthesis and recurring intelligence digest staging",
        ],
    ),
)


__all__ = [
    "ContextAgentContract",
    "ContextGraphRole",
    "ContextMutationLevel",
    "ContextPackHandoffContract",
    "context_agent_contracts",
    "get_context_agent_contract",
]
