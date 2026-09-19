"""Opt-in Slack acceptance policy; one root by default, one reviewed follow-up optionally."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keystone_agents.canary_runtime import MUTATION_DISABLED_ENV, validate_canary_state_dir
from keystone_agents.execution_request import latest_slack_operator_request
from keystone_agents.model_provider import (
    CANARY_ACCEPTANCE_OPENAI_MODEL,
    CANARY_LUNA_MODEL,
    CANARY_TERRA_MODEL,
)

PROFILE_ENV = "KEYSTONE_CANARY_ACCEPTANCE_PROFILE"
DIGEST_ENV = "KEYSTONE_CANARY_ACCEPTANCE_DIGEST"
CLAIM_ENV = "KEYSTONE_CANARY_ACCEPTANCE_CLAIM"
GMAIL_READ_ONLY_ENV = "KEYSTONE_CANARY_GMAIL_READ_ONLY"
TURN_REQUEST_LIMIT_ENV = "KEYSTONE_CANARY_TURN_MAX_REQUESTS"
MODEL = CANARY_ACCEPTANCE_OPENAI_MODEL
_AGENT = ContextVar("keystone_canary_acceptance_agent", default="")
_LEASE = ContextVar("keystone_canary_acceptance_lease", default="")
_INSTRUCTION_REPAIR_AGENT = "instruction_following_repair"
_TOOL_FREE_AGENTS = frozenset({_INSTRUCTION_REPAIR_AGENT})
_MINI_ONLY_AGENTS = frozenset({_INSTRUCTION_REPAIR_AGENT})
_LEGACY_SCENARIO = "gmail_read_only"
_PUBLIC_PREPRINT_SCENARIO = "local_public_preprints"
_PUBLIC_PREPRINT_HISTORY_TOOL = "retrieve_preprint_announcement_history"
_PUBLIC_PREPRINT_MIN_CANDIDATES = 1
_PUBLIC_PREPRINT_MAX_CANDIDATES = 25
_PUBLIC_PREPRINT_ROOT_REQUEST_CEILING = 5
_PUBLIC_PREPRINT_FOLLOWUP_REQUEST_CEILING = 2
_PUBLIC_PREPRINT_AGGREGATE_REQUEST_CEILING = 7
_FOLLOWUP_RENEWAL_FIELDS = frozenset(
    {
        "approval_reference",
        "approved_reserve_usd",
        "balance_observed_at",
        "followup_request_sha256",
        "observed_balance_usd",
    }
)
_FOLLOWUP_RENEWAL_REVIEW_FIELDS = frozenset(
    {
        "schema",
        "status",
        "usage_reconciled",
        "observed_balance_usd",
        "approved_reserve_usd",
        "balance_observed_at",
        "open_hold_usd",
        "approval_reference",
        "evidence_reference",
        "rationale",
        "original_runtime_source_sha256",
        "replacement_runtime_source_sha256",
    }
)
_PUBLIC_PREPRINT_UNBOUND_ROOT_KEYS = frozenset(
    {
        "schema",
        "source",
        "team_id",
        "user_id",
        "channel_id",
        "thread_ts",
        "request_ts",
        "request_text",
        "read_context",
        "thread_root_request",
        "thread_messages",
        "thread_fetch_status",
        "warnings",
    }
)
_SLACK_TIMESTAMP_RE = re.compile(r"\d{10,}\.[0-9]{6}")
_AGENTS = frozenset(
    {
        "orchestrator",
        "chief_of_staff",
        "gmail_triage",
        "gmail_triage_selection_repair",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "web_query_planner",
        _INSTRUCTION_REPAIR_AGENT,
    }
)
_PUBLIC_PREPRINT_AGENTS = frozenset(
    {
        "orchestrator",
        "chief_of_staff",
        "preprints_context_agent",
        "opportunity_scout",
        _INSTRUCTION_REPAIR_AGENT,
    }
)
_ROOT = Path(__file__).resolve().parents[2]
_OMIT = object()


class CanaryPolicyError(RuntimeError):
    """A fixed, content-free code for a rejected acceptance action."""

    def __init__(self, message: str, *, measurements: dict[str, int] | None = None):
        super().__init__(message)
        self.measurements = dict(measurements or {})


def _hash(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canary_followup_request(value: str) -> str:
    """Read the approved turn from the bounded Slack envelope, never its prior result.

    KS may truncate context.request_text before the repeated trailing follow-up,
    while the actual CLI argument stays complete. Its leading authoritative
    field survives that truncation. When both fields exist they must agree.
    """
    raw = str(value or "").strip()
    current = latest_slack_operator_request(raw)
    if "continue this prior slack thread" not in raw.lower():
        return current
    headings = re.findall(
        r"^Current user request \(authoritative\):[^\S\n]*(.*?)"
        r"(?=^(?:Linked WorkItem:|Prior task owner \(advisory\):|Provider affinity:|"
        r"Previous request:|Previous result title:|Previous result:|User follow-up:|"
        r"Continue the same agent task\b)|\Z)",
        raw, flags=re.I | re.M | re.S,
    )
    if not headings:
        return current
    if len(headings) != 1:
        raise CanaryPolicyError("canary_followup_authority_conflict")
    authoritative = latest_slack_operator_request(headings[0].strip())
    if re.search(r"^User follow-up:", raw, flags=re.I | re.M) and current != authoritative:
        raise CanaryPolicyError("canary_followup_authority_conflict")
    return authoritative


class AcceptanceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.slack_acceptance_profile.v1"] = (
        "keystone.slack_acceptance_profile.v1"
    )
    scenario: Literal["gmail_read_only", "local_public_preprints"] = Field(
        default=_LEGACY_SCENARIO,
        exclude_if=lambda value: value == _LEGACY_SCENARIO,
    )
    enabled: bool = False
    repo_root: Path
    state_dir: Path
    request_sha256: str = ""
    context_sha256: str = ""
    channel_id: str = ""
    team_id: str = ""
    author_id: str = ""
    gmail_token_copy: Path | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    gmail_credentials_file: Path | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    key_env_file: Path
    public_preprint_snapshot: Path | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    public_preprint_snapshot_sha256: str = Field(
        default="", exclude_if=lambda value: not value
    )
    approval_reference: str = ""
    max_requests: int = Field(default=8, ge=1, le=12)
    root_max_requests: int | None = Field(
        default=None, ge=1, le=12, strict=True, exclude_if=lambda value: value is None
    )
    followup_max_requests: int | None = Field(
        default=None, ge=1, le=12, strict=True, exclude_if=lambda value: value is None
    )
    model_profile: Literal["mini", "terra_luna", "terra_mini"] = "mini"
    max_structured_output_retries: int = Field(default=0, ge=0, le=1, strict=True)
    expanded_budget_reviewed: bool = False
    max_input_bytes: int = Field(default=240_000, ge=1, le=300_000)
    max_body_bytes: int = Field(default=300_000, ge=1, le=336_000)
    max_output_tokens: int = Field(default=12000, ge=1, le=12000)
    reserved_input_tokens: int = Field(default=400_000, ge=400_000, le=400_000)
    observed_balance_usd: Decimal | None = None
    approved_reserve_usd: Decimal | None = None
    balance_observed_at: datetime | None = None
    balance_floor_usd: Decimal = Field(default=Decimal("5"), ge=Decimal("5"))
    followup_request_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> AcceptanceProfile:
        if self.scenario == _LEGACY_SCENARIO:
            if self.gmail_token_copy is None or self.gmail_credentials_file is None:
                raise ValueError("Legacy Gmail acceptance requires isolated Gmail files.")
            if self.public_preprint_snapshot is not None or self.public_preprint_snapshot_sha256:
                raise ValueError("Legacy Gmail acceptance cannot bind a public preprint source.")
        else:
            if self.gmail_token_copy is not None or self.gmail_credentials_file is not None:
                raise ValueError("Public preprint acceptance cannot include Gmail credentials.")
            if self.public_preprint_snapshot is None or not _is_sha256(
                self.public_preprint_snapshot_sha256
            ):
                raise ValueError(
                    "Public preprint acceptance requires one SHA-256-bound source snapshot."
                )
            if (
                self.model_profile != "mini"
                or self.root_max_requests is None
                or not 1
                <= self.root_max_requests
                <= _PUBLIC_PREPRINT_ROOT_REQUEST_CEILING
                or self.followup_max_requests is None
                or not 1
                <= self.followup_max_requests
                <= _PUBLIC_PREPRINT_FOLLOWUP_REQUEST_CEILING
                or self.max_requests != self.root_max_requests + self.followup_max_requests
                or self.max_requests > _PUBLIC_PREPRINT_AGGREGATE_REQUEST_CEILING
                or not self.followup_request_sha256
                or self.max_output_tokens != 3_000
                or self.max_input_bytes != 130_000
                or self.max_body_bytes != 140_000
                or self.max_structured_output_retries != 0
            ):
                raise ValueError(
                    "Public preprint acceptance requires explicit nonempty request "
                    "allocations within the reviewed mini 5/2/7 ceilings, an aggregate "
                    "equal to the two turn allocations, and 3000/130000/140000 "
                    "serialization limits."
                )
            if not self.author_id:
                raise ValueError("Public preprint acceptance requires an exact author.")
        if self.max_requests > 8 and not self.expanded_budget_reviewed:
            raise ValueError("Expanded request budget requires explicit profile review.")
        if self.followup_request_sha256:
            if (self.root_max_requests is None) != (self.followup_max_requests is None):
                raise ValueError(
                    "Reviewed two-turn request limits require both root and follow-up maxima."
                )
        elif self.followup_max_requests is not None:
            raise ValueError("A follow-up request limit requires an approved follow-up.")
        for turn_limit in (self.root_max_requests, self.followup_max_requests):
            if turn_limit is not None and turn_limit > self.max_requests:
                raise ValueError("A per-turn request maximum exceeds the shared budget.")
        if (
            self.root_max_requests is not None
            and self.followup_max_requests is not None
            and self.root_max_requests + self.followup_max_requests > self.max_requests
        ):
            raise ValueError("Per-turn request maxima exceed the shared request budget.")
        if self.max_input_bytes > self.max_body_bytes:
            raise ValueError("Input bound exceeds complete request bound.")
        if self.followup_request_sha256 and self.model_profile != "mini":
            raise ValueError("Two-turn acceptance currently requires the all-mini budget profile.")
        if self.enabled:
            for digest in (
                self.request_sha256,
                *([self.context_sha256] if self.context_sha256 else []),
                *([self.followup_request_sha256] if self.followup_request_sha256 else []),
            ):
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ValueError(
                        "Enabled profile requires exact reviewed request/context digests."
                    )
            if not self.channel_id or not self.team_id or not self.approval_reference:
                raise ValueError(
                    "Enabled profile requires reviewed channel/team and approval reference."
                )
            for value in (self.observed_balance_usd, self.approved_reserve_usd):
                if value is None or not value.is_finite() or value < 0:
                    raise ValueError(
                        "Enabled profile requires finite reviewed balance and reserve."
                    )
            if self.approved_reserve_usd < self.required_reserve_usd:
                raise ValueError("Reviewed reserve is below the conservative model reservation.")
            if self.observed_balance_usd - self.required_reserve_usd < self.balance_floor_usd:
                raise ValueError("Model reservation would cross the reviewed balance floor.")
            if self.balance_observed_at is None or self.balance_observed_at.tzinfo is None:
                raise ValueError("A timezone-aware balance observation is required.")
        return self

    def model_for_agent(self, agent: str) -> str:
        if self.model_profile == "mini":
            return MODEL
        return (CANARY_TERRA_MODEL if agent == "orchestrator" else
                MODEL if self.model_profile == "terra_mini" else CANARY_LUNA_MODEL)

    @property
    def is_public_preprint_scenario(self) -> bool:
        return self.scenario == _PUBLIC_PREPRINT_SCENARIO

    @property
    def allowed_agents(self) -> frozenset[str]:
        return _PUBLIC_PREPRINT_AGENTS if self.is_public_preprint_scenario else _AGENTS

    def allowed_function_tools(self, agent: str) -> frozenset[str]:
        if not self.is_public_preprint_scenario:
            return frozenset()
        if agent == "preprints_context_agent":
            return frozenset({_PUBLIC_PREPRINT_HISTORY_TOOL})
        return frozenset()

    @property
    def required_reserve_usd(self) -> Decimal:
        if self.model_profile in {"terra_luna", "terra_mini"}:
            # At most one Terra planning response; all remaining responses are Luna.
            # Reserve long-context rates for the existing 400k token allowance.
            # Explicit-only caching with no breakpoints is enforced before dispatch.
            tokens = Decimal(self.reserved_input_tokens)
            output = Decimal(self.max_output_tokens)
            terra = tokens * Decimal("4") + output * Decimal("18")
            luna = (tokens * Decimal("0.75") + output * Decimal("4.50")
                    if self.model_profile == "terra_mini" else
                    tokens * Decimal("0.4") + output * Decimal("1.8"))
            return (terra + (self.max_requests - 1) * luna) / Decimal(1_000_000)
        # Reviewed standard text rates; no cached-token discount is assumed.
        # Reserve the model's entire 400k input allowance, not a chars/token estimate.
        return (
            self.max_requests
            * (
                Decimal(self.reserved_input_tokens) * Decimal("0.75")
                + Decimal(self.max_output_tokens) * Decimal("4.50")
            )
            / Decimal(1_000_000)
        )

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.state_dir.resolve() / 'keystone-agents.sqlite3'}"

    @property
    def has_per_turn_request_limits(self) -> bool:
        return self.root_max_requests is not None

    def max_requests_for_turn(self, turn_ordinal: int) -> int:
        if turn_ordinal == 1:
            return self.root_max_requests or self.max_requests
        if turn_ordinal == 2 and self.followup_request_sha256:
            return self.followup_max_requests or self.max_requests
        raise CanaryPolicyError("canary_turn_request_limit_unavailable")

    @property
    def digest(self) -> str:
        # Preserve existing one-root and two-turn hashes when optional controls
        # are absent; opt-in limits remain bound into the reviewed profile.
        excluded: set[str] = set()
        if not self.followup_request_sha256:
            excluded.add("followup_request_sha256")
        if self.root_max_requests is None:
            excluded.add("root_max_requests")
        if self.followup_max_requests is None:
            excluded.add("followup_max_requests")
        if not self.is_public_preprint_scenario:
            excluded.update(
                {
                    "scenario",
                    "public_preprint_snapshot",
                    "public_preprint_snapshot_sha256",
                }
            )
        return _hash(self.model_dump_json(exclude=excluded))

    def check_paths(self) -> None:
        if self.repo_root.resolve() != _ROOT:
            raise CanaryPolicyError("canary_wrong_loaded_repository")
        validate_canary_state_dir(self.state_dir)
        if self.is_public_preprint_scenario:
            self.validated_public_preprint_snapshot()
            credential_paths = (self.key_env_file,)
        else:
            assert self.gmail_token_copy is not None
            assert self.gmail_credentials_file is not None
            token = self.gmail_token_copy.resolve()
            roots = (self.state_dir.resolve(), _ROOT / ".keystone/v2/slack-readiness")
            if not any(token.is_relative_to(root) for root in roots):
                raise CanaryPolicyError("canary_token_must_be_isolated_copy")
            credential_paths = (
                self.gmail_token_copy,
                self.gmail_credentials_file,
                self.key_env_file,
            )
        for path in credential_paths:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise CanaryPolicyError("canary_credential_reference_not_regular")
        if (
            not self.is_public_preprint_scenario
            and self.gmail_token_copy is not None
            and self.gmail_token_copy.stat().st_nlink != 1
        ):
            raise CanaryPolicyError("canary_token_copy_is_hardlinked")

    def validated_public_preprint_snapshot(self) -> Path:
        if not self.is_public_preprint_scenario or self.public_preprint_snapshot is None:
            raise CanaryPolicyError("canary_public_preprint_snapshot_not_allowed")
        state = self.state_dir.resolve()
        source_root = state / "public-sources"
        candidate = self.public_preprint_snapshot.expanduser()
        if not candidate.is_absolute() or source_root.is_symlink():
            raise CanaryPolicyError("canary_public_preprint_snapshot_scope_invalid")
        try:
            info = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise CanaryPolicyError("canary_public_preprint_snapshot_missing") from exc
        if (
            resolved != candidate
            or not resolved.is_relative_to(source_root)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
        ):
            raise CanaryPolicyError("canary_public_preprint_snapshot_scope_invalid")
        if _hash_file(resolved) != self.public_preprint_snapshot_sha256:
            raise CanaryPolicyError("canary_public_preprint_snapshot_hash_mismatch")
        try:
            with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(discovery_candidates)"
                    ).fetchall()
                }
                required = {
                    "candidate_id",
                    "candidate_type",
                    "source",
                    "source_item_id",
                    "title",
                    "summary",
                    "published",
                    "url",
                    "topics_json",
                    "status",
                    "last_seen_at",
                    "metadata_json",
                }
                total, preprints = connection.execute(
                    "SELECT COUNT(*), SUM(candidate_type = 'preprint') "
                    "FROM discovery_candidates"
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            raise CanaryPolicyError("canary_public_preprint_snapshot_invalid") from exc
        if (
            not required.issubset(columns)
            or total != preprints
            or not _PUBLIC_PREPRINT_MIN_CANDIDATES
            <= int(total or 0)
            <= _PUBLIC_PREPRINT_MAX_CANDIDATES
        ):
            raise CanaryPolicyError("canary_public_preprint_snapshot_invalid")
        return resolved

    def check_fresh_balance(self) -> None:
        if not self.enabled:
            raise CanaryPolicyError("canary_profile_not_enabled")
        if self.balance_observed_at is None:
            raise CanaryPolicyError("canary_balance_observation_missing")
        age = (datetime.now(UTC) - self.balance_observed_at.astimezone(UTC)).total_seconds()
        if age < -60 or age > 1800:
            raise CanaryPolicyError("canary_balance_observation_stale")

    def manifest(self) -> dict[str, Any]:
        manifest = {
            "schema": self.schema_name,
            "enabled": self.enabled,
            "profile_sha256": self.digest,
            "model": MODEL if self.model_profile == "mini" else self.model_profile,
            "model_profile": self.model_profile,
            "agent_models": {
                agent: self.model_for_agent(agent) for agent in sorted(self.allowed_agents)
            },
            "max_orchestrator_requests": (
                self.max_requests_for_turn(1)
                if self.is_public_preprint_scenario
                else 1
                if self.model_profile != "mini"
                else self.max_requests
            ),
            "max_requests": self.max_requests,
            "max_structured_output_retries": self.max_structured_output_retries,
            "max_input_bytes": self.max_input_bytes,
            "max_body_bytes": self.max_body_bytes,
            "max_output_tokens": self.max_output_tokens,
            "reserved_input_tokens_per_request": self.reserved_input_tokens,
            "required_reserve_usd": str(self.required_reserve_usd),
            "balance_floor_usd": str(self.balance_floor_usd),
            "hosted_tools_allowed": False,
            "tool_free_agents": sorted(_TOOL_FREE_AGENTS),
            "mini_only_agents": sorted(_MINI_ONLY_AGENTS),
            "http_retries": 0,
            "scope": ("two_reviewed_turns_one_thread_and_shared_budget"
                      if self.followup_request_sha256 else
                      "one_reviewed_request_and_channel_first_root_bound_atomically"),
            "max_slack_turns": 2 if self.followup_request_sha256 else 1,
            "gmail_operations": [] if self.is_public_preprint_scenario else ["GET"],
            "reserve_basis": (
                "standard text rates, no discounts; other account activity is external"
            ),
            "pricing_source": (
                "https://developers.openai.com/api/docs/pricing"
                if self.model_profile == "terra_luna"
                else "https://developers.openai.com/api/docs/models/gpt-5.4-mini"
            ),
            "cache_write_policy": (
                "explicit_without_breakpoints"
                if self.model_profile == "terra_luna" else "existing_sdk_policy"
            ),
        }
        if self.has_per_turn_request_limits:
            manifest["per_turn_request_limits"] = {
                "root": self.max_requests_for_turn(1),
                **(
                    {"followup": self.max_requests_for_turn(2)}
                    if self.followup_request_sha256
                    else {}
                ),
            }
        if self.is_public_preprint_scenario:
            manifest.update(
                {
                    "scenario": self.scenario,
                    "source_snapshot_sha256": self.public_preprint_snapshot_sha256,
                    "root_context_binding": (
                        "exact_digest"
                        if self.context_sha256
                        else "atomic_first_minimal_slack_root"
                    ),
                    "allowed_function_tools": {
                        agent: sorted(self.allowed_function_tools(agent))
                        for agent in sorted(self.allowed_agents)
                    },
                    "provider_reads": ["profile_bound_local_public_preprint_snapshot"],
                }
            )
        return manifest


def load_profile(path: str | Path | None = None) -> AcceptanceProfile | None:
    raw = str(path or os.environ.get(PROFILE_ENV, "")).strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink() or candidate.stat().st_size > 32_000:
        raise CanaryPolicyError("canary_profile_file_invalid")
    profile = AcceptanceProfile.model_validate_json(candidate.read_text())
    expected = os.environ.get(DIGEST_ENV)
    if expected and profile.digest != expected:
        raise CanaryPolicyError("canary_profile_changed")
    return profile


def public_preprint_snapshot_guard(path: str | Path) -> str:
    """Revalidate one profile-bound public snapshot immediately before its read."""

    profile = load_profile()
    if profile is None or not profile.enabled or not profile.is_public_preprint_scenario:
        raise CanaryPolicyError("canary_public_preprint_snapshot_not_allowed")
    ScopeLedger(profile).require_active_turn(1)
    profile.check_paths()
    expected = profile.validated_public_preprint_snapshot()
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise CanaryPolicyError("canary_public_preprint_snapshot_missing") from exc
    if resolved != expected:
        raise CanaryPolicyError("canary_public_preprint_snapshot_scope_invalid")
    return profile.public_preprint_snapshot_sha256


def public_preprint_snapshot_binding() -> tuple[Path, str] | None:
    """Return the exact root-turn snapshot, or no binding for other scenarios."""

    if not os.environ.get(PROFILE_ENV):
        return None
    profile = load_profile()
    if profile is None or not profile.is_public_preprint_scenario:
        return None
    if profile.public_preprint_snapshot is None:
        raise CanaryPolicyError("canary_public_preprint_snapshot_not_allowed")
    digest = public_preprint_snapshot_guard(profile.public_preprint_snapshot)
    return profile.public_preprint_snapshot.resolve(strict=True), digest


def _validate_unbound_public_preprint_root(context: Mapping[str, Any]) -> None:
    """Admit only the actual minimal bridge envelope for one new public root.

    Slack assigns the root timestamp after posting, so an enabled public profile
    may leave ``context_sha256`` empty. This narrow path does not trust a partial
    digest or wildcard: it validates every allowed field, then ``ScopeLedger``
    stores the complete received object under its atomic one-root claim.
    """

    if set(context) != _PUBLIC_PREPRINT_UNBOUND_ROOT_KEYS:
        raise CanaryPolicyError("canary_public_root_context_not_minimal")
    request_ts = str(context.get("request_ts") or "").strip()
    if (
        not _SLACK_TIMESTAMP_RE.fullmatch(request_ts)
        or context.get("thread_ts") != request_ts
        or context.get("read_context") != ""
        or context.get("thread_root_request") != ""
        or context.get("thread_messages") != []
        or context.get("thread_fetch_status") != "not_requested"
        or context.get("warnings") != []
    ):
        raise CanaryPolicyError("canary_public_root_context_not_minimal")


class ScopeLedger:
    def __init__(self, profile: AcceptanceProfile):
        self.profile = profile
        self.path = profile.state_dir.resolve() / "acceptance-scope.sqlite3"

    @contextmanager
    def connection(self):
        self.profile.check_paths()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() and (self.path.is_symlink() or self.path.stat().st_nlink != 1):
            raise CanaryPolicyError("canary_ledger_path_invalid")
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE IF NOT EXISTS scope (slot INTEGER PRIMARY KEY CHECK(slot=1), "
                "profile TEXT, claim TEXT, context TEXT, status TEXT, execution_id TEXT, "
                "requests INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS dispatches "
                "(ordinal INTEGER PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, used INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS accepted_turns "
                "(ordinal INTEGER PRIMARY KEY CHECK(ordinal BETWEEN 1 AND 2), "
                "request_ts TEXT UNIQUE NOT NULL, context TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS renewals "
                "(ordinal INTEGER PRIMARY KEY, record TEXT NOT NULL)"
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.path.chmod(0o600)

    def require_active(self) -> None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
            if (
                row is None
                or row["profile"] != self.profile.digest
                or row["claim"] != os.environ.get(CLAIM_ENV)
                or row["status"] != "running"
            ):
                raise CanaryPolicyError("canary_provider_without_active_root")

    def require_active_turn(self, expected_turn: int) -> None:
        if (
            not self.path.is_file()
            or self.path.is_symlink()
            or self.path.stat().st_nlink != 1
        ):
            raise CanaryPolicyError("canary_provider_without_active_root")
        try:
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
                turns = connection.execute(
                    "SELECT COUNT(*) FROM accepted_turns"
                ).fetchone()[0]
        except sqlite3.Error as exc:
            raise CanaryPolicyError("canary_provider_without_active_root") from exc
        if (
            row is None
            or row["profile"] != self.profile.digest
            or row["claim"] != os.environ.get(CLAIM_ENV)
            or row["status"] != "running"
        ):
            raise CanaryPolicyError("canary_provider_without_active_root")
        if int(turns or 0) != expected_turn:
            raise CanaryPolicyError("canary_provider_not_allowed_for_turn")

    def lease(self) -> str:
        self.require_active()
        nonce = uuid4().hex
        with self.connection() as connection:
            connection.execute("INSERT INTO leases VALUES(?,0)", (nonce,))
        return nonce

    def claim(self, context: Mapping[str, Any]) -> str:
        self.profile.check_fresh_balance()
        if not self.path.exists() and any(
            (self.profile.state_dir / name).exists()
            for name in ("keystone-agents.sqlite3", "sdk-sessions.sqlite3")
        ):
            raise CanaryPolicyError("canary_requires_fresh_business_and_session_state")
        if (
            context.get("schema") != "keystone.slack.history_context.v1"
            or context.get("source") != "slack_app_mention_history"
            or context.get("channel_id") != self.profile.channel_id
            or context.get("team_id") != self.profile.team_id
            or self.profile.author_id
            and context.get("user_id") != self.profile.author_id
        ):
            raise CanaryPolicyError("canary_context_scope_mismatch")
        if self.profile.is_public_preprint_scenario and not self.profile.context_sha256:
            _validate_unbound_public_preprint_root(context)
        request_ts = str(context.get("request_ts") or "")
        if not request_ts or request_ts != context.get("thread_ts"):
            raise CanaryPolicyError("canary_requires_one_new_root")
        messages = context.get("thread_messages") or []
        if (
            not isinstance(messages, list)
            or len(messages) > 1
            or any(
                not isinstance(message, Mapping)
                or message.get("ts") != request_ts
                or str(message.get("role") or "").lower() in {"assistant", "bot"}
                for message in messages
            )
        ):
            raise CanaryPolicyError("canary_existing_thread_context_not_approved")
        context_text = json.dumps(dict(context), sort_keys=True, ensure_ascii=True, allow_nan=False)
        if self.profile.context_sha256 and _hash(context_text) != self.profile.context_sha256:
            raise CanaryPolicyError("canary_context_digest_mismatch")
        if _hash(str(context.get("request_text") or "").strip()) != self.profile.request_sha256:
            raise CanaryPolicyError("canary_context_request_mismatch")
        claim = _hash(f"{self.profile.channel_id}:{request_ts}")
        with self.connection() as connection:
            prior = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
            if prior is not None:
                if prior["claim"] != claim or prior["profile"] != self.profile.digest:
                    raise CanaryPolicyError("canary_another_root_or_profile_denied")
                if prior["context"] != context_text:
                    raise CanaryPolicyError("canary_bound_context_changed")
                raise CanaryPolicyError("canary_exact_duplicate_no_execution")
            connection.execute(
                "INSERT INTO scope VALUES(1,?,?,?,'running','',0)",
                (self.profile.digest, claim, context_text),
            )
            connection.execute(
                "INSERT INTO accepted_turns VALUES(1,?,?)", (request_ts, context_text),
            )
        return claim

    def claim_followup(self, context: Mapping[str, Any]) -> str:
        """Admit exactly one reviewed natural follow-up without resetting the budget."""
        self.profile.check_fresh_balance()
        if not self.profile.followup_request_sha256:
            raise CanaryPolicyError("canary_followup_not_approved")
        if (
            context.get("schema") != "keystone.slack.history_context.v1"
            or context.get("source") != "slack_app_mention_history"
            or context.get("channel_id") != self.profile.channel_id
            or context.get("team_id") != self.profile.team_id
            or self.profile.author_id and context.get("user_id") != self.profile.author_id
            or _hash(canary_followup_request(str(context.get("request_text") or "")))
            != self.profile.followup_request_sha256
        ):
            raise CanaryPolicyError("canary_followup_scope_mismatch")
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
            if row is None or row["profile"] != self.profile.digest:
                raise CanaryPolicyError("canary_followup_root_not_bound")
            if row["status"] != "awaiting_followup":
                raise CanaryPolicyError("canary_thread_not_awaiting_followup")
            if row["requests"] >= self.profile.max_requests:
                raise CanaryPolicyError("canary_request_budget_exhausted")
            root = json.loads(row["context"])
            request_ts = str(context.get("request_ts") or "")
            try:
                later = bool(re.fullmatch(r"\d+\.\d{6}", request_ts)) and (
                    Decimal(request_ts) > Decimal(str(root.get("request_ts") or ""))
                )
            except ArithmeticError:
                later = False
            if context.get("thread_ts") != root.get("thread_ts") or not later:
                raise CanaryPolicyError("canary_followup_thread_mismatch")
            messages = context.get("thread_messages") or []
            if not isinstance(messages, list) or len(messages) > 12:
                raise CanaryPolicyError("canary_followup_context_unbounded")
            context_text = json.dumps(
                dict(context), sort_keys=True, ensure_ascii=True, allow_nan=False,
            )
            connection.execute(
                "INSERT INTO accepted_turns VALUES(2,?,?)", (request_ts, context_text),
            )
            # Each Slack turn gets a new execution; the root claim, DB and aggregate
            # model request counter remain unchanged across both turns.
            connection.execute("UPDATE scope SET status='running',execution_id='' WHERE slot=1")
            return str(row["claim"])

    def renew_awaiting_followup(
        self,
        replacement_profile: AcceptanceProfile,
        *,
        expected_old_profile_digest: str,
        review: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Renew one idle second-turn binding without changing its history or limits."""

        if expected_old_profile_digest != self.profile.digest:
            raise CanaryPolicyError("canary_followup_renewal_old_digest_mismatch")
        try:
            replacement = AcceptanceProfile.model_validate(
                replacement_profile.model_dump()
            )
        except ValueError as exc:
            raise CanaryPolicyError("canary_followup_renewal_profile_invalid") from exc
        old_scope = self.profile.model_dump(mode="json", exclude=_FOLLOWUP_RENEWAL_FIELDS)
        replacement_scope = replacement.model_dump(
            mode="json", exclude=_FOLLOWUP_RENEWAL_FIELDS
        )
        if old_scope != replacement_scope:
            raise CanaryPolicyError("canary_followup_renewal_scope_mismatch")
        if not replacement.enabled or not replacement.followup_request_sha256:
            raise CanaryPolicyError("canary_followup_renewal_request_hash_invalid")
        replacement.check_paths()
        replacement.check_fresh_balance()

        review_data = dict(review)
        if set(review_data) != _FOLLOWUP_RENEWAL_REVIEW_FIELDS:
            raise CanaryPolicyError("canary_followup_renewal_budget_evidence_unknown")
        try:
            observed_balance = Decimal(str(review_data["observed_balance_usd"]))
            approved_reserve = Decimal(str(review_data["approved_reserve_usd"]))
            open_hold = Decimal(str(review_data["open_hold_usd"]))
            observed_at = datetime.fromisoformat(
                str(review_data["balance_observed_at"]).replace("Z", "+00:00")
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise CanaryPolicyError(
                "canary_followup_renewal_budget_evidence_unknown"
            ) from exc
        if (
            review_data["schema"] != "keystone.canary_followup_renewal_review.v1"
            or review_data["status"] != "reconciled"
            or review_data["usage_reconciled"] is not True
            or not observed_balance.is_finite()
            or not approved_reserve.is_finite()
            or not open_hold.is_finite()
            or open_hold != 0
            or observed_balance != replacement.observed_balance_usd
            or approved_reserve != replacement.approved_reserve_usd
            or replacement.balance_observed_at is None
            or observed_at.tzinfo is None
            or observed_at.astimezone(UTC)
            != replacement.balance_observed_at.astimezone(UTC)
            or review_data["approval_reference"] != replacement.approval_reference
            or not str(review_data["evidence_reference"]).strip()
            or not str(review_data["rationale"]).strip()
            or not _is_sha256(str(review_data["original_runtime_source_sha256"]))
            or not _is_sha256(str(review_data["replacement_runtime_source_sha256"]))
        ):
            raise CanaryPolicyError("canary_followup_renewal_budget_evidence_unknown")

        loaded_runtime_path = self.profile.state_dir.resolve() / "loaded-runtime.json"
        if (
            not loaded_runtime_path.is_file()
            or loaded_runtime_path.is_symlink()
            or loaded_runtime_path.stat().st_nlink != 1
            or loaded_runtime_path.stat().st_size > 256_000
        ):
            raise CanaryPolicyError("canary_followup_renewal_root_source_missing")
        try:
            loaded_runtime = json.loads(loaded_runtime_path.read_text())
            original_source_sha256 = loaded_runtime["runtime_fingerprint"][
                "source_sha256"
            ]
        except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
            raise CanaryPolicyError(
                "canary_followup_renewal_root_source_missing"
            ) from exc
        from keystone_agents.runtime.provenance import build_runtime_fingerprint

        current_runtime = build_runtime_fingerprint(repo_root=self.profile.repo_root)
        if (
            original_source_sha256
            != review_data["original_runtime_source_sha256"]
            or current_runtime["source_read_error_count"] != 0
            or current_runtime["source_sha256"]
            != review_data["replacement_runtime_source_sha256"]
        ):
            raise CanaryPolicyError("canary_followup_renewal_source_mismatch")

        from keystone_agents.runtime.durable_execution import execution_database_path

        execution_path = execution_database_path(self.profile.database_url)
        if (
            not execution_path.is_file()
            or execution_path.is_symlink()
            or execution_path.stat().st_nlink != 1
        ):
            raise CanaryPolicyError("canary_followup_renewal_execution_missing")

        with self.connection() as connection:
            row = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
            if row is None:
                raise CanaryPolicyError("canary_followup_renewal_root_missing")
            if row["profile"] != expected_old_profile_digest:
                raise CanaryPolicyError("canary_followup_renewal_old_digest_mismatch")
            if row["status"] != "awaiting_followup":
                raise CanaryPolicyError("canary_followup_renewal_state_not_idle")
            requests = int(row["requests"])
            if requests >= self.profile.max_requests:
                raise CanaryPolicyError("canary_request_budget_exhausted")
            if not row["execution_id"]:
                raise CanaryPolicyError("canary_followup_renewal_execution_missing")

            accepted = connection.execute(
                "SELECT ordinal,request_ts,context FROM accepted_turns ORDER BY ordinal"
            ).fetchall()
            if len(accepted) != 1 or int(accepted[0]["ordinal"]) != 1:
                raise CanaryPolicyError("canary_followup_renewal_root_history_invalid")
            try:
                root = json.loads(row["context"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise CanaryPolicyError(
                    "canary_followup_renewal_root_history_invalid"
                ) from exc
            root_ts = str(root.get("request_ts") or "")
            if (
                accepted[0]["context"] != row["context"]
                or accepted[0]["request_ts"] != root_ts
                or root.get("schema") != "keystone.slack.history_context.v1"
                or root.get("source") != "slack_app_mention_history"
                or root.get("channel_id") != self.profile.channel_id
                or root.get("team_id") != self.profile.team_id
                or self.profile.author_id
                and root.get("user_id") != self.profile.author_id
                or root.get("thread_ts") != root_ts
                or _hash(str(root.get("request_text") or "").strip())
                != self.profile.request_sha256
                or self.profile.context_sha256
                and _hash(str(row["context"])) != self.profile.context_sha256
                or row["claim"] != _hash(f"{self.profile.channel_id}:{root_ts}")
            ):
                raise CanaryPolicyError("canary_followup_renewal_root_history_invalid")

            dispatches = connection.execute(
                "SELECT ordinal,metadata FROM dispatches ORDER BY ordinal"
            ).fetchall()
            if len(dispatches) != requests or [item["ordinal"] for item in dispatches] != list(
                range(1, requests + 1)
            ):
                raise CanaryPolicyError("canary_followup_renewal_dispatch_history_invalid")
            expected_manifest = self.profile.manifest()
            for item in dispatches:
                try:
                    metadata = json.loads(item["metadata"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CanaryPolicyError(
                        "canary_followup_renewal_dispatch_history_invalid"
                    ) from exc
                agent = str(metadata.get("agent") or "")
                if (
                    metadata.get("ordinal") != item["ordinal"]
                    or metadata.get("profile_sha256") != self.profile.digest
                    or metadata.get("turn_ordinal") != 1
                    or metadata.get("turn_request_ordinal") != item["ordinal"]
                    or metadata.get("reviewed_turn_max_requests")
                    != self.profile.max_requests_for_turn(1)
                    or type(metadata.get("turn_max_requests")) is not int
                    or not 1
                    <= metadata["turn_max_requests"]
                    <= self.profile.max_requests_for_turn(1)
                    or metadata.get("aggregate_max_requests") != self.profile.max_requests
                    or metadata.get("source_snapshot_sha256")
                    != self.profile.public_preprint_snapshot_sha256
                    or agent not in self.profile.allowed_agents
                    or metadata.get("model") != self.profile.model_for_agent(agent)
                    or metadata.get("agent_models") != expected_manifest["agent_models"]
                    or metadata.get("allowed_function_tools")
                    != expected_manifest.get("allowed_function_tools")
                ):
                    raise CanaryPolicyError(
                        "canary_followup_renewal_dispatch_history_invalid"
                    )
            if connection.execute(
                "SELECT COUNT(*) FROM leases WHERE used=0"
            ).fetchone()[0]:
                raise CanaryPolicyError("canary_followup_renewal_unused_lease")

            try:
                with sqlite3.connect(
                    f"file:{execution_path}?mode=ro", uri=True
                ) as execution_connection:
                    execution_row = execution_connection.execute(
                        "SELECT status,consumed FROM executions WHERE id=?",
                        (row["execution_id"],),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise CanaryPolicyError(
                    "canary_followup_renewal_execution_missing"
                ) from exc
            if execution_row is None:
                raise CanaryPolicyError("canary_followup_renewal_execution_missing")
            if execution_row[0] != "completed":
                raise CanaryPolicyError("canary_followup_renewal_execution_active")
            if int(execution_row[1]) != requests:
                raise CanaryPolicyError("canary_followup_renewal_dispatch_history_invalid")

            audit = {
                "schema": "keystone.canary_followup_renewal.v1",
                "renewal_ordinal": connection.execute(
                    "SELECT COUNT(*) + 1 FROM renewals"
                ).fetchone()[0],
                "old_profile_sha256": self.profile.digest,
                "new_profile_sha256": replacement.digest,
                "old_followup_request_sha256": self.profile.followup_request_sha256,
                "new_followup_request_sha256": replacement.followup_request_sha256,
                "claim": row["claim"],
                "root_request_ts": root_ts,
                "accepted_turns": 1,
                "consumed_requests": requests,
                "remaining_requests": replacement.max_requests - requests,
                "rationale": str(review_data["rationale"]).strip(),
                "budget_evidence": {
                    key: review_data[key]
                    for key in (
                        "status",
                        "usage_reconciled",
                        "observed_balance_usd",
                        "approved_reserve_usd",
                        "balance_observed_at",
                        "open_hold_usd",
                        "approval_reference",
                        "evidence_reference",
                    )
                },
                "original_runtime_source_sha256": review_data[
                    "original_runtime_source_sha256"
                ],
                "replacement_runtime_source_sha256": review_data[
                    "replacement_runtime_source_sha256"
                ],
            }
            connection.execute(
                "INSERT INTO renewals(ordinal,record) VALUES(?,?)",
                (
                    audit["renewal_ordinal"],
                    json.dumps(audit, sort_keys=True, allow_nan=False),
                ),
            )
            changed = connection.execute(
                "UPDATE scope SET profile=? WHERE slot=1 AND profile=? "
                "AND status='awaiting_followup'",
                (replacement.digest, expected_old_profile_digest),
            )
            if changed.rowcount != 1:
                raise CanaryPolicyError("canary_followup_renewal_compare_and_set_failed")
        return audit

    def thread_status(self) -> dict[str, Any]:
        """Read the disposable binding without touching the operator database."""
        self.profile.check_paths()
        state, count, requests = "not_started", 0, 0
        if self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_nlink != 1:
                raise CanaryPolicyError("canary_ledger_path_invalid")
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as connection:
                row = connection.execute(
                    "SELECT status,requests,profile FROM scope WHERE slot=1",
                ).fetchone()
                if row:
                    if row[2] != self.profile.digest:
                        raise CanaryPolicyError("canary_profile_changed")
                    state, requests = str(row[0]), int(row[1])
                    try:
                        count = connection.execute(
                            "SELECT COUNT(*) FROM accepted_turns",
                        ).fetchone()[0]
                    except sqlite3.OperationalError:
                        count = 1  # Historical one-root ledger.
        return {
            "status": state, "accepted_turns": count,
            "max_slack_turns": 2 if self.profile.followup_request_sha256 else 1,
            "model_requests": requests,
            "remaining_model_requests": max(0, self.profile.max_requests - requests),
            "approval_window_expired": bool(
                self.profile.balance_observed_at is None
                or (datetime.now(UTC) - self.profile.balance_observed_at.astimezone(UTC))
                .total_seconds() > 1800
            ),
            "keep_isolated_binding": state in {"running", "awaiting_followup"},
            "safe_to_restore_after_child_exit": state in {"completed", "expired"},
            "post_restoration_thread_routing_supported": False,
        }

    def expire_thread(self) -> None:
        """Close an idle test binding; this does not restore or change the Slack worker."""
        with self.connection() as connection:
            row = connection.execute("SELECT status,profile FROM scope WHERE slot=1").fetchone()
            if row is None or row[1] != self.profile.digest or row[0] == "running":
                raise CanaryPolicyError("canary_thread_cannot_expire_while_running_or_unbound")
            connection.execute("UPDATE scope SET status='expired' WHERE slot=1")

    def resume(self, execution_id: str) -> str:
        self.profile.check_fresh_balance()
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
            if (
                row is None
                or row["profile"] != self.profile.digest
                or row["status"] != "failed"
                or not execution_id
                or row["execution_id"] != execution_id
            ):
                raise CanaryPolicyError("canary_resume_not_bound_to_failed_execution")
            if row["requests"] >= self.profile.max_requests:
                raise CanaryPolicyError("canary_request_budget_exhausted")
            connection.execute("UPDATE scope SET status='running' WHERE slot=1")
            return str(row["claim"])

    def finish(self, claim: str, returncode: int) -> None:
        with self.connection() as connection:
            turns = connection.execute("SELECT COUNT(*) FROM accepted_turns").fetchone()[0]
            state = (
                "awaiting_followup"
                if returncode == 0 and self.profile.followup_request_sha256 and turns == 1
                else "completed" if returncode == 0 else "failed"
            )
            connection.execute(
                "UPDATE scope SET status=? WHERE slot=1 AND claim=? AND profile=?",
                (state, claim, self.profile.digest),
            )

    def dispatch(
        self, metadata: Mapping[str, Any], *, lease: str = "", agent: str = ""
    ) -> dict[str, Any]:
        claim = os.environ.get(CLAIM_ENV, "")
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
            if (
                row is None
                or row["profile"] != self.profile.digest
                or row["claim"] != claim
                or row["status"] != "running"
            ):
                raise CanaryPolicyError("canary_dispatch_without_active_root")
            if row["requests"] >= self.profile.max_requests:
                raise CanaryPolicyError("canary_request_budget_exhausted")
            turn_row = connection.execute(
                "SELECT COUNT(*) FROM accepted_turns"
            ).fetchone()
            turn_ordinal = max(1, int(turn_row[0] if turn_row else 0))
            if turn_ordinal not in {1, 2}:
                raise CanaryPolicyError("canary_turn_request_limit_unavailable")
            turn_request_count = 0
            reviewed_turn_max_requests = self.profile.max_requests_for_turn(turn_ordinal)
            turn_max_requests = reviewed_turn_max_requests
            if self.profile.has_per_turn_request_limits:
                for prior_dispatch in connection.execute(
                    "SELECT metadata FROM dispatches ORDER BY ordinal"
                ):
                    try:
                        prior_metadata = json.loads(prior_dispatch["metadata"])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise CanaryPolicyError(
                            "canary_turn_dispatch_accounting_missing"
                        ) from exc
                    prior_turn = prior_metadata.get("turn_ordinal")
                    if type(prior_turn) is not int or prior_turn not in {1, 2}:
                        raise CanaryPolicyError("canary_turn_dispatch_accounting_missing")
                    if prior_turn == turn_ordinal:
                        turn_request_count += 1
                        prior_limit = prior_metadata.get("turn_max_requests")
                        if (
                            type(prior_limit) is not int
                            or prior_limit < 1
                            or prior_limit > reviewed_turn_max_requests
                        ):
                            raise CanaryPolicyError(
                                "canary_turn_dispatch_accounting_missing"
                            )
                        turn_max_requests = min(turn_max_requests, prior_limit)
                requested_turn_limit = os.environ.get(TURN_REQUEST_LIMIT_ENV, "")
                if requested_turn_limit:
                    try:
                        parsed_turn_limit = int(requested_turn_limit)
                    except ValueError as exc:
                        raise CanaryPolicyError(
                            "canary_turn_request_limit_invalid"
                        ) from exc
                    if (
                        parsed_turn_limit < 1
                        or parsed_turn_limit > reviewed_turn_max_requests
                    ):
                        raise CanaryPolicyError("canary_turn_request_limit_invalid")
                    turn_max_requests = min(turn_max_requests, parsed_turn_limit)
                if turn_request_count >= turn_max_requests:
                    raise CanaryPolicyError("canary_turn_request_budget_exhausted")
            owner = agent or _AGENT.get()
            if self.profile.model_profile != "mini" and owner == "orchestrator":
                prior = connection.execute("SELECT metadata FROM dispatches").fetchall()
                if any(json.loads(item["metadata"]).get("agent") == owner for item in prior):
                    raise CanaryPolicyError("canary_orchestrator_comparison_limit")
            from keystone_agents.runtime.durable_execution import current_execution

            changed = connection.execute(
                "UPDATE leases SET used=1 WHERE id=? AND used=0", (lease or _LEASE.get(),)
            )
            if changed.rowcount != 1:
                raise CanaryPolicyError("canary_model_dispatch_lease_missing_or_used")
            execution = current_execution()
            execution_id = execution.execution_id if execution is not None else ""
            if row["execution_id"] and execution_id != row["execution_id"]:
                raise CanaryPolicyError("canary_execution_identity_drift")
            ordinal = int(row["requests"]) + 1
            receipt = {
                **self.profile.manifest(),
                **metadata,
                "ordinal": ordinal,
                "execution_id": execution_id,
                "agent": owner,
                "model": self.profile.model_for_agent(owner),
            }
            if self.profile.has_per_turn_request_limits:
                receipt.update(
                    {
                        "turn_ordinal": turn_ordinal,
                        "turn_request_ordinal": turn_request_count + 1,
                        "reviewed_turn_max_requests": reviewed_turn_max_requests,
                        "turn_max_requests": turn_max_requests,
                        "turn_remaining_after_reservation": (
                            turn_max_requests - turn_request_count - 1
                        ),
                        "aggregate_max_requests": self.profile.max_requests,
                        "aggregate_remaining_after_reservation": (
                            self.profile.max_requests - ordinal
                        ),
                    }
                )
            connection.execute(
                "UPDATE scope SET requests=?,execution_id=? WHERE slot=1", (ordinal, execution_id)
            )
            connection.execute(
                "INSERT INTO dispatches VALUES(?,?)",
                (ordinal, json.dumps(receipt, sort_keys=True, allow_nan=False)),
            )
            return receipt


def admit_agent(agent_name: str, *, observer: Any = None) -> None:
    if not os.environ.get(PROFILE_ENV):
        return
    _AGENT.set("")
    _LEASE.set("")
    profile = load_profile()
    if (
        profile is None
        or not profile.enabled
        or agent_name not in profile.allowed_agents
        or (
            agent_name in _MINI_ONLY_AGENTS
            and profile.model_profile != "mini"
        )
    ):
        raise CanaryPolicyError("canary_agent_not_allowed")
    _AGENT.set(agent_name)
    lease = ScopeLedger(profile).lease()
    _LEASE.set(lease)
    if observer is not None:
        pending = getattr(observer, "_keystone_canary_admissions", None)
        if pending is None:
            pending = []
            observer._keystone_canary_admissions = pending
        pending.append({"agent": agent_name, "lease": lease})


def validate_live_config(provider: str, model: str, kwargs: Mapping[str, Any]) -> None:
    profile = load_profile()
    if profile is None:
        return
    if (
        not profile.enabled
        or provider != "openai"
        or model not in {profile.model_for_agent(owner) for owner in profile.allowed_agents}
        or kwargs.get("use_responses") is False
        or str(kwargs.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        != "https://api.openai.com/v1"
    ):
        raise CanaryPolicyError("canary_model_provider_not_allowed")


def _json_value(value: Any) -> Any:
    if type(value).__name__ in {"Omit", "NotGiven"} and type(value).__module__.startswith("openai"):
        return _OMIT
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): parsed
            for key, item in value.items()
            if (parsed := _json_value(item)) is not _OMIT
        }
    if isinstance(value, list | tuple):
        return [parsed for item in value if (parsed := _json_value(item)) is not _OMIT]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise CanaryPolicyError("canary_non_json_request_body")


def _reasoning_item_digest(value: Any) -> str:
    # SDK input conversion omits unset/null and output-only created_by metadata.
    item = _json_value(value)
    return _hash(json.dumps(
        {key: field for key, field in item.items()
         if field is not None and key != "created_by"},
        ensure_ascii=True, sort_keys=True, allow_nan=False,
    ))


def observe_canary_response_reasoning(
    response: Any, *, observer: Any, dispatch_receipt: Mapping[str, Any] | None,
) -> None:
    """Remember only hashes of reasoning returned by this guarded SDK invocation."""
    if observer is None or dispatch_receipt is None or not any(
        item is dispatch_receipt
        for item in getattr(observer, "_keystone_canary_dispatches", ())
    ):
        return
    scope = (
        dispatch_receipt.get("profile_sha256"), _hash(os.environ.get(CLAIM_ENV, "")),
        dispatch_receipt.get("execution_id", ""), dispatch_receipt.get("model"),
    )
    observed = getattr(observer, "_keystone_canary_reasoning", set())
    for item in getattr(response, "output", ()) or ():
        if getattr(item, "type", None) == "reasoning" and isinstance(
            getattr(item, "encrypted_content", None), str,
        ) and item.encrypted_content:
            observed.add((*scope, _reasoning_item_digest(item)))
    observer._keystone_canary_reasoning = observed


def guarded_response_kwargs(
    client: Any,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    *,
    observer: Any = _OMIT,
) -> dict[str, Any]:
    profile = load_profile()
    if profile is None:
        return dict(kwargs)
    profile.check_fresh_balance()
    admission = {"agent": _AGENT.get(), "lease": _LEASE.get()}
    if observer is not _OMIT:
        pending = getattr(observer, "_keystone_canary_admissions", [])
        admission = pending.pop(0) if pending else {}
    if args or not admission.get("agent") or not admission.get("lease"):
        raise CanaryPolicyError("canary_unbound_model_request")
    if getattr(client, "max_retries", None) != 0:
        raise CanaryPolicyError("canary_http_retries_must_be_zero")
    body = _json_value(kwargs)
    if admission["agent"] in _TOOL_FREE_AGENTS and body.get("tools"):
        raise CanaryPolicyError("canary_instruction_repair_tools_not_allowed")
    if (
        body.get("model") != profile.model_for_agent(admission["agent"])
        or not (body.get("stream") is None or body.get("stream") is False)
        or str(getattr(client, "base_url", "")).rstrip("/") != "https://api.openai.com/v1"
    ):
        raise CanaryPolicyError("canary_response_endpoint_not_allowed")
    if any(
        body.get(key)
        for key in (
            "previous_response_id",
            "conversation",
            "prompt",
            "background",
            "extra_body",
            "extra_query",
            "context_management",
        )
    ):
        raise CanaryPolicyError("canary_unbounded_response_context")
    headers = body.get("extra_headers") or {}
    if not isinstance(headers, dict) or any(
        key.lower() != "user-agent" or value != f"Agents/Python {version('openai-agents')}"
        for key, value in headers.items()
    ):
        raise CanaryPolicyError("canary_extra_headers_not_allowed")
    if body.get("service_tier") not in (None, "default"):
        raise CanaryPolicyError("canary_nonstandard_service_tier")
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise CanaryPolicyError("canary_hosted_tool_not_allowed")
        if profile.is_public_preprint_scenario:
            name = str(tool.get("name") or "").strip()
            if name not in profile.allowed_function_tools(str(admission["agent"])):
                raise CanaryPolicyError("canary_function_tool_not_allowed")
            if name == _PUBLIC_PREPRINT_HISTORY_TOOL:
                ScopeLedger(profile).require_active_turn(1)
    from keystone_agents.runtime.durable_execution import current_execution

    execution = current_execution()
    reasoning_scope = (
        profile.digest, _hash(os.environ.get(CLAIM_ENV, "")),
        execution.execution_id if execution is not None else "", body.get("model"),
    )
    pending = [(body.get("input"), None, "input")]
    while pending:
        value, parent_type, parent_field = pending.pop()
        if isinstance(value, dict):
            if "id" in value and value.get("type") is None:
                raise CanaryPolicyError("canary_server_item_reference_not_allowed")
            input_type = value.get("type")
            reasoning_text = bool(
                parent_type == "reasoning"
                and (
                    (parent_field == "summary" and input_type == "summary_text")
                    or (parent_field == "content" and input_type == "reasoning_text")
                )
                and isinstance(value.get("text"), str)
            )
            observed_reasoning = bool(
                input_type == "reasoning" and parent_type is None and parent_field == "input"
                and (*reasoning_scope, _reasoning_item_digest(value))
                in getattr(observer, "_keystone_canary_reasoning", ())
            )
            if (
                value.get("type")
                not in {
                    None,
                    "message",
                    "input_text",
                    "output_text",
                    "function_call",
                    "function_call_output",
                    "reasoning",
                }
                and not reasoning_text
                or (value.get("encrypted_content") and not observed_reasoning)
                or any(value.get(key) for key in ("image_url", "file_url", "file_id", "file_data"))
            ):
                raise CanaryPolicyError("canary_non_text_input_not_allowed", measurements={
                    "reasoning_summary_text": int(input_type == "summary_text"),
                    "reasoning_text_content": int(input_type == "reasoning_text"),
                    "reasoning_parent": int(parent_type == "reasoning"),
                    "encrypted_content_present": int(bool(value.get("encrypted_content"))),
                    "media_reference_present": int(any(
                        value.get(key) for key in ("image_url", "file_url", "file_id", "file_data")
                    )),
                })
            pending.extend((item, input_type, key) for key, item in value.items())
        elif isinstance(value, list):
            pending.extend((item, parent_type, parent_field) for item in value)
    if profile.model_profile == "terra_luna" or (
        profile.model_profile == "terra_mini" and admission["agent"] == "orchestrator"
    ):
        # Disable implicit cache writes; explicit breakpoints are outside this test.
        scan = [body]
        while scan:
            value = scan.pop()
            if isinstance(value, dict):
                if "prompt_cache_breakpoint" in value:
                    raise CanaryPolicyError("canary_explicit_cache_write_not_allowed")
                scan.extend(value.values())
            elif isinstance(value, list):
                scan.extend(value)
        if body.get("prompt_cache_retention"):
            raise CanaryPolicyError("canary_legacy_cache_policy_not_allowed")
        body["prompt_cache_options"] = {"mode": "explicit"}
    requested = body.get("max_output_tokens")
    if requested is not None and (type(requested) is not int or requested < 1):
        raise CanaryPolicyError("canary_output_limit_invalid")
    body["max_output_tokens"] = min(
        requested or profile.max_output_tokens, profile.max_output_tokens
    )
    body["store"] = False
    body["service_tier"] = "default"
    input_bytes = len(
        json.dumps(
            {"instructions": body.get("instructions"), "input": body.get("input")},
            ensure_ascii=True,
            sort_keys=True,
            allow_nan=False,
        ).encode()
    )
    body_bytes = len(json.dumps(body, ensure_ascii=True, sort_keys=True, allow_nan=False).encode())
    if input_bytes > profile.max_input_bytes or body_bytes > profile.max_body_bytes:
        raise CanaryPolicyError("canary_serialized_request_bound_exceeded", measurements={
            "input_bytes": input_bytes, "max_input_bytes": profile.max_input_bytes,
            "body_bytes": body_bytes, "max_body_bytes": profile.max_body_bytes,
        })
    receipt = ScopeLedger(profile).dispatch(
        {
            "serialized_input_bytes": input_bytes,
            "serialized_body_bytes": body_bytes,
            "enforced_max_output_tokens": body["max_output_tokens"],
            "reasoning_effort": (body.get("reasoning") or {}).get("effort", "provider_default"),
            "api_dispatch_reserved": True,
        },
        lease=admission["lease"],
        agent=admission["agent"],
    )
    if observer is not _OMIT:
        records = getattr(observer, "_keystone_canary_dispatches", None)
        if records is None:
            records = []
            observer._keystone_canary_dispatches = records
        records.append(receipt)
    return {
        **kwargs,
        "max_output_tokens": body["max_output_tokens"],
        "store": False,
        "service_tier": "default",
        **({"prompt_cache_options": {"mode": "explicit"}}
           if profile.model_profile == "terra_luna" or (
               profile.model_profile == "terra_mini" and admission["agent"] == "orchestrator"
           ) else {}),
    }


def gmail_read_guard(method: str, token_path: Path, api_base_url: str) -> None:
    if not os.environ.get(PROFILE_ENV) and os.environ.get(GMAIL_READ_ONLY_ENV) != "true":
        return
    profile = load_profile()
    if (
        profile is None
        or not profile.enabled
        or profile.is_public_preprint_scenario
        or method.upper() != "GET"
    ):
        raise CanaryPolicyError("canary_gmail_mutation_denied")
    if _AGENT.get() in _TOOL_FREE_AGENTS:
        raise CanaryPolicyError("canary_instruction_repair_provider_action_not_allowed")
    profile.check_paths()
    if api_base_url.rstrip("/") != "https://gmail.googleapis.com/gmail/v1/users/me":
        raise CanaryPolicyError("canary_gmail_endpoint_not_allowed")
    assert profile.gmail_token_copy is not None
    if token_path.resolve() != profile.gmail_token_copy.resolve():
        raise CanaryPolicyError("canary_gmail_token_scope_mismatch")
    ScopeLedger(profile).require_active()


def child_environment(
    profile: AcceptanceProfile,
    claim: str,
    profile_path: Path,
    *,
    turn_request_limit: int | None = None,
) -> dict[str, str]:
    from dotenv import dotenv_values

    # Do not source a shell file or forward another provider's credentials.
    key = ""
    with profile.key_env_file.open() as handle:
        for line in handle:
            if line.strip().startswith("KEYSTONE_OPENAI_API_KEY="):
                key = str(
                    dotenv_values(stream=io.StringIO(line), interpolate=False).get(
                        "KEYSTONE_OPENAI_API_KEY"
                    )
                    or ""
                )
    if not key:
        raise CanaryPolicyError("canary_repo_openai_key_missing")
    public_snapshot = (
        profile.validated_public_preprint_snapshot()
        if profile.is_public_preprint_scenario
        else None
    )
    env = {
        name: value
        for name, value in os.environ.items()
        if name
        in {"PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env.update(MUTATION_DISABLED_ENV)
    env.update(
        {
            PROFILE_ENV: str(profile_path),
            DIGEST_ENV: profile.digest,
            CLAIM_ENV: claim,
            GMAIL_READ_ONLY_ENV: "true",
            "KEYSTONE_OPENAI_API_KEY": key,
            "DATABASE_URL": profile.database_url,
            "GOOGLE_TOKEN_FILE": str(profile.gmail_token_copy),
            "GOOGLE_CREDENTIALS_FILE": str(profile.gmail_credentials_file),
            "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
            "KEYSTONE_LIVE_MODE": "true",
            "KEYSTONE_DRY_RUN": "false",
            "MODEL_PROVIDER": "openai",
            "KEYSTONE_OPENAI_MODEL": profile.model_for_agent("chief_of_staff"),
            "KEYSTONE_LIVE_MODEL_MAX_RETRIES": "0",
            "KEYSTONE_MODEL_REQUEST_BUDGET_LIMIT": str(profile.max_requests),
            "KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES": str(
                profile.max_structured_output_retries
            ),
            "KEYSTONE_SDK_SESSION_DB": str(profile.state_dir / "sdk-sessions.sqlite3"),
            "KEYSTONE_SDK_SESSIONS": "true",
            "KEYSTONE_HOME": str(profile.state_dir / ".keystone"),
            "KEYSTONE_RUNTIME_STATE_DIR": str(profile.state_dir),
            "KEYSTONE_WORKITEM_LANGGRAPH": "true",
            "KEYSTONE_CONTEXT_CONFIG_OVERRIDE": "false",
            "KEYSTONE_CONTEXT_CONFIG_OVERRIDE_KEYS": "",
            "KEYSTONE_CONTEXT_CONFIG_REPO": str(profile.state_dir / "disabled-context-repo"),
            "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH": str(
                profile.state_dir / "disabled-workspace-token.json"
            ),
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": f"{_ROOT / 'src'}{os.pathsep}{_ROOT}",
            "SEARCH_PROVIDER": "searxng",
            "SEARXNG_BASE_URL": "http://127.0.0.1:18080",
            "KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL": "false",
            "KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK": "false",
            "KEYSTONE_EXA_SEARCH_FALLBACK": "false",
            "KEYSTONE_TAVILY_SEARCH_FALLBACK": "false",
            "KEYSTONE_ENABLE_WEBSITE_EXTRACTION": "true",
            "KEYSTONE_WEBSITE_EXTRACTOR": "trafilatura",
            "KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK": "trafilatura",
            "KEYSTONE_TRACE_SUMMARY_DB": str(profile.state_dir / "trace-summaries.sqlite3"),
        }
    )
    if public_snapshot is not None:
        for name in (
            GMAIL_READ_ONLY_ENV,
            "GOOGLE_TOKEN_FILE",
            "GOOGLE_CREDENTIALS_FILE",
            "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH",
            "KEYSTONE_ENABLE_LIVE_GMAIL",
            "SEARCH_PROVIDER",
            "SEARXNG_BASE_URL",
            "KEYSTONE_ENABLE_WEBSITE_EXTRACTION",
            "KEYSTONE_WEBSITE_EXTRACTOR",
            "KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK",
        ):
            env.pop(name, None)
        env.update(
            {
                "DISCOVERY_STORE_PATH": str(public_snapshot),
                "KEYSTONE_ENABLE_LIVE_RESEARCH": "false",
                "KEYSTONE_RSS_CONTEXT_LIVE_SLACK_READ_ENABLED": "false",
            }
        )
    for agent in profile.allowed_agents:
        env[f"KEYSTONE_{agent.upper()}_MODEL"] = profile.model_for_agent(agent)
        env[f"KEYSTONE_{agent.upper()}_MODEL_PROVIDER"] = "openai"
    if profile.has_per_turn_request_limits and turn_request_limit is not None:
        if not 1 <= turn_request_limit <= profile.max_requests:
            raise CanaryPolicyError("canary_turn_request_limit_invalid")
        env[TURN_REQUEST_LIMIT_ENV] = str(turn_request_limit)
    return env
