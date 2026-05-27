"""External tool placeholders.

Tool instances are inert by default. Instantiate with `live=True` only after adding credential,
approval, and audit handling.
"""

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
)
from keystone_agents.tools.html_review_tool import (
    HtmlReviewResult,
    extract_research_claims_from_html,
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
    "HtmlReviewResult",
    "extract_research_claims_from_html",
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
    "FirecrawlSearchProvider",
    "SearxngSearchProvider",
    "SerperSearchProvider",
    "SerperTool",
    "build_search_provider",
    "StorageTool",
    "WebScrapeTool",
    "WebsiteExtractionTool",
    "extract_website_content",
]


def __getattr__(name: str):
    if name == "SerperTool":
        from keystone_agents.tools.serper_tool import SerperTool

        return SerperTool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
