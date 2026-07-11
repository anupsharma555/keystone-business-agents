"""Approve one exact aggregate email style profile for drafting only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.tools.email_style_tool import load_email_style_profile_from_storage
from keystone_agents.tools.storage_tool import StorageTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--reviewer", default="Anup")
    parser.add_argument(
        "--notes",
        default="Approved aggregate SENT style for draft-only use; no send permission.",
    )
    parser.add_argument("--output", type=Path)
    return parser


def approve_profile(
    *, profile_id: str, database_url: str, reviewer: str, notes: str
) -> dict[str, object]:
    storage = StorageTool(database_url)
    profiles = storage.list_email_style_profiles(profile_id=profile_id, approved_only=False)
    if not profiles:
        raise RuntimeError(f"Email style profile not found: {profile_id}")
    profile = EmailStyleProfile.model_validate(profiles[-1])
    if profile.source != "live_gmail_sent":
        raise RuntimeError("Only a live Gmail SENT aggregate profile may use this approval path.")
    if profile.raw_sent_email_bodies_included:
        raise RuntimeError("Style profile approval blocked because raw sent bodies are present.")
    if profile.send_enabled or profile.sent:
        raise RuntimeError("Style profile approval cannot enable or record sending.")
    if not any(signoff.lower().startswith("sincerely") for signoff in profile.signoffs):
        raise RuntimeError("Style profile approval requires the verified Sincerely signoff.")

    approved = profile.model_copy(
        update={
            "approval_state": ApprovalState.APPROVED_FOR_DRAFTING,
            "approval_scope": ApprovalScope.DRAFTING,
            "send_enabled": False,
            "sent": False,
        }
    )
    saved = storage.save_email_style_profile(approved)
    approval = storage.save_approval(
        object_type="email_style_profile",
        object_id=profile_id,
        decision=ApprovalState.APPROVED_FOR_DRAFTING,
        scope=ApprovalScope.DRAFTING,
        reviewer=reviewer,
        notes=notes,
        source_agent="human_review",
    )
    loaded = load_email_style_profile_from_storage(profile_id, database_url=database_url)
    verified = bool(
        loaded is not None
        and loaded.approved_for_drafting
        and loaded.raw_sent_email_bodies_included is False
        and loaded.send_enabled is False
        and loaded.sent is False
    )
    if not verified:
        raise RuntimeError("Approved style profile failed local read-back verification.")
    return {
        "status": "pass",
        "profile_id": profile_id,
        "approval_state": approved.approval_state.value,
        "approval_scope": approved.approval_scope.value,
        "signoffs": approved.signoffs,
        "raw_sent_email_bodies_included": False,
        "send_enabled": False,
        "sent": False,
        "storage": {"profile": saved, "approval": approval},
        "verified": verified,
    }


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    payload = approve_profile(
        profile_id=args.profile_id,
        database_url=args.database_url,
        reviewer=args.reviewer,
        notes=args.notes,
    )
    if args.output is not None:
        _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
