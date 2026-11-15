"""Host-owned admission for optional compact Slack continuity.

Only current transport identity and its matching bounded channel capability can
admit this policy. Stored WorkItem metadata and model tool arguments are not
scope authorities. The policy never changes provider-cache or source-read rules.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

ENABLED_ENV = "KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED"
CHANNEL_IDS_ENV = "KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS"
TEAM_IDS_ENV = "KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS"
RAW_HISTORY_OMITTED_REASON = "typed_context_replaces_raw_history"


@dataclass(frozen=True)
class SlackCompactContinuityPolicy:
    eligible: bool = False
    reason: str = "feature_disabled"
    team_id: str = field(default="", repr=False)
    channel_id: str = field(default="", repr=False)
    user_id: str = field(default="", repr=False)
    thread_ts: str = field(default="", repr=False)
    request_ts: str = field(default="", repr=False)

    def metadata(self) -> dict[str, Any]:
        return {
            "schema": "keystone.compact_continuity_admission.v1",
            "eligible": self.eligible,
            "raw_sdk_history": "omitted" if self.eligible else "existing_policy",
            "reason": self.reason,
        }


_CURRENT_POLICY: ContextVar[SlackCompactContinuityPolicy | None] = ContextVar(
    "keystone_slack_compact_continuity_policy", default=None,
)


def current_continuity_policy() -> SlackCompactContinuityPolicy | None:
    return _CURRENT_POLICY.get()


def compact_continuity_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(ENABLED_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def activate_continuity_policy(
    policy: SlackCompactContinuityPolicy,
) -> Iterator[SlackCompactContinuityPolicy]:
    # A nested host entry has its own current identity. Never reuse a parent's
    # eligible memory scope as authority for a different or incomplete entry.
    token = _CURRENT_POLICY.set(policy)
    try:
        yield policy
    finally:
        _CURRENT_POLICY.reset(token)


def resolve_slack_compact_continuity_policy(
    host_context: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> SlackCompactContinuityPolicy:
    """Validate current host transport scope; never scan prose or saved task data."""

    env = os.environ if environ is None else environ
    if not compact_continuity_enabled(env):
        return SlackCompactContinuityPolicy()
    if environ is None:
        from keystone_agents.context_env import context_env_value

        configured_teams = context_env_value(TEAM_IDS_ENV)
    else:
        configured_teams = str(env.get(TEAM_IDS_ENV, ""))
    workspace_mode = bool(configured_teams.strip())
    if workspace_mode:
        teams = tuple(dict.fromkeys(re.split(r"[,\s]+", configured_teams.strip())))
        if (len(configured_teams) > 2048 or len(teams) > 16
                or any(not re.fullmatch(r"T[A-Z0-9]{2,30}", value) for value in teams)):
            return SlackCompactContinuityPolicy(reason="invalid_team_configuration")
    else:
        configured_channels = str(env.get(CHANNEL_IDS_ENV, ""))
        channels = tuple(dict.fromkeys(re.split(r"[,\s]+", configured_channels.strip())))
        if (
            not configured_channels.strip() or len(configured_channels) > 2048
            or len(channels) > 16
            or any(not re.fullmatch(r"C[A-Z0-9]{2,30}", value) for value in channels)
        ):
            return SlackCompactContinuityPolicy(reason="invalid_channel_configuration")
    context = host_context if isinstance(host_context, Mapping) else {}
    identity = context.get("slack_scope") or context.get("slack_context") or context
    if not isinstance(identity, Mapping):
        return SlackCompactContinuityPolicy(reason="slack_identity_incomplete")
    channel = str(identity.get("channel_id") or "")
    team = str(identity.get("team_id") or "")
    if workspace_mode:
        if team not in teams:
            return SlackCompactContinuityPolicy(reason="team_not_configured")
        if not re.fullmatch(r"[CG][A-Z0-9]{2,30}", channel):
            return SlackCompactContinuityPolicy(reason="channel_identity_invalid")
    elif channel not in channels:
        return SlackCompactContinuityPolicy(reason="channel_not_configured")
    user = str(identity.get("user_id") or "")
    thread = str(identity.get("thread_ts") or "")
    request = str(identity.get("request_ts") or "")
    if not (
        re.fullmatch(r"T[A-Z0-9]{2,30}", team)
        and re.fullmatch(r"[UW][A-Z0-9]{2,30}", user)
        and re.fullmatch(r"\d{1,12}(?:\.\d{1,6})?", thread)
        and re.fullmatch(r"\d{1,12}(?:\.\d{1,6})?", request)
    ):
        return SlackCompactContinuityPolicy(reason="slack_identity_incomplete")
    try:
        if not Decimal(0) < Decimal(thread) <= Decimal(request):
            return SlackCompactContinuityPolicy(reason="slack_identity_incomplete")
    except InvalidOperation:
        return SlackCompactContinuityPolicy(reason="slack_identity_incomplete")
    descriptor = context.get("channel_context_capability")
    if not isinstance(descriptor, Mapping):
        return SlackCompactContinuityPolicy(reason="channel_capability_missing")
    from keystone_agents.tools.slack_channel_context_tools import SlackChannelContextCapability

    try:
        bound = SlackChannelContextCapability(descriptor).descriptor
    except (TypeError, ValueError):
        return SlackCompactContinuityPolicy(reason="channel_capability_invalid")
    if bound["channel_id"] != channel or bound["request_ts"] != request:
        return SlackCompactContinuityPolicy(reason="channel_capability_scope_mismatch")
    if workspace_mode and bound.get("team_id") != team:
        return SlackCompactContinuityPolicy(reason="channel_capability_scope_mismatch")
    return SlackCompactContinuityPolicy(
        eligible=True, reason=RAW_HISTORY_OMITTED_REASON,
        team_id=team, channel_id=channel, user_id=user, thread_ts=thread, request_ts=request,
    )


def continuity_policy_from_specialist_environment() -> SlackCompactContinuityPolicy:
    """Recover only the existing host-authored child execution-context envelope."""

    if not compact_continuity_enabled():
        return SlackCompactContinuityPolicy()
    from keystone_agents.orchestrator.preflight_context import (
        load_specialist_execution_context_from_env,
    )

    return resolve_slack_compact_continuity_policy(load_specialist_execution_context_from_env())
