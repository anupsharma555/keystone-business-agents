"""Fixture and context helpers for Business Research Analyst."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from keystone_agents.schemas.contact_context import ContactRecord, CRMAccountContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"


def resolve_fixture_path(fixture: str | Path, *, default_suffix: str = ".json") -> Path:
    """Resolve a test fixture by explicit path, stem, or fixture filename."""

    raw_path = Path(fixture)
    candidates = [raw_path]
    if raw_path.suffix == "":
        candidates.append(raw_path.with_suffix(default_suffix))
        candidates.append(FIXTURE_ROOT / f"{raw_path.name}{default_suffix}")
    else:
        candidates.append(FIXTURE_ROOT / raw_path.name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Fixture not found: {fixture}")


def read_fixture_json(fixture: str | Path) -> dict[str, Any]:
    """Read a JSON fixture object."""

    return json.loads(resolve_fixture_path(fixture).read_text(encoding="utf-8"))


def coerce_contact_context(value: ContactRecord | dict[str, Any] | None) -> ContactRecord | None:
    """Coerce approved local contact context."""

    if value is None:
        return None
    if isinstance(value, ContactRecord):
        return value
    return ContactRecord.model_validate(value)


def coerce_crm_context(
    value: CRMAccountContext | dict[str, Any] | None,
) -> CRMAccountContext | None:
    """Coerce approved local CRM context."""

    if value is None:
        return None
    if isinstance(value, CRMAccountContext):
        return value
    return CRMAccountContext.model_validate(value)
