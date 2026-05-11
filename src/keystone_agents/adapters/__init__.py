"""Provider adapter interfaces.

These protocols define optional integration seams. Direct implementations in
`keystone_agents.tools`, `keystone_agents.model_provider`, and
`keystone_agents.storage` remain available and should not be replaced by
optional providers unless explicitly configured.
"""

from keystone_agents.adapters.providers import (
    CRMLead,
    CRMProvider,
    CRMWriteResult,
    GmailDraftResult,
    GmailMessage,
    GmailProvider,
    KnowledgeDocument,
    KnowledgeProvider,
    ModelProvider,
    ScrapedPage,
    ScrapeProvider,
    SearchProvider,
    SearchResult,
    SlackMessageResult,
    SlackProvider,
    StorageProvider,
)

__all__ = [
    "GmailDraftResult",
    "GmailMessage",
    "GmailProvider",
    "CRMLead",
    "CRMProvider",
    "CRMWriteResult",
    "KnowledgeDocument",
    "KnowledgeProvider",
    "ModelProvider",
    "SearchProvider",
    "ScrapeProvider",
    "ScrapedPage",
    "SearchResult",
    "SlackMessageResult",
    "SlackProvider",
    "StorageProvider",
]
