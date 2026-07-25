"""Provider binding for read-only Gmail contact-evidence answers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Any

from keystone_agents.models import GmailContactLookupSDKInput
from keystone_agents.run import SDKSynthesisOutcome, run_retrieved_sdk_synthesis
from keystone_agents.schemas.email_triage import (
    GmailContactLookupResult,
    GmailResolvedContact,
)


class GmailContactBindingError(ValueError):
    """Raised when an SDK answer cannot be bound to provider-returned evidence."""


@dataclass(frozen=True)
class GmailContactLookupExecution:
    """One verified read-only Gmail contact lookup across provider and SDK layers."""

    outcome: SDKSynthesisOutcome
    human_summary: str
    provider_receipt: dict[str, Any]

    @property
    def result(self) -> GmailContactLookupResult:
        return self.outcome.final_output


def run_gmail_contact_lookup_workflow(
    *,
    operator_request: str,
    gmail_query: str,
    max_messages: int = 10,
    label: str | None = None,
    gmail_tool: Any | None = None,
    run_config: Any | None = None,
    live_sdk: bool = True,
    model: str | None = None,
    session: Any | None = None,
    trace_include_sensitive_data: bool | None = None,
) -> GmailContactLookupExecution:
    """Search Gmail once, synthesize with one specialist, and bind the answer."""

    request = str(operator_request or "").strip()
    query = str(gmail_query or "").strip()
    if not request:
        raise ValueError("A current operator request is required for Gmail contact lookup.")
    if not query:
        raise ValueError("A bounded Gmail query is required for contact lookup.")
    if max_messages < 1 or max_messages > 25:
        raise ValueError("Gmail contact lookup max_messages must be between 1 and 25.")

    from keystone_agents.agents.gmail_triage import build_gmail_contact_lookup_agent
    from keystone_agents.tools.gmail_tool import GmailTool

    provider = gmail_tool or GmailTool(live=True)

    def retrieve() -> list[dict[str, Any]]:
        return provider.search_message_summaries(
            label=label,
            max_results=max_messages,
            query=query,
        )

    def normalize(summaries: list[dict[str, Any]]) -> GmailContactLookupSDKInput:
        return GmailContactLookupSDKInput.from_summaries(
            summaries,
            operator_request=request,
            gmail_query=query,
        )

    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_contact_lookup_agent(
            model=model,
            request_text=request,
        ),
        output_type=GmailContactLookupResult,
        retrieve=retrieve,
        normalize=normalize,
        finalize_output=bind_gmail_contact_lookup_result,
        input_summary="bounded Gmail known-contact evidence lookup",
        input_audit_payload={
            "contact_lookup": True,
            "gmail_query": query,
            "max_messages": max_messages,
            "provider_write": False,
        },
        workflow_name="Keystone Gmail Contact Evidence Lookup",
        trace_metadata={
            "workflow_kind": "gmail_contact_lookup",
            "provider": "gmail",
            "provider_write": False,
        },
        run_config=run_config,
        live=live_sdk,
        session=session,
        trace_include_sensitive_data=trace_include_sensitive_data,
        save=False,
        model_label="sdk-live" if live_sdk else "sdk-local",
    )
    result = outcome.final_output
    receipt = gmail_contact_lookup_receipt(
        query=query,
        candidate_count=len(outcome.typed_input.candidates),
        result=result,
    )
    return GmailContactLookupExecution(
        outcome=outcome,
        human_summary=gmail_contact_lookup_human_summary(result),
        provider_receipt=receipt,
    )


def bind_gmail_contact_lookup_result(
    summaries: list[Mapping[str, Any]],
    result: GmailContactLookupResult,
) -> GmailContactLookupResult:
    """Validate every selected contact against exact Gmail message headers."""

    by_message_id = {
        str(summary.get("id") or "").strip(): summary
        for summary in summaries
        if str(summary.get("id") or "").strip()
    }
    if not result.found:
        return result.model_copy(
            update={
                "contacts": [],
                "supporting_message_ids": [],
                "send_enabled": False,
                "provider_write": False,
            }
        )

    bound_contacts: list[GmailResolvedContact] = []
    supporting_ids: list[str] = []
    for contact in result.contacts:
        message_id, summary = _resolve_contact_provider_message(
            summaries=summaries,
            by_message_id=by_message_id,
            contact=contact,
        )
        header_key = "from" if contact.source_field == "from" else "to"
        header = str(summary.get(header_key) or "").strip()
        header_addresses = [
            (str(name or "").strip(), str(address or "").strip())
            for name, address in getaddresses([header])
            if str(address or "").strip()
        ]
        selected = next(
            (
                (name, address)
                for name, address in header_addresses
                if address.casefold() == contact.contact_email.casefold()
            ),
            None,
        )
        if selected is None:
            raise GmailContactBindingError(
                "The Gmail specialist returned an email address that was not present "
                f"in the selected message's {header_key.title()} header."
            )
        expected_thread_id = str(summary.get("threadId") or "").strip()
        if contact.thread_id and contact.thread_id != expected_thread_id:
            raise GmailContactBindingError(
                "The Gmail specialist returned a thread id that did not match the "
                "selected provider message."
            )
        canonical_name, canonical_email = selected
        bound_contacts.append(
            contact.model_copy(
                update={
                    "message_id": message_id,
                    "thread_id": expected_thread_id,
                    "contact_name": canonical_name or contact.contact_name,
                    "contact_email": canonical_email,
                }
            )
        )
        supporting_ids.append(message_id)

    consolidated_contacts = _consolidate_verified_contacts(bound_contacts)
    return result.model_copy(
        update={
            "contacts": consolidated_contacts,
            "supporting_message_ids": list(dict.fromkeys(supporting_ids)),
            "answer": _verified_contact_summary(consolidated_contacts),
            "send_enabled": False,
            "provider_write": False,
        }
    )


def _consolidate_verified_contacts(
    contacts: list[GmailResolvedContact],
) -> list[GmailResolvedContact]:
    """Merge only contacts with strong provider-bound identity evidence."""

    parent = list(range(len(contacts)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left in enumerate(contacts):
        for right_index in range(left_index + 1, len(contacts)):
            if _has_shared_contact_identity(left, contacts[right_index]):
                union(left_index, right_index)

    groups: dict[int, list[tuple[int, GmailResolvedContact]]] = {}
    for index, contact in enumerate(contacts):
        groups.setdefault(find(index), []).append((index, contact))

    consolidated: list[GmailResolvedContact] = []
    for members in groups.values():
        _, representative = min(
            members,
            key=lambda item: _contact_stability_rank(item[1], item[0]),
        )
        contact_name = representative.contact_name or next(
            (
                contact.contact_name
                for _, contact in members
                if contact.contact_name
            ),
            "",
        )
        consolidated.append(
            representative.model_copy(update={"contact_name": contact_name})
        )
    return consolidated


def _has_shared_contact_identity(
    left: GmailResolvedContact,
    right: GmailResolvedContact,
) -> bool:
    """Return whether two verified contacts safely identify the same person."""

    left_email = left.contact_email.casefold()
    right_email = right.contact_email.casefold()
    if left_email == right_email:
        return True
    if not left.thread_id or left.thread_id != right.thread_id:
        return False
    left_name = " ".join(left.contact_name.casefold().split())
    right_name = " ".join(right.contact_name.casefold().split())
    if not left_name or left_name != right_name:
        return False
    left_local, left_domain = _email_parts(left_email)
    right_local, right_domain = _email_parts(right_email)
    return (
        bool(left_local)
        and left_local == right_local
        and _domains_share_stable_parent(left_domain, right_domain)
    )


def _email_parts(address: str) -> tuple[str, str]:
    local, separator, domain = address.casefold().rpartition("@")
    if not separator:
        return "", ""
    return local, domain.rstrip(".")


def _domains_share_stable_parent(left: str, right: str) -> bool:
    if not left or not right or left == right:
        return False
    return left.endswith(f".{right}") or right.endswith(f".{left}")


def _contact_stability_rank(
    contact: GmailResolvedContact,
    original_index: int,
) -> tuple[int, int, int]:
    """Prefer a shallower direct domain while retaining deterministic order."""

    _local, domain = _email_parts(contact.contact_email)
    return domain.count("."), len(domain), original_index


def _resolve_contact_provider_message(
    *,
    summaries: list[Mapping[str, Any]],
    by_message_id: Mapping[str, Mapping[str, Any]],
    contact: GmailResolvedContact,
) -> tuple[str, Mapping[str, Any]]:
    """Resolve one model-selected contact to one exact provider message."""

    header_key = "from" if contact.source_field == "from" else "to"
    exact = by_message_id.get(contact.message_id)
    if exact is not None:
        exact_addresses = {
            str(address or "").strip().casefold()
            for _name, address in getaddresses(
                [str(exact.get(header_key) or "").strip()]
            )
            if str(address or "").strip()
        }
        if contact.contact_email.casefold() in exact_addresses:
            return contact.message_id, exact

        exact_thread_id = str(exact.get("threadId") or "").strip()
        if contact.thread_id and contact.thread_id != exact_thread_id:
            return contact.message_id, exact
        thread_candidates = {exact_thread_id} if exact_thread_id else set()
    else:
        thread_candidates = {
            value
            for value in (contact.message_id, contact.thread_id)
            if str(value or "").strip()
        }

    matches: list[tuple[str, Mapping[str, Any]]] = []
    for summary in summaries:
        message_id = str(summary.get("id") or "").strip()
        thread_id = str(summary.get("threadId") or "").strip()
        if not message_id or thread_id not in thread_candidates:
            continue
        addresses = {
            str(address or "").strip().casefold()
            for _name, address in getaddresses(
                [str(summary.get(header_key) or "").strip()]
            )
            if str(address or "").strip()
        }
        if contact.contact_email.casefold() in addresses:
            matches.append((message_id, summary))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise GmailContactBindingError(
            "The Gmail specialist selected a provider thread with multiple matching "
            "messages; one exact message is required."
        )
    if exact is not None:
        return contact.message_id, exact
    raise GmailContactBindingError(
        "The Gmail specialist selected a contact from an unknown message."
    )


def _verified_contact_summary(contacts: list[GmailResolvedContact]) -> str:
    """Render only provider-bound contact fields as the public found answer."""

    if len(contacts) == 1:
        contact = contacts[0]
        identity = (
            f"{contact.contact_name} <{contact.contact_email}>"
            if contact.contact_name
            else contact.contact_email
        )
        detail = f" ({contact.relationship})" if contact.relationship else ""
        return f"The best-supported contact is {identity}{detail}."
    lines = ["Best-supported contacts:"]
    for contact in contacts:
        identity = (
            f"{contact.contact_name} <{contact.contact_email}>"
            if contact.contact_name
            else contact.contact_email
        )
        detail = f" ({contact.relationship})" if contact.relationship else ""
        lines.append(f"- {identity}{detail}")
    return "\n".join(lines)


def gmail_contact_lookup_human_summary(result: GmailContactLookupResult) -> str:
    """Render a compact operator answer without workflow metadata."""

    if not result.found:
        return result.answer
    return _verified_contact_summary(result.contacts)


def gmail_contact_lookup_receipt(
    *,
    query: str,
    candidate_count: int,
    result: GmailContactLookupResult,
) -> dict[str, Any]:
    """Build a safe provider receipt for Slack/backend verification."""

    return {
        "provider": "gmail",
        "operation": "search_and_read_contact_evidence",
        "query": str(query or "").strip(),
        "candidate_count": int(candidate_count),
        "selected_message_ids": list(result.supporting_message_ids),
        "provider_read": True,
        "provider_write": False,
        "verified": True,
    }
