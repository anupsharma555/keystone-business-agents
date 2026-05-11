"""Local opportunity pipeline state helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from keystone_agents.opportunity_scout.scoring import normalize_type
from keystone_agents.schemas.opportunity import ExistingOpportunityState

SKIP_EXISTING_STATUSES = {"approved", "approved_for_drafting", "drafted", "rejected", "archived"}
UPDATE_EXISTING_STATUSES = {"candidate", "researched"}


def normalize_company_key(company_name: str) -> str:
    """Normalize a company name for duplicate detection."""

    text = company_name.lower()
    text = re.sub(r"\b(inc|llc|ltd|corp|corporation|company|co)\b", "", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text or company_name.lower().strip()


def normalize_pipeline_status(value: str) -> str:
    """Normalize and validate local opportunity pipeline status."""

    status = value.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "approved_for_outreach": "approved",
        "approved_for_draft": "approved_for_drafting",
        "approval_ready": "approved",
        "done": "drafted",
        "discarded": "rejected",
        "inactive": "archived",
    }
    status = aliases.get(status, status)
    allowed = SKIP_EXISTING_STATUSES | UPDATE_EXISTING_STATUSES
    if status not in allowed:
        raise ValueError(
            f"Opportunity pipeline status must be one of: {', '.join(sorted(allowed))}."
        )
    return status


def state_items_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Return state records from a supported state payload shape."""

    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("states", "records", "opportunities", "items"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
        return [payload]
    raise ValueError("Existing opportunity state must be JSON object/list or local path.")


def load_existing_state_payload(existing_state: Any) -> Any:
    """Load state JSON from a payload, JSON string, or local path."""

    if existing_state is None:
        return None
    if isinstance(existing_state, Path):
        return json.loads(existing_state.read_text(encoding="utf-8"))
    if isinstance(existing_state, str):
        text = existing_state.strip()
        if not text:
            return None
        path = Path(text)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return json.loads(text)
    return existing_state


def load_existing_state_map(existing_state: Any = None) -> dict[str, ExistingOpportunityState]:
    """Load existing opportunity state keyed by normalized company name."""

    payload = load_existing_state_payload(existing_state)
    states: dict[str, ExistingOpportunityState] = {}
    for item in state_items_from_payload(payload):
        company_name = str(
            item.get("company_name")
            or item.get("target_company")
            or item.get("company")
            or item.get("name")
            or ""
        ).strip()
        if not company_name:
            continue
        status = normalize_pipeline_status(
            str(item.get("status") or item.get("state") or "candidate")
        )
        normalized = normalize_company_key(company_name)
        states[normalized] = ExistingOpportunityState(
            company_name=company_name,
            status=status,  # type: ignore[arg-type]
            opportunity_type=(
                normalize_type(str(item["opportunity_type"]))
                if item.get("opportunity_type")
                else None
            ),
            notes=str(item.get("notes") or item.get("reason") or ""),
            last_seen=str(item.get("last_seen") or item.get("updated_at") or "") or None,
            source=str(item.get("source") or "local_pipeline"),
            normalized_company_key=normalized,
        )
    return states
