"""Shared provider scope policy derived from the semantic request plan."""

from __future__ import annotations

from keystone_agents.authority.semantic import ExecutionIntentAuthority


def semantic_provider_side_effect_policy(
    manual_plan: object | None,
) -> str | None:
    """Return provider scope from an LLM plan without re-parsing request prose.

    The policy describes what the operator requested. Provider-specific live
    flags, approvals, exact object identity, delete restrictions, and read-back
    checks remain authoritative at the tool boundary.
    """

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if not authority.canonical:
        return None
    assert authority.plan is not None
    plan = authority.plan
    provider = plan.provider_system
    if provider == "unspecified":
        return None
    operations = list(authority.effective_provider_operations(provider))
    target = plan.primary_target or "the selected object"
    mutation_operations = [
        operation
        for operation in operations
        if operation in {"create", "update", "delete", "attach"}
    ]
    operation_text = ", ".join(operations) or "read"

    if not mutation_operations:
        return (
            f"Read-only {provider} scope for {target}: the semantic plan permits only "
            f"{operation_text}. Use bounded typed provider reads and report only verified "
            "provider results. No provider mutation, send, post, publication, or unrelated "
            "system action is permitted."
        )
    if provider == "google_calendar":
        return (
            f"The authenticated operator requested the exact Google Calendar operations "
            f"{operation_text} for {target}. Use only the matching dedicated Calendar "
            "tools, configured primary calendar and timezone, supplied approval reference, "
            "and provider read-back. Resolve a unique existing event for updates/deletes. "
            "Do not mutate Gmail, Slack, Airtable, Workspace files, or unrelated systems."
        )
    if provider == "airtable":
        attachment_scope = (
            " One selected attachment may be uploaded only after the exact created or "
            "matched record and attachment field are known and must be read back."
            if "attach" in mutation_operations
            else ""
        )
        return (
            f"The authenticated operator requested the exact Airtable operations "
            f"{operation_text} for {target}. Read schema first, use the exact table and "
            "field mapping, preserve one provider record identity across related stages, "
            f"and verify every mutation.{attachment_scope} Deletes remain limited to "
            "dedicated approved test or duplicate-compensation gates. Do not mutate other "
            "providers or expand to additional records."
        )
    if provider == "google_workspace":
        return (
            f"The authenticated operator requested the exact Google Workspace operations "
            f"{operation_text} for {target}. Use only typed Workspace tools, the approved "
            "folder/object scope, and provider read-back. Trash/delete remains limited to "
            "the supported exact-object cleanup boundary. Do not share, publish, email, "
            "post, or mutate unrelated files or systems."
        )
    if provider == "gmail":
        return (
            f"The authenticated operator requested the exact Gmail operations "
            f"{operation_text} for {target}. Reads and draft create/update may use only "
            "typed Gmail tools with exact account and object resolution plus provider "
            "read-back. Never send. Draft deletion remains limited to the dedicated marked "
            "test-draft cleanup gate. Do not mutate unrelated messages, drafts, or systems."
        )
    if provider == "zotero":
        return (
            f"The authenticated operator requested the exact Zotero operations "
            f"{operation_text} for {target}. Use typed Zotero tools, exact library/item "
            "identity, capability checks, and provider read-back. Cleanup is limited to "
            "the supported exact marked test object. Do not mutate unrelated library items "
            "or other systems."
        )
    if provider == "slack":
        return (
            f"The semantic plan requests Slack operations {operation_text} for {target}. "
            "Use typed Slack tools and exact channel/thread identity. Draft/review remains "
            "the default; posting requires the existing scoped approval boundary. Do not "
            "post to additional channels or mutate unrelated providers."
        )
    return (
        f"The semantic plan requests provider operations {operation_text} in {provider} "
        f"for {target}. Use only typed tools for that exact scope and require all existing "
        "approval, identity, live-flag, and verification gates. No unrelated action is "
        "permitted."
    )
