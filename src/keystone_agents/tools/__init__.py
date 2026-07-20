"""External tool placeholders.

Tool instances are inert by default. Instantiate with `live=True` only after adding credential,
approval, and audit handling.
"""

from importlib import import_module

from keystone_agents.tools.announcement_context_tools import (
    retrieve_preprint_announcement_history,
    retrieve_preprint_announcement_history_impl,
    retrieve_rss_announcement_history,
    retrieve_rss_announcement_history_impl,
)
from keystone_agents.tools.apify_tool import ApifyTool
from keystone_agents.tools.approval_tool import ApprovalTool, post_approval_request
from keystone_agents.tools.browserless_tool import BrowserlessTool
from keystone_agents.tools.email_style_tool import (
    DEFAULT_EMAIL_STYLE_PROFILE,
    load_email_style_profile,
    load_email_style_profile_fixture,
    load_email_style_profile_model,
)
from keystone_agents.tools.gmail_tool import (
    GmailTool,
    apply_gmail_labels,
    create_gmail_draft_reply,
    get_gmail_message,
    get_thread,
    list_threads_by_label_filter,
    modify_gmail_message_state,
)
from keystone_agents.tools.html_review_tool import (
    HtmlReviewResult,
    extract_research_claims_from_html,
)
from keystone_agents.tools.internal_data_tools import (
    google_drive_get_file_metadata,
    google_drive_get_file_metadata_impl,
    google_drive_search_files,
    google_drive_search_files_impl,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.memory_tool import (
    check_workflow_duplicate,
    learn_email_style_profile,
    record_workflow_dedup,
    retrieve_chief_of_staff_memory,
    retrieve_memory,
    retrieve_outreach_examples,
    save_approval_decision_memory,
    save_company_profile_memory,
    save_entity_memory,
    save_human_feedback_memory,
    save_opportunity_memory,
    save_outreach_dedup_memory,
    save_retrieval_tool_performance_memory,
)
from keystone_agents.tools.outreach_template_tool import (
    list_outreach_templates,
    load_outreach_template,
)
from keystone_agents.tools.search_provider import (
    ExaSearchProvider,
    FirecrawlSearchProvider,
    SearchProviderName,
    SearchRequest,
    SearchResult,
    SearxngSearchProvider,
    SerperSearchProvider,
    build_search_provider,
)
from keystone_agents.tools.storage_tool import StorageTool
from keystone_agents.tools.web_scrape_tool import WebScrapeTool
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionTool,
    extract_website_content,
)

__all__ = [
    "ApifyTool",
    "ApprovalTool",
    "post_approval_request",
    "BrowserlessTool",
    "DEFAULT_EMAIL_STYLE_PROFILE",
    "load_email_style_profile",
    "load_email_style_profile_fixture",
    "load_email_style_profile_model",
    "GmailTool",
    "apply_gmail_labels",
    "create_gmail_draft_reply",
    "get_gmail_message",
    "get_thread",
    "list_threads_by_label_filter",
    "modify_gmail_message_state",
    "HtmlReviewResult",
    "extract_research_claims_from_html",
    "retrieve_rss_announcement_history",
    "retrieve_rss_announcement_history_impl",
    "retrieve_preprint_announcement_history",
    "retrieve_preprint_announcement_history_impl",
    "google_drive_get_file_metadata",
    "google_drive_get_file_metadata_impl",
    "google_drive_search_files",
    "google_drive_search_files_impl",
    "list_local_context_sources",
    "search_local_context",
    "read_local_context_file",
    "retrieve_chief_of_staff_memory",
    "retrieve_memory",
    "retrieve_outreach_examples",
    "save_approval_decision_memory",
    "save_company_profile_memory",
    "save_entity_memory",
    "save_human_feedback_memory",
    "save_opportunity_memory",
    "save_outreach_dedup_memory",
    "save_retrieval_tool_performance_memory",
    "learn_email_style_profile",
    "check_workflow_duplicate",
    "record_workflow_dedup",
    "list_outreach_templates",
    "load_outreach_template",
    "SearchResult",
    "SearchRequest",
    "SearchProviderName",
    "ExaSearchProvider",
    "FirecrawlSearchProvider",
    "SearxngSearchProvider",
    "SerperSearchProvider",
    "SerperTool",
    "build_search_provider",
    "StorageTool",
    "WebScrapeTool",
    "WebsiteExtractionTool",
    "extract_website_content",
    "DEFAULT_ZOTERO_IMPORTER_SCRIPT",
    "ZOTERO_CONTEXT_TOOL_NAMES",
    "ZOTERO_IMPORT_TOOL_NAMES",
    "ZOTERO_READ_CONTEXT_TOOL_NAMES",
    "zotero_import_article_with_backend",
    "zotero_read_api_metadata",
    "zotero_read_item_children",
    "zotero_read_pdf_attachment_text",
    "zotero_resolve_article_context",
    "zotero_resolve_collection_context",
]


_ZOTERO_CONTEXT_EXPORTS = {
    "DEFAULT_ZOTERO_IMPORTER_SCRIPT",
    "ZOTERO_CONTEXT_TOOL_NAMES",
    "ZOTERO_IMPORT_TOOL_NAMES",
    "ZOTERO_READ_CONTEXT_TOOL_NAMES",
    "zotero_import_article_with_backend",
    "zotero_read_api_metadata",
    "zotero_read_item_children",
    "zotero_read_pdf_attachment_text",
    "zotero_resolve_article_context",
    "zotero_resolve_collection_context",
}


def __getattr__(name: str):
    if name == "SerperTool":
        from keystone_agents.tools.serper_tool import SerperTool

        return SerperTool
    if name in _ZOTERO_CONTEXT_EXPORTS:
        zotero_context_tools = import_module("keystone_agents.tools.zotero_context_tools")
        return getattr(zotero_context_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
