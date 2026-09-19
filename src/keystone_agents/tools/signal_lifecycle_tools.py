"""WorkItem-owned trigger, deduplication, and resume tools for feed signals."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.runtime.work_item_lock import work_item_execution_lock
from keystone_agents.schemas.signal_lifecycle import (
    SIGNAL_LIFECYCLE_ARTIFACT_TYPE,
    SIGNAL_LIFECYCLE_STAGE_ORDER,
    SignalDisposition,
    SignalIdentityDecision,
    SignalLifecycleCheckpoint,
    SignalLifecycleStage,
    SignalLifecycleStatus,
    SignalSourceKind,
)
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemEvent,
    utc_now_iso,
)
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env

try:  # pragma: no cover - fcntl is available on supported Unix runtimes.
    import fcntl
except ImportError:  # pragma: no cover - process-local fallback for non-Unix tooling.
    fcntl = None

_MAX_CANDIDATES = 25
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_PROCESS_ADMISSION_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_ADMISSION_LOCKS_GUARD = threading.Lock()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def signal_trigger_id(
    *,
    source_kind: SignalSourceKind | str,
    trigger_ref: str,
    dedupe_scope: str,
) -> str:
    """Return the stable trigger id shared by admission and deterministic execution."""

    kind = SignalSourceKind(source_kind)
    trigger_ref_digest = _digest(_bounded_text(trigger_ref, max_chars=500))
    scope_digest = _digest(_bounded_text(dedupe_scope, max_chars=300))
    return f"sig_{_digest(f'{kind.value}|{scope_digest}|{trigger_ref_digest}')[:24]}"


@contextmanager
def _signal_admission_guard(store: SQLiteStore):
    """Serialize admission for one local SQLite database across threads/processes.

    Keystone currently operates this lifecycle on a single host. The adjacent advisory
    lock prevents two compliant workers from both admitting the same trigger/revision
    between the durable read and checkpoint write without adding a risky schema migration.
    """

    lock_key = (
        str(Path(store.path).expanduser().resolve())
        if store.path != ":memory:"
        else store.path
    )
    with _PROCESS_ADMISSION_LOCKS_GUARD:
        process_lock = _PROCESS_ADMISSION_LOCKS.setdefault(lock_key, threading.Lock())
    with process_lock:
        if store.path == ":memory:" or fcntl is None:
            yield "process_lock"
            return
        lock_path = Path(f"{lock_key}.signal-lifecycle.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield "sqlite_path_advisory_file_lock"
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _bounded_text(value: object, *, max_chars: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:max_chars]


def _canonical_url(value: object) -> str:
    text = _bounded_text(value, max_chars=1200)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.casefold().rstrip("/")
    if not parts.scheme or not parts.netloc:
        return text.casefold().rstrip("/")
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_QUERY_KEYS
            and not key.casefold().startswith(_TRACKING_QUERY_PREFIXES)
        ]
    )
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, query, ""))


def _publication_identity(value: object) -> str:
    normalized = _bounded_text(value, max_chars=300).casefold()
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized)
    normalized = re.sub(r"^doi:\s*", "", normalized)
    normalized = re.sub(r"v\d+$", "", normalized)
    return normalized.strip().rstrip("/")


def _revision_token(item: Mapping[str, Any]) -> str:
    explicit = _bounded_text(item.get("content_version") or item.get("version"), max_chars=80)
    if explicit:
        return explicit.casefold()
    joined_ids = " ".join(
        str(value or "")
        for value in [
            item.get("feed_item_id"),
            item.get("url"),
            *(item.get("publication_ids") or []),
        ]
    )
    versions = [int(value) for value in re.findall(r"v(\d+)(?:$|[?&#/\s])", joined_ids, re.I)]
    if versions:
        return f"v{max(versions)}"
    published = _bounded_text(item.get("published_at"), max_chars=80)
    if published:
        return published.casefold()
    content_basis = "|".join(
        _bounded_text(item.get(key), max_chars=1000)
        for key in ("title", "summary", "selection_reason")
    )
    return f"content:{_digest(content_basis)[:16]}"


def signal_item_identity(
    item: Mapping[str, Any],
    *,
    source_kind: SignalSourceKind | str,
) -> tuple[str, str, str]:
    """Return item ref, canonical identity, and revision identity."""

    kind = SignalSourceKind(source_kind)
    item_ref = _bounded_text(item.get("feed_item_id") or item.get("url"), max_chars=500)
    publication_ids = item.get("publication_ids") or []
    if not isinstance(publication_ids, list | tuple):
        publication_ids = [publication_ids]
    canonical = ""
    if kind == SignalSourceKind.PREPRINTS:
        canonical = next(
            (
                f"publication:{normalized}"
                for value in publication_ids
                if (normalized := _publication_identity(value))
            ),
            "",
        )
    if not canonical:
        url = _canonical_url(item.get("url"))
        if kind == SignalSourceKind.PREPRINTS:
            url = re.sub(r"v\d+$", "", url, flags=re.I)
        if url:
            canonical = f"url:{url}"
    if not canonical and item_ref:
        canonical = f"item:{_publication_identity(item_ref)}"
    if not canonical:
        return item_ref, "", ""
    revision = f"rev_{_digest(f'{canonical}|{_revision_token(item)}')[:24]}"
    return item_ref, canonical, revision


def _revision_rank(item: Mapping[str, Any], index: int) -> tuple[int, str, int]:
    token = _revision_token(item)
    version = re.search(r"^v(\d+)$", token)
    return (int(version.group(1)) if version else 0, token, -index)


def _artifact_checkpoint(artifact: WorkItemArtifactRef) -> SignalLifecycleCheckpoint | None:
    if artifact.artifact_type != SIGNAL_LIFECYCLE_ARTIFACT_TYPE:
        return None
    payload = artifact.metadata.get("checkpoint")
    if not isinstance(payload, Mapping):
        return None
    try:
        return SignalLifecycleCheckpoint.model_validate(payload)
    except ValueError:
        return None


def _checkpoints(work_item: WorkItem) -> list[SignalLifecycleCheckpoint]:
    return [
        checkpoint
        for artifact in work_item.artifact_refs
        if (checkpoint := _artifact_checkpoint(artifact)) is not None
    ]


def _all_work_items(store: SQLiteStore) -> list[WorkItem]:
    """Read every durable WorkItem so deduplication does not expire after a list cap."""

    items: list[WorkItem] = []
    for row in store.fetch_all("work_items"):
        payload = row.get("work_item_json")
        try:
            parsed = json.loads(payload) if isinstance(payload, str) else payload
            items.append(WorkItem.model_validate(parsed))
        except (TypeError, ValueError):
            continue
    return items


def _checkpoint_artifact(checkpoint: SignalLifecycleCheckpoint) -> WorkItemArtifactRef:
    return WorkItemArtifactRef(
        artifact_type=SIGNAL_LIFECYCLE_ARTIFACT_TYPE,
        artifact_id=checkpoint.trigger_id,
        source_agent=f"{checkpoint.source_kind.value}_context_agent",
        approval_state="internal_checkpoint",
        title=f"{checkpoint.source_kind.value} signal lifecycle",
        summary=(
            f"{checkpoint.status.value}; {len(checkpoint.admitted_revision_ids)} admitted; "
            f"{len(checkpoint.duplicate_revision_ids)} duplicates"
        ),
        selected=True,
        metadata={
            "checkpoint": checkpoint.model_dump(mode="json"),
            "send_enabled": False,
            "external_write_performed": False,
        },
    )


def _replace_checkpoint(work_item: WorkItem, checkpoint: SignalLifecycleCheckpoint) -> WorkItem:
    artifact = _checkpoint_artifact(checkpoint)
    artifacts = [
        existing
        for existing in work_item.artifact_refs
        if not (
            existing.artifact_type == artifact.artifact_type
            and existing.artifact_id == artifact.artifact_id
        )
    ]
    return work_item.model_copy(update={"artifact_refs": [*artifacts, artifact]}).touch()


def _persist_checkpoint(
    store: SQLiteStore,
    work_item: WorkItem,
    checkpoint: SignalLifecycleCheckpoint,
    *,
    event_type: str,
    summary: str,
) -> WorkItem:
    with store.transaction():
        updated = _replace_checkpoint(work_item, checkpoint)
        artifact = _checkpoint_artifact(checkpoint)
        store.save_work_item(updated)
        store.save_work_item_artifact(updated.id, artifact)
        store.save_work_item_event(
            updated.id,
            WorkItemEvent(
                event_type=event_type,
                actor=f"{checkpoint.source_kind.value}_context_agent",
                summary=summary,
                metadata={
                    "schema": checkpoint.schema_name,
                    "trigger_id": checkpoint.trigger_id,
                    "status": checkpoint.status.value,
                    "completed_stage": (
                        checkpoint.completed_stages[-1].value
                        if checkpoint.completed_stages
                        else ""
                    ),
                    "next_stage": checkpoint.next_stage.value if checkpoint.next_stage else "",
                    "admitted_count": len(checkpoint.admitted_revision_ids),
                    "duplicate_count": len(checkpoint.duplicate_revision_ids),
                    "dry_run": checkpoint.dry_run,
                    "provider_calls_made": checkpoint.provider_calls_made,
                    "external_writes_performed": False,
                    "admission_guard": checkpoint.admission_guard,
                },
            ),
        )
    return updated


def _parse_candidates(candidate_items_json: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(candidate_items_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("candidate_items_json must be a JSON array") from exc
    if not isinstance(payload, list):
        raise ValueError("candidate_items_json must be a JSON array")
    if len(payload) > _MAX_CANDIDATES:
        raise ValueError(f"signal lifecycle accepts at most {_MAX_CANDIDATES} candidates")
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def _prior_identity_maps(
    store: SQLiteStore,
    *,
    source_kind: SignalSourceKind,
    dedupe_scope_digest: str,
) -> tuple[dict[str, str], dict[str, str]]:
    revisions: dict[str, str] = {}
    canonical: dict[str, str] = {}
    for item in _all_work_items(store):
        for checkpoint in _checkpoints(item):
            if (
                checkpoint.source_kind != source_kind
                or checkpoint.dedupe_scope_digest != dedupe_scope_digest
                or checkpoint.status
                not in {
                    SignalLifecycleStatus.READY,
                    SignalLifecycleStatus.PARTIAL_FAILURE,
                    SignalLifecycleStatus.COMPLETED,
                }
            ):
                continue
            for decision in checkpoint.decisions:
                if decision.disposition not in {
                    SignalDisposition.ADMITTED,
                    SignalDisposition.UPDATED,
                }:
                    continue
                revisions[decision.revision_identity] = item.id
                canonical[decision.canonical_identity] = item.id
    return revisions, canonical


def _matching_trigger(
    store: SQLiteStore,
    trigger_id: str,
) -> tuple[WorkItem, SignalLifecycleCheckpoint] | None:
    for item in _all_work_items(store):
        for checkpoint in _checkpoints(item):
            if checkpoint.trigger_id == trigger_id:
                return item, checkpoint
    return None


def prepare_signal_lifecycle_checkpoint_impl(
    *,
    work_item_id: str,
    source_kind: SignalSourceKind | Literal["rss", "preprints"],
    trigger_ref: str,
    dedupe_scope: str,
    candidate_items_json: str,
    trigger_label: str = "",
    scheduled: bool = False,
    dry_run: bool = True,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Classify one bounded trigger and optionally persist its resume checkpoint."""

    store = SQLiteStore(database_url or database_url_from_env())
    with (
        work_item_execution_lock(database_url or database_url_from_env(), work_item_id),
        _signal_admission_guard(store) as admission_guard,
    ):
        result = _prepare_signal_lifecycle_checkpoint_under_guard(
            work_item_id=work_item_id,
            source_kind=source_kind,
            trigger_ref=trigger_ref,
            dedupe_scope=dedupe_scope,
            candidate_items_json=candidate_items_json,
            trigger_label=trigger_label,
            scheduled=scheduled,
            dry_run=dry_run,
            store=store,
            admission_guard=admission_guard,
        )
    return {**result, "admission_guard": admission_guard}


