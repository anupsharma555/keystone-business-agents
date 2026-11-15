"""Request-bound, local operator context tools for optional agent use.

The host binds identity, current-request provenance, database and capture
authorization before tools are offered. Models choose only a query or bounded
capture content; they cannot choose another scope or a provider operation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal

from pydantic import Field

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.runtime.scoped_memory import (
    ScopedMemoryCaptureInput,
    capture_scoped_memory,
    memory_capture_fingerprint,
    query_scoped_memory_facts,
    revalidate_scoped_memory_facts,
)
from keystone_agents.schemas.memory_scope import (
    MEMORY_CAPTURE_KEY,
    MEMORY_SCOPE_KEY,
    SCOPED_FACT_TYPES,
    MemoryCaptureProvenance,
    SlackMemoryScope,
)
from keystone_agents.sdk import function_tool
from keystone_agents.tools.storage_tool import StorageTool
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    validate_public_http_url,
)

MAX_OPERATOR_CONTEXT_BYTES = 4096
DEFAULT_OPERATOR_CONTEXT_BYTES = 4096
MAX_OPERATOR_CONTEXT_FACTS = 4
MAX_OPERATOR_CONTEXT_QUERIES = 2
MAX_CAPTURE_SOURCE_DESCRIPTOR_BYTES = 512
_MIN_RESPONSE_BYTES = 128
_PROFILE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "operator_context.md"
_WORDS = re.compile(r"[a-z0-9]+")
_PROFILE_ANCHORS = frozenset({
    "background", "skills", "experience", "profile", "identity", "founder",
    "priorities", "priority", "goals", "strategy",
})


@dataclass(frozen=True)
class _ProfileFact:
    key: str
    title: str
    summary: str
    tags: frozenset[str]
    source_checks: tuple[str, ...]


_PROFILE_FACTS = (
    _ProfileFact(
        key="background",
        title="Approved professional background",
        summary=(
            "The operator is a physician-scientist and founder focused on clinical AI, "
            "psychiatry, neuroinformatics, clinical research support, evidence synthesis, "
            "and practical analytics workflows."
        ),
        tags=frozenset({
            "background", "profile", "identity", "founder", "physician", "scientist",
            "clinical", "psychiatry", "neuroinformatics", "research", "analytics", "ai",
        }),
        source_checks=(
            "physician-scientist and founder of",
            "clinical AI, psychiatry, neuroinformatics, clinical research support",
        ),
    ),
    _ProfileFact(
        key="skills",
        title="Approved research and analytics skills",
        summary=(
            "Approved experience includes clinical trial assessment and support, validated "
            "rating scales, source documentation, GCP-aligned workflows, evidence synthesis, "
            "data analysis, Python/Jupyter, and AI-enabled research operations."
        ),
        tags=frozenset({
            "skills", "experience", "profile", "clinical", "trial", "rating", "scales",
            "evidence", "analysis", "python", "jupyter", "research", "operations", "ai",
        }),
        source_checks=(
            "clinical trial assessment/support, validated rating scales, source documentation",
            "GCP-aligned workflows, evidence synthesis, data analysis, Python/Jupyter",
        ),
    ),
    _ProfileFact(
        key="priorities",
        title="Approved decision priorities",
        summary=(
            "The default priority order is risk reduction, review quality, relevance, source "
            "quality, cost, then speed. Outreach and opportunity decisions favor review quality "
            "and source depth."
        ),
        tags=frozenset({
            "priorities", "priority", "prioritize", "goals", "strategy", "decision",
            "risk", "review", "quality", "relevance", "source", "cost", "speed",
            "outreach", "opportunity",
        }),
        source_checks=(
            "Default priority order: risk reduction, review quality, relevance, source quality",
            "For outreach, opportunity qualification, and business-critical decisions",
        ),
    ),
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _byte_count(value: object) -> int:
    compact_utf8 = len(_json(value).encode("utf-8"))
    sdk_json = len(json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8"))
    return max(compact_utf8, sdk_json)


def _saved_capture_receipt(record_id: int) -> dict[str, Any]:
    return {
        "status": "saved", "id": record_id, "verified": True,
        "record_id": str(record_id), "provider": "local_memory", "provider_write": False,
        "operation": "capture_operator_memory", "verification": {"passed": True},
    }


# Reserve a whole receipt before writing, including the largest SQLite row ID.
_MIN_CAPTURE_RESPONSE_BYTES = _byte_count(_saved_capture_receipt(9223372036854775807))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _profile_candidates(query: str) -> tuple[list[dict[str, Any]], str]:
    """Admit only short approved fields still present in the authority file."""

    terms = set(_WORDS.findall(query.lower()))
    if not terms:
        return [], ""
    try:
        source = _PROFILE_PATH.read_text(encoding="utf-8")
    except OSError:
        return [], ""
    normalized = " ".join(source.split())
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    explicit_profile_query = bool(terms & _PROFILE_ANCHORS)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for fact in _PROFILE_FACTS:
        matches = terms & fact.tags
        if len(matches) < (1 if explicit_profile_query else 2):
            continue
        if not all(check in normalized for check in fact.source_checks):
            continue
        ranked.append((len(matches), _profile_fact_dict(fact)))
    ranked.sort(key=lambda pair: -pair[0])
    return [fact for _, fact in ranked], source_hash


def _profile_fact_dict(fact: _ProfileFact) -> dict[str, Any]:
    return {
        "memory_id": f"approved_profile:{fact.key}",
        "memory_type": "approved_operator_profile",
        "title": fact.title,
        "summary": fact.summary,
        "source_ids": [f"approved_operator_profile:{fact.key}"],
        "source_urls": [],
    }


def _valid_public_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        return validate_public_http_url(url, resolve_hostname=False) == url
    except (ValueError, WebsiteExtractionError):
        return False


def _allowed_capture_sources(source_refs: Mapping[str, str]) -> list[dict[str, str]]:
    """Expose only whole, validated host references within a small model budget."""

    allowed: list[dict[str, str]] = []
    for source_id, url in source_refs.items():
        if (
            not isinstance(source_id, str) or not source_id or len(source_id) > 200
            or not _valid_public_url(url)
        ):
            continue
        candidate = {"id": source_id, "url": url}
        if _byte_count([*allowed, candidate]) > MAX_CAPTURE_SOURCE_DESCRIPTOR_BYTES:
            continue
        allowed.append(candidate)
        if len(allowed) == 2:
            break
    return allowed


def _validate_initial_facts(
    value: list[dict[str, Any]] | None, *, max_bytes: int,
    include_approved_profile: bool,
) -> list[dict[str, Any]]:
    """Reject any seed that is not one whole prior admitted fact projection."""

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_OPERATOR_CONTEXT_FACTS:
        raise ValueError("Initial operator facts must be a list of at most four facts.")
    if not value:
        return []
    approved_profiles, _ = (
        _profile_candidates("profile background skills priorities")
        if include_approved_profile else ([], "")
    )
    profile_by_id = {fact["memory_id"]: fact for fact in approved_profiles}
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Initial operator fact must be a dictionary.")
        memory_id = raw.get("memory_id")
        if raw.get("memory_type") == "approved_operator_profile":
            if not include_approved_profile:
                raise ValueError("Approved operator profile is not enabled for this scope.")
            if raw != profile_by_id.get(memory_id):
                raise ValueError("Initial profile fact is not current approved context.")
        else:
            required = {
                "memory_id", "memory_type", "title", "summary", "source_ids",
                "source_urls", "created_at",
            }
            if set(raw) != required or type(memory_id) is not int or memory_id <= 0:
                raise ValueError("Initial scoped memory fact has invalid shape.")
            if raw.get("memory_type") not in SCOPED_FACT_TYPES:
                raise ValueError("Initial scoped memory type is not admissible.")
            for field, limit in (("title", 120), ("summary", 350)):
                text = raw.get(field)
                if not isinstance(text, str) or not text.strip() or len(text) > limit:
                    raise ValueError("Initial scoped memory text is out of bounds.")
            if not isinstance(raw.get("created_at"), str) or not raw["created_at"]:
                raise ValueError("Initial scoped memory timestamp is missing.")
            source_ids = raw.get("source_ids")
            source_urls = raw.get("source_urls")
            if (
                not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 2
                or any(not isinstance(ref, str) or not ref or len(ref) > 200 for ref in source_ids)
                or len(source_ids) != len(set(source_ids))
                or not isinstance(source_urls, list) or len(source_urls) > 2
                or any(not _valid_public_url(url) for url in source_urls)
            ):
                raise ValueError("Initial scoped memory references are invalid.")
        identity = str(memory_id)
        if identity in seen:
            raise ValueError("Initial operator facts contain a repeated identity.")
        seen.add(identity)
        facts.append(json.loads(_json(raw)))
    context = {"facts": facts}
    response = {
        "status": "ok", "facts": facts, "omitted_count": 0,
        "remaining_fact_slots": MAX_OPERATOR_CONTEXT_FACTS - len(facts),
    }
    if _byte_count(context) > max_bytes or _byte_count(response) > max_bytes:
        raise ValueError("Initial operator facts exceed the current byte budget.")
    return facts


class OperatorContextCapability:
    """One host-bound tool bundle and evidence ledger for a single request."""

    def __init__(
        self, *, scope: SlackMemoryScope, provenance: MemoryCaptureProvenance,
        database_url: str, raw_request: str,
        source_refs: Mapping[str, str] | None = None,
        capture_authorized: bool = False,
        include_approved_profile: bool = False,
        max_bytes: int = DEFAULT_OPERATOR_CONTEXT_BYTES,
        initial_facts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.scope = SlackMemoryScope.model_validate(scope)
        self.provenance = MemoryCaptureProvenance.model_validate(provenance)
        if not database_url:
            raise ValueError("Operator context requires an explicit local database URL.")
        if not isinstance(raw_request, str) or not raw_request.strip():
            raise ValueError("Operator context requires the current raw request.")
        expected = memory_capture_fingerprint(
            self.scope, thread_ts=self.provenance.thread_ts,
            request_ts=self.provenance.request_ts, raw_request=raw_request,
        )
        if expected != self.provenance.request_fingerprint:
            raise ValueError("Operator context provenance does not match this request.")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("Operator context byte budget must be a nonnegative integer.")
        self.max_bytes = min(MAX_OPERATOR_CONTEXT_BYTES, max_bytes)
        self.include_approved_profile = include_approved_profile is True
        validated_facts = _validate_initial_facts(
            initial_facts, max_bytes=self.max_bytes,
            include_approved_profile=self.include_approved_profile,
        )
        self.capture_authorized = capture_authorized is True
        self._allowed_capture_sources = (
            _allowed_capture_sources(source_refs or {}) if self.capture_authorized else []
        )
        self._source_refs = (
            {item["id"]: item["url"] for item in self._allowed_capture_sources}
            if self.capture_authorized else dict(source_refs or {})
        )
        self._storage = StorageTool(
            database_url=database_url, agent_name="operator_context", dry_run=False,
        )
        scoped_facts = revalidate_scoped_memory_facts(
            self._storage, scope=self.scope,
            facts=[fact for fact in validated_facts if type(fact["memory_id"]) is int],
            max_bytes=self.max_bytes,
        )
        valid_scoped_ids = {fact["memory_id"] for fact in scoped_facts}
        seed_facts = [
            fact for fact in validated_facts
            if type(fact["memory_id"]) is not int or fact["memory_id"] in valid_scoped_ids
        ]
        self._lock = Lock()
        self._capture_identity: tuple[str, str, str, tuple[str, ...]] | None = None
        self._capture_receipt: dict[str, Any] | None = None
        self.usage: dict[str, int] = {
            "query_calls": 0, "capture_calls": 0, "facts_returned": 0,
            "omitted_count": 0, "response_bytes": 0, "query_limit_rejections": 0,
            "capture_limit_rejections": 0, "capture_saved_count": 0,
        }
        # Evidence may contain approved fact text for the next model stage.
        # Telemetry callbacks should use usage, which contains counts only.
        self.evidence: dict[str, Any] = {
            "schema": "keystone.operator_context_evidence.v1",
            "scope_hash": _hash(self.scope.model_dump(mode="json")),
            "provenance_hash": _hash(self.provenance.model_dump(mode="json")),
            "raw_request_sha256": hashlib.sha256(raw_request.encode("utf-8")).hexdigest(),
            "model_context": {"facts": seed_facts} if seed_facts else None,
            "serialized_bytes": _byte_count({"facts": seed_facts}) if seed_facts else 0,
            "omitted_count": 0,
            "query_count": 0, "capture_count": 0, "capture_receipts": [],
            "profile_source_sha256": (
                hashlib.sha256(_PROFILE_PATH.read_bytes()).hexdigest()
                if any(fact["memory_type"] == "approved_operator_profile" for fact in seed_facts)
                else ""
            ),
            "max_bytes": self.max_bytes,
            "remaining_fact_slots": MAX_OPERATOR_CONTEXT_FACTS - len(seed_facts),
            "remaining_bytes": self.max_bytes - (
                _byte_count({"facts": seed_facts}) if seed_facts else 0
            ),
        }
        self.tools: list[Any] = self._build_tools() if self.max_bytes >= _MIN_RESPONSE_BYTES else []
        if not self.tools:
            self.evidence["omission_reason"] = "context_budget_unavailable"

    @property
    def descriptor(self) -> dict[str, Any]:
        """Data-only binding receipt; reconstruct tools from trusted host inputs."""

        descriptor = {
            "schema": "keystone.operator_context_capability.v1",
            "scope_hash": self.evidence["scope_hash"],
            "provenance_hash": self.evidence["provenance_hash"],
            "raw_request_sha256": self.evidence["raw_request_sha256"],
            "capture_authorized": self.capture_authorized,
            "max_bytes": self.max_bytes,
            "seed_fact_count": len((self.evidence.get("model_context") or {}).get("facts") or []),
            "tool_names": [tool.name for tool in self.tools],
        }
        if self.capture_authorized and self.max_bytes >= _MIN_CAPTURE_RESPONSE_BYTES:
            descriptor["allowed_capture_sources"] = list(self._allowed_capture_sources)
        return descriptor

    @property
    def sdk_tools(self) -> list[Any]:
        """Native SDK views for callers that do not use build_sdk_agent."""

        return [getattr(tool, "sdk_tool", tool) for tool in self.tools]

    def _build_tools(self) -> list[Any]:
        @function_tool(**keystone_tool_guardrail_kwargs())
        def query_operator_context(
            query: Annotated[str, Field(min_length=1, max_length=300)],
            include_profile: bool = True,
        ) -> str:
            """Read at most four relevant, approved facts for this request.

            Query operator background, skills, priorities or saved preferences only
            when useful to the current answer. Scope and storage are host-bound.
            Empty or unrelated queries return no facts. No provider call occurs.
            """

            return self._query(query, include_profile=include_profile)

        tools: list[Any] = [query_operator_context]
        if self.capture_authorized and self.max_bytes >= _MIN_CAPTURE_RESPONSE_BYTES:
            @function_tool(**keystone_tool_guardrail_kwargs())
            def capture_operator_memory(
                kind: Literal["preference", "reference"],
                title: Annotated[str, Field(min_length=1, max_length=120)],
                summary: Annotated[str, Field(min_length=1, max_length=350)],
                source_ref_ids: Annotated[list[str], Field(max_length=2)] | None = None,
            ) -> str:
                """Save one current operator-approved preference or exact reference.

                Use only this request's approved source_ref_ids. The host owns the
                destination and request identity; this tool cannot send or publish.
                """

                return self._capture(kind, title, summary, source_ref_ids or [])

            tools.append(capture_operator_memory)
        return tools

    def _query(self, query: str, *, include_profile: bool) -> str:
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            return self._empty_response("query_required")
        with self._lock:
            if self.usage["query_calls"] >= MAX_OPERATOR_CONTEXT_QUERIES:
                self.usage["query_limit_rejections"] += 1
                return self._empty_response("query_limit_reached")
            return self._query_admitted(query, include_profile=include_profile)

    def _query_admitted(self, query: str, *, include_profile: bool) -> str:
        self.usage["query_calls"] += 1
        self.evidence["query_count"] += 1
        memory_unavailable = False
        try:
            projection = query_scoped_memory_facts(
                self._storage, scope=self.scope, query=query, max_bytes=self.max_bytes,
            )
            memory_facts = list((projection.model_context or {}).get("facts") or [])
            memory_omitted = projection.omitted_count
        except Exception as exc:
            self.evidence["query_error_type"] = type(exc).__name__
            memory_unavailable = True
            memory_facts = []
            memory_omitted = 0
        profile_facts, source_hash = (
            _profile_candidates(query)
            if include_profile and self.include_approved_profile else ([], "")
        )
        terms = set(_WORDS.findall(query.lower()))
        ordered = (
            [*profile_facts, *memory_facts]
            if terms & _PROFILE_ANCHORS else [*memory_facts, *profile_facts]
        )
        prior = list((self.evidence.get("model_context") or {}).get("facts") or [])
        ledger = list(prior)
        ledger_by_id = {str(fact.get("memory_id")): fact for fact in ledger}
        facts: list[dict[str, Any]] = []
        already_admitted_ids: list[int | str] = []
        budget_omitted = False
        for fact in ordered:
            identity = str(fact.get("memory_id"))
            if (
                any(str(selected.get("memory_id")) == identity for selected in facts)
                or any(str(seen) == identity for seen in already_admitted_ids)
            ):
                continue
            admitted = ledger_by_id.get(identity)
            if admitted is not None:
                already_admitted_ids.append(admitted["memory_id"])
                continue
            if len(ledger) >= MAX_OPERATOR_CONTEXT_FACTS:
                continue
            admitted = fact
            if _byte_count({"facts": [*ledger, admitted]}) > self.max_bytes:
                budget_omitted = True
                continue
            # Reserve wrapper space for status and omission metadata before
            # admitting a fact to the shared forwardable ledger.
            candidate_response = {
                "status": "memory_unavailable", "facts": [*facts, admitted],
                "already_admitted_ids": already_admitted_ids,
                "omitted_count": 99, "remaining_fact_slots": 0,
            }
            if _byte_count(candidate_response) > self.max_bytes:
                budget_omitted = True
                continue
            facts.append(admitted)
            ledger.append(admitted)
            ledger_by_id[identity] = admitted
        omitted = memory_omitted + len(ordered) - len(facts) - len(already_admitted_ids)
        status = (
            "partial" if memory_unavailable and facts else
            "memory_unavailable" if memory_unavailable else
            "ok" if facts else
            "capacity_reached" if omitted and len(ledger) >= MAX_OPERATOR_CONTEXT_FACTS else
            "budget_exhausted" if omitted and budget_omitted else
            "already_admitted" if already_admitted_ids else "empty"
        )
        response = {
            "status": status, "facts": facts,
            "already_admitted_ids": already_admitted_ids, "omitted_count": omitted,
            "remaining_fact_slots": MAX_OPERATOR_CONTEXT_FACTS - len(ledger),
        }
        if _byte_count(response) > self.max_bytes:
            return self._empty_response("context_budget_unavailable")
        self.usage["facts_returned"] += len(facts)
        self.usage["omitted_count"] += omitted
        self.usage["response_bytes"] += _byte_count(response)
        self.evidence["omitted_count"] += omitted
        if any(fact.get("memory_type") == "approved_operator_profile" for fact in facts):
            self.evidence["profile_source_sha256"] = source_hash
        context = {"facts": ledger} if ledger else None
        self.evidence["model_context"] = context
        self.evidence["serialized_bytes"] = _byte_count(context) if context else 0
        self.evidence["remaining_fact_slots"] = MAX_OPERATOR_CONTEXT_FACTS - len(ledger)
        self.evidence["remaining_bytes"] = (
            self.max_bytes - self.evidence["serialized_bytes"]
        )
        return _json(response)

    def _empty_response(self, reason: str) -> str:
        response = {"status": reason, "facts": []}
        if _byte_count(response) > self.max_bytes:
            response = {}
        self.usage["response_bytes"] += _byte_count(response)
        return _json(response)

    def _capture(
        self, kind: str, title: str, summary: str, source_ref_ids: list[str],
    ) -> str:
        self.usage["capture_calls"] += 1
        with self._lock:
            output = self._capture_locked(kind, title, summary, source_ref_ids)
            self.usage["response_bytes"] += _byte_count(json.loads(output))
            return output

    def _capture_locked(
        self, kind: str, title: str, summary: str, source_ref_ids: list[str],
    ) -> str:
        if self.max_bytes < _MIN_CAPTURE_RESPONSE_BYTES:
            self.usage["capture_limit_rejections"] += 1
            return _json({"status": "blocked", "reason": "capture_budget_unavailable"})
        identity = (kind, title, summary, tuple(source_ref_ids))
        if self._capture_receipt is not None:
            if identity == self._capture_identity:
                return _json(self._capture_receipt)
            self.usage["capture_limit_rejections"] += 1
            return _json({"status": "blocked", "reason": "capture_limit_reached"})
        try:
            capture = ScopedMemoryCaptureInput(
                kind=kind, title=title, summary=summary, source_ref_ids=source_ref_ids,
            )
            receipt = capture_scoped_memory(
                self._storage, scope=self.scope, provenance=self.provenance,
                capture_authorized=self.capture_authorized,
                capture=capture, source_refs=self._source_refs,
            )
        except Exception as exc:
            self.evidence["capture_error_type"] = type(exc).__name__
            return _json({"status": "blocked", "reason": "capture_scope_or_storage_unavailable"})
        receipt_id = receipt.get("id")
        if receipt.get("status") != "saved" or not isinstance(receipt_id, int):
            return _json({"status": "unverified", "reason": "capture_receipt_missing"})
        try:
            with self._storage.store.managed_connection() as connection:
                row = connection.execute(
                    "SELECT title, summary, source_ids_json, metadata_json "
                    "FROM memory_items WHERE id=?", (receipt_id,),
                ).fetchone()
            metadata = json.loads(row["metadata_json"]) if row is not None else {}
            saved_source_ids = json.loads(row["source_ids_json"]) if row is not None else []
        except Exception as exc:
            self.evidence["capture_error_type"] = type(exc).__name__
            return _json({"status": "unverified", "id": receipt_id})
        if (
            row is None
            or row["title"] != capture.title
            or row["summary"] != capture.summary
            or saved_source_ids != (
                capture.source_ref_ids
                or [f"operator-request:{self.provenance.request_fingerprint}"]
            )
            or metadata.get(MEMORY_SCOPE_KEY) != self.scope.model_dump(mode="json")
            or metadata.get(MEMORY_CAPTURE_KEY)
            != self.provenance.model_dump(mode="json")
        ):
            self.evidence["capture_error_type"] = "ReadbackMismatch"
            return _json({"status": "unverified", "id": receipt_id})
        safe_receipt = _saved_capture_receipt(receipt_id)
        self._capture_identity = identity
        self._capture_receipt = safe_receipt
        self.usage["capture_saved_count"] = 1
        self.evidence["capture_count"] = 1
        receipts = self.evidence["capture_receipts"]
        receipts.append(safe_receipt)
        return _json(safe_receipt)


__all__ = ["OperatorContextCapability"]
