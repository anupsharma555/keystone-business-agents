"""Privacy-safe correlation identities for bounded validation executions."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class ValidationExecutionIdentity:
    """Local receipt and SDK-trace correlation fields for one validation run."""

    scenario: str
    route: str
    run_id: str
    case_id: str
    created_at_utc: str

    def trace_metadata(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "route": self.route,
            "workflow_kind": self.scenario,
        }

    def receipt(self) -> dict[str, str]:
        return asdict(self)


def create_validation_execution_identity(
    *,
    scenario: str,
    route: str,
    now: datetime | None = None,
    nonce: str | None = None,
) -> ValidationExecutionIdentity:
    """Create a bounded identity containing no prompt, provider, or user content."""

    safe_scenario = _safe_label(scenario, field_name="scenario")
    safe_route = _safe_label(route, field_name="route")
    created = (now or datetime.now(UTC)).astimezone(UTC)
    safe_nonce = _safe_nonce(nonce or uuid.uuid4().hex[:8])
    run_id = (
        f"kba_{safe_scenario[:28]}_{created.strftime('%Y%m%dT%H%M%SZ')}_{safe_nonce}"
    )
    return ValidationExecutionIdentity(
        scenario=safe_scenario,
        route=safe_route,
        run_id=run_id,
        case_id=f"validation_{safe_scenario}",
        created_at_utc=created.isoformat().replace("+00:00", "Z"),
    )


def _safe_label(value: str, *, field_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if not normalized or len(normalized) > 64:
        raise ValueError(f"{field_name} must normalize to 1-64 safe characters.")
    return normalized


def _safe_nonce(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{8}", normalized):
        raise ValueError("nonce must contain exactly eight lowercase hexadecimal characters.")
    return normalized