def _prepare_signal_lifecycle_checkpoint_under_guard(
    *,
    work_item_id: str,
    source_kind: SignalSourceKind | Literal["rss", "preprints"],
    trigger_ref: str,
    dedupe_scope: str,
    candidate_items_json: str,
    trigger_label: str,
    scheduled: bool,
    dry_run: bool,
    store: SQLiteStore,
    admission_guard: str,
) -> dict[str, Any]:
    """Run one admission decision while the database-scoped guard is held."""

    clean_work_item_id = _bounded_text(work_item_id, max_chars=200)
    clean_trigger_ref = _bounded_text(trigger_ref, max_chars=500)
    clean_scope = _bounded_text(dedupe_scope, max_chars=300)
    if not clean_work_item_id or not clean_trigger_ref or not clean_scope:
        return {
            "status": "blocked",
            "reason": "work_item_id_trigger_ref_and_dedupe_scope_are_required",
            "dry_run": dry_run,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }
    kind = SignalSourceKind(source_kind)
    work_item = store.get_work_item(clean_work_item_id)
    if work_item is None:
        return {
            "status": "blocked",
            "reason": "work_item_not_found",
            "work_item_id": clean_work_item_id,
            "dry_run": dry_run,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }
    if work_item.current_route.value != f"{kind.value}_context_agent":
        return {
            "status": "blocked",
            "reason": "work_item_route_does_not_own_signal_kind",
            "work_item_id": clean_work_item_id,
            "route": work_item.current_route.value,
            "source_kind": kind.value,
            "dry_run": dry_run,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }

    candidates = _parse_candidates(candidate_items_json)
    trigger_ref_digest = _digest(clean_trigger_ref)
    scope_digest = _digest(clean_scope)
    trigger_id = signal_trigger_id(
        source_kind=kind,
        trigger_ref=clean_trigger_ref,
        dedupe_scope=clean_scope,
    )
    matched = _matching_trigger(store, trigger_id)
    if matched is not None:
        matched_item, existing = matched
        if matched_item.id == work_item.id:
            if existing.status in {
                SignalLifecycleStatus.COMPLETED,
                SignalLifecycleStatus.DUPLICATE_TRIGGER,
                SignalLifecycleStatus.NO_ACTION,
            }:
                return {
                    "status": existing.status.value,
                    "idempotent_reuse": True,
                    "checkpoint": existing.model_dump(mode="json"),
                    "provider_calls_made": 0,
                    "external_writes_performed": False,
                }
            resumed = existing.model_copy(
                update={
                    "resumed": True,
                    "attempt_count": min(100, existing.attempt_count + 1),
                    "dry_run": dry_run,
                    "admission_guard": admission_guard,
                    "updated_at": utc_now_iso(),
                    "safe_next_action": (
                        f"Resume at {existing.next_stage.value}."
                        if existing.next_stage
                        else "Inspect the existing checkpoint before continuing."
                    ),
                }
            )
            if not dry_run:
                _persist_checkpoint(
                    store,
                    work_item,
                    resumed,
                    event_type="signal_lifecycle_resumed",
                    summary="Resumed the existing signal lifecycle checkpoint.",
                )
            return {
                "status": "dry_run" if dry_run else "resumed",
                "idempotent_reuse": True,
                "checkpoint": resumed.model_dump(mode="json"),
                "provider_calls_made": 0,
                "external_writes_performed": False,
            }

        duplicate = SignalLifecycleCheckpoint(
            work_item_id=work_item.id,
            source_kind=kind,
            trigger_id=trigger_id,
            trigger_ref_digest=trigger_ref_digest,
            dedupe_scope_digest=scope_digest,
            trigger_label=_bounded_text(trigger_label, max_chars=160),
            scheduled=scheduled,
            status=SignalLifecycleStatus.DUPLICATE_TRIGGER,
            next_stage=None,
            canonical_work_item_id=matched_item.id,
            canonical_work_item_ids=[matched_item.id],
            safe_next_action=(
                f"Reuse canonical WorkItem {matched_item.id}; do not create duplicate work."
            ),
            dry_run=dry_run,
            admission_guard=admission_guard,
        )
        if not dry_run:
            _persist_checkpoint(
                store,
                work_item,
                duplicate,
                event_type="signal_trigger_suppressed",
                summary="Suppressed a duplicate signal trigger in favor of its canonical WorkItem.",
            )
        return {
            "status": "dry_run" if dry_run else "duplicate_trigger",
            "idempotent_reuse": True,
            "checkpoint": duplicate.model_dump(mode="json"),
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }

    prior_revisions, prior_canonical = _prior_identity_maps(
        store,
        source_kind=kind,
        dedupe_scope_digest=scope_digest,
    )
    indexed: list[tuple[int, dict[str, Any], str, str, str]] = []
    invalid: list[SignalIdentityDecision] = []
    for index, candidate in enumerate(candidates):
        item_ref, canonical, revision = signal_item_identity(candidate, source_kind=kind)
        if not canonical or not revision:
            invalid.append(
                SignalIdentityDecision(
                    item_ref=item_ref,
                    disposition=SignalDisposition.INVALID,
                    reason="No stable publication id, URL, or feed item id was available.",
                )
            )
            continue
        indexed.append((index, candidate, item_ref, canonical, revision))

    selected_index_by_canonical: dict[str, int] = {}
    for index, candidate, _item_ref, canonical, _revision in indexed:
        selected = selected_index_by_canonical.get(canonical)
        if selected is None or _revision_rank(candidate, index) > _revision_rank(
            candidates[selected], selected
        ):
            selected_index_by_canonical[canonical] = index

    decisions: list[SignalIdentityDecision] = []
    admitted: list[str] = []
    duplicates: list[str] = []
    for index, _candidate, item_ref, canonical, revision in indexed:
        if selected_index_by_canonical[canonical] != index:
            matched_work_item_id = prior_revisions.get(revision, "")
            decisions.append(
                SignalIdentityDecision(
                    item_ref=item_ref,
                    canonical_identity=canonical,
                    revision_identity=revision,
                    disposition=SignalDisposition.DUPLICATE,
                    matched_work_item_id=matched_work_item_id,
                    reason=(
                        "This exact source revision completed in the same dedupe scope."
                        if matched_work_item_id
                        else (
                            "A newer or first-ranked revision of this canonical item is "
                            "in the trigger."
                        )
                    ),
                )
            )
            duplicates.append(revision)
            continue
        if revision in prior_revisions:
            disposition = SignalDisposition.DUPLICATE
            matched_work_item_id = prior_revisions[revision]
            reason = "This exact source revision completed in the same dedupe scope."
            duplicates.append(revision)
        elif canonical in prior_canonical:
            disposition = SignalDisposition.UPDATED
            matched_work_item_id = prior_canonical[canonical]
            reason = "A new revision of a previously completed canonical source was admitted."
            admitted.append(revision)
        else:
            disposition = SignalDisposition.ADMITTED
            matched_work_item_id = ""
            reason = "This canonical source revision is new in the dedupe scope."
            admitted.append(revision)
        decisions.append(
            SignalIdentityDecision(
                item_ref=item_ref,
                canonical_identity=canonical,
                revision_identity=revision,
                disposition=disposition,
                matched_work_item_id=matched_work_item_id,
                reason=reason,
            )
        )
    decisions.extend(invalid)

    matched_work_item_ids = {
        decision.matched_work_item_id
        for decision in decisions
        if decision.disposition == SignalDisposition.DUPLICATE
        and decision.matched_work_item_id
    }
    if not admitted:
        canonical_work_item_ids = sorted(matched_work_item_ids)
        canonical_work_item_id = (
            canonical_work_item_ids[0] if len(canonical_work_item_ids) == 1 else ""
        )
        status = (
            SignalLifecycleStatus.DUPLICATE_TRIGGER
            if canonical_work_item_ids
            else SignalLifecycleStatus.NO_ACTION
        )
        checkpoint = SignalLifecycleCheckpoint(
            work_item_id=work_item.id,
            source_kind=kind,
            trigger_id=trigger_id,
            trigger_ref_digest=trigger_ref_digest,
            dedupe_scope_digest=scope_digest,
            trigger_label=_bounded_text(trigger_label, max_chars=160),
            scheduled=scheduled,
            status=status,
            next_stage=None,
            decisions=decisions,
            duplicate_revision_ids=list(dict.fromkeys(duplicates)),
            canonical_work_item_id=canonical_work_item_id,
            canonical_work_item_ids=canonical_work_item_ids,
            safe_next_action=(
                (
                    f"Reuse canonical WorkItem {canonical_work_item_id}; do not create "
                    "duplicate downstream work."
                )
                if canonical_work_item_id
                else (
                    "Reuse the listed canonical WorkItems; do not create duplicate "
                    "downstream work."
                    if canonical_work_item_ids
                    else "No stable new signal revisions were admitted; perform no work."
                )
            ),
            dry_run=dry_run,
            admission_guard=admission_guard,
        )
        if not dry_run:
            _persist_checkpoint(
                store,
                work_item,
                checkpoint,
                event_type=(
                    "signal_trigger_suppressed"
                    if status == SignalLifecycleStatus.DUPLICATE_TRIGGER
                    else "signal_trigger_no_action"
                ),
                summary=(
                    "Suppressed duplicate signal revisions in favor of canonical WorkItems."
                    if canonical_work_item_ids
                    else "Recorded an explicit no-op because no stable revision was admitted."
                ),
            )
        return {
            "status": "dry_run" if dry_run else status.value,
            "idempotent_reuse": True,
            "checkpoint": checkpoint.model_dump(mode="json"),
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }

    checkpoint = SignalLifecycleCheckpoint(
        work_item_id=work_item.id,
        source_kind=kind,
        trigger_id=trigger_id,
        trigger_ref_digest=trigger_ref_digest,
        dedupe_scope_digest=scope_digest,
        trigger_label=_bounded_text(trigger_label, max_chars=160),
        scheduled=scheduled,
        status=SignalLifecycleStatus.PLANNED if dry_run else SignalLifecycleStatus.READY,
        decisions=decisions,
        admitted_revision_ids=list(dict.fromkeys(admitted)),
        duplicate_revision_ids=list(dict.fromkeys(duplicates)),
        safe_next_action="Retrieve context for admitted revisions, then checkpoint the handoff.",
        dry_run=dry_run,
        admission_guard=admission_guard,
    )
    if not dry_run:
        _persist_checkpoint(
            store,
            work_item,
            checkpoint,
            event_type="signal_trigger_checkpointed",
            summary="Checkpointed a bounded signal trigger and its deduplication decisions.",
        )
    return {
        "status": "dry_run" if dry_run else "checkpointed",
        "idempotent_reuse": False,
        "checkpoint": checkpoint.model_dump(mode="json"),
        "provider_calls_made": 0,
        "external_writes_performed": False,
    }


def inspect_signal_lifecycle_impl(
    *,
    work_item_id: str = "",
    source_kind: Literal["", "rss", "preprints"] = "",
    trigger_id: str = "",
    database_url: str | None = None,
) -> dict[str, Any]:
    """Read one exact WorkItem checkpoint or the latest checkpoint for a source."""

    store = SQLiteStore(database_url or database_url_from_env())
    clean_work_item_id = _bounded_text(work_item_id, max_chars=200)
    clean_source_kind = _bounded_text(source_kind, max_chars=20)
    resolution = "exact_work_item"
    if clean_work_item_id:
        work_item = store.get_work_item(clean_work_item_id)
    elif clean_source_kind:
        route = {
            SignalSourceKind.RSS.value: "rss_context_agent",
            SignalSourceKind.PREPRINTS.value: "preprints_context_agent",
        }[SignalSourceKind(clean_source_kind).value]
        work_item = next(
            (
                item
                for item in store.list_work_items(current_route=route, limit=50)
                if _checkpoints(item)
            ),
            None,
        )
        resolution = "latest_source_checkpoint"
    else:
        return {
            "status": "invalid_scope",
            "reason": "work_item_id_or_source_kind_required",
            "work_item_id": "",
            "source_kind": "",
            "checkpoints": [],
            "checkpoint_count": 0,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }
    if work_item is None:
        return {
            "status": "no_checkpoint" if clean_source_kind else "not_found",
            "reason": (
                "no_saved_checkpoint_for_source"
                if clean_source_kind
                else "work_item_not_found"
            ),
            "resolution": resolution,
            "work_item_id": clean_work_item_id,
            "source_kind": clean_source_kind,
            "checkpoints": [],
            "checkpoint_count": 0,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }
    checkpoints = _checkpoints(work_item)
    clean_trigger_id = _bounded_text(trigger_id, max_chars=100)
    if clean_trigger_id:
        checkpoints = [item for item in checkpoints if item.trigger_id == clean_trigger_id]
    return {
        "status": "success",
        "resolution": resolution,
        "work_item_id": work_item.id,
        "source_kind": (
            checkpoints[-1].source_kind.value if checkpoints else clean_source_kind
        ),
        "checkpoints": [item.model_dump(mode="json") for item in checkpoints[-5:]],
        "checkpoint_count": len(checkpoints),
        "provider_calls_made": 0,
        "external_writes_performed": False,
    }


def advance_signal_lifecycle_checkpoint_impl(
    *, work_item_id: str, trigger_id: str, completed_stage: SignalLifecycleStage | str,
    outcome: Literal["completed", "failed"] = "completed", failure_code: str = "",
    failure_summary: str = "", provider_calls_made: int = 0, dry_run: bool = True,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Serialize each expected stage transition across cooperative local workers."""
    store = SQLiteStore(database_url or database_url_from_env())
    with (
        work_item_execution_lock(database_url or database_url_from_env(), work_item_id),
        _signal_admission_guard(store),
    ):
        return _advance_signal_lifecycle_checkpoint_locked(
            work_item_id=work_item_id, trigger_id=trigger_id, completed_stage=completed_stage,
            outcome=outcome, failure_code=failure_code, failure_summary=failure_summary,
            provider_calls_made=provider_calls_made, dry_run=dry_run, database_url=database_url,
        )


def _advance_signal_lifecycle_checkpoint_locked(
    *,
    work_item_id: str,
    trigger_id: str,
    completed_stage: SignalLifecycleStage | str,
    outcome: Literal["completed", "failed"] = "completed",
    failure_code: str = "",
    failure_summary: str = "",
    provider_calls_made: int = 0,
    dry_run: bool = True,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Advance exactly one expected stage, or retain it as the resume point on failure."""

    store = SQLiteStore(database_url or database_url_from_env())
    work_item = store.get_work_item(_bounded_text(work_item_id, max_chars=200))
    if work_item is None:
        return {
            "status": "blocked",
            "reason": "work_item_not_found",
            "dry_run": dry_run,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }
    clean_trigger_id = _bounded_text(trigger_id, max_chars=100)
    checkpoint = next(
        (
            item
            for item in reversed(_checkpoints(work_item))
            if item.trigger_id == clean_trigger_id
        ),
        None,
    )
    if checkpoint is None:
        return {
            "status": "blocked",
            "reason": "signal_checkpoint_not_found",
            "work_item_id": work_item.id,
            "trigger_id": clean_trigger_id,
            "dry_run": dry_run,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }
    expected_route = f"{checkpoint.source_kind.value}_context_agent"
    if checkpoint.work_item_id != work_item.id or work_item.current_route.value != expected_route:
        return {
            "status": "blocked",
            "reason": "signal_checkpoint_kind_does_not_match_work_item_route",
            "work_item_id": work_item.id,
            "checkpoint_work_item_id": checkpoint.work_item_id,
            "checkpoint_source_kind": checkpoint.source_kind.value,
            "route": work_item.current_route.value,
            "dry_run": dry_run,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }
    if checkpoint.status in {
        SignalLifecycleStatus.COMPLETED,
        SignalLifecycleStatus.DUPLICATE_TRIGGER,
        SignalLifecycleStatus.NO_ACTION,
    }:
        return {
            "status": checkpoint.status.value,
            "idempotent_reuse": True,
            "checkpoint": checkpoint.model_dump(mode="json"),
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }

    stage = SignalLifecycleStage(completed_stage)
    if checkpoint.next_stage != stage:
        return {
            "status": "blocked",
            "reason": "out_of_order_signal_lifecycle_stage",
            "expected_stage": checkpoint.next_stage.value if checkpoint.next_stage else "",
            "received_stage": stage.value,
            "checkpoint": checkpoint.model_dump(mode="json"),
            "dry_run": dry_run,
            "provider_calls_made": 0,
            "external_writes_performed": False,
        }

    call_count = max(0, min(100, int(provider_calls_made)))
    if outcome == "failed":
        updated = checkpoint.model_copy(
            update={
                "status": SignalLifecycleStatus.PARTIAL_FAILURE,
                "failed_stage": stage.value,
                "failure_code": _bounded_text(failure_code or "stage_failed", max_chars=120),
                "failure_summary": _bounded_text(failure_summary, max_chars=600),
                "safe_next_action": f"Retry the same WorkItem at {stage.value}.",
                "attempt_count": min(100, checkpoint.attempt_count + 1),
                "resumed": True,
                "dry_run": dry_run,
                "provider_calls_made": checkpoint.provider_calls_made + call_count,
                "updated_at": utc_now_iso(),
            }
        )
        event_type = "signal_lifecycle_stage_failed"
        summary = f"Checkpointed a recoverable failure at {stage.value}."
    else:
        completed = list(dict.fromkeys([*checkpoint.completed_stages, stage]))
        stage_index = SIGNAL_LIFECYCLE_STAGE_ORDER.index(stage)
        next_stage = (
            SIGNAL_LIFECYCLE_STAGE_ORDER[stage_index + 1]
            if stage_index + 1 < len(SIGNAL_LIFECYCLE_STAGE_ORDER)
            else None
        )
        updated = checkpoint.model_copy(
            update={
                "status": (
                    SignalLifecycleStatus.COMPLETED
                    if stage == SignalLifecycleStage.COMPLETED
                    else SignalLifecycleStatus.READY
                ),
                "completed_stages": completed,
                "next_stage": next_stage,
                "failed_stage": "",
                "failure_code": "",
                "failure_summary": "",
                "safe_next_action": (
                    f"Continue at {next_stage.value}." if next_stage else ""
                ),
                "dry_run": dry_run,
                "provider_calls_made": checkpoint.provider_calls_made + call_count,
                "updated_at": utc_now_iso(),
            }
        )
        event_type = "signal_lifecycle_stage_completed"
        summary = f"Completed signal lifecycle stage {stage.value}."
    if not dry_run:
        _persist_checkpoint(
            store,
            work_item,
            updated,
            event_type=event_type,
            summary=summary,
        )
    return {
        "status": "dry_run" if dry_run else updated.status.value,
        "checkpoint": updated.model_dump(mode="json"),
        "provider_calls_made": call_count,
        "external_writes_performed": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def prepare_signal_lifecycle_checkpoint(
    work_item_id: str,
    source_kind: Literal["rss", "preprints"],
    trigger_ref: str,
    dedupe_scope: str,
    candidate_items_json: str,
    trigger_label: str = "",
    scheduled: bool = False,
    dry_run: bool = True,
) -> str:
    """Plan or persist one WorkItem-owned signal trigger and dedup checkpoint."""

    return json.dumps(
        prepare_signal_lifecycle_checkpoint_impl(
            work_item_id=work_item_id,
            source_kind=source_kind,
            trigger_ref=trigger_ref,
            dedupe_scope=dedupe_scope,
            candidate_items_json=candidate_items_json,
            trigger_label=trigger_label,
            scheduled=scheduled,
            dry_run=dry_run,
        ),
        ensure_ascii=True,
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def inspect_signal_lifecycle(
    work_item_id: str = "",
    source_kind: Literal["", "rss", "preprints"] = "",
    trigger_id: str = "",
) -> str:
    """Inspect an exact WorkItem or latest source checkpoint without modifying it."""

    return json.dumps(
        inspect_signal_lifecycle_impl(
            work_item_id=work_item_id,
            source_kind=source_kind,
            trigger_id=trigger_id,
        ),
        ensure_ascii=True,
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def advance_signal_lifecycle_checkpoint(
    work_item_id: str,
    trigger_id: str,
    completed_stage: Literal[
        "context_retrieved", "handoff_prepared", "review_completed", "completed"
    ],
    outcome: Literal["completed", "failed"] = "completed",
    failure_code: str = "",
    failure_summary: str = "",
    provider_calls_made: int = 0,
    dry_run: bool = True,
) -> str:
    """Advance one expected signal stage or persist its exact resume point."""

    return json.dumps(
        advance_signal_lifecycle_checkpoint_impl(
            work_item_id=work_item_id,
            trigger_id=trigger_id,
            completed_stage=completed_stage,
            outcome=outcome,
            failure_code=failure_code,
            failure_summary=failure_summary,
            provider_calls_made=provider_calls_made,
            dry_run=dry_run,
        ),
        ensure_ascii=True,
        sort_keys=True,
    )


__all__ = [
    "advance_signal_lifecycle_checkpoint",
    "advance_signal_lifecycle_checkpoint_impl",
    "inspect_signal_lifecycle",
    "inspect_signal_lifecycle_impl",
    "prepare_signal_lifecycle_checkpoint",
    "prepare_signal_lifecycle_checkpoint_impl",
    "signal_item_identity",
    "signal_trigger_id",
]
