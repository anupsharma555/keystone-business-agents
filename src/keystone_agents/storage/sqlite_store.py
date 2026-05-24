"""SQLite persistence and audit logging for Keystone agents."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from keystone_agents.config import default_database_url
from keystone_agents.schemas.approval import (
    ApprovalDecisionRecord,
    ApprovalQueueItem,
    ApprovalQueueStatus,
    ApprovalScope,
    ApprovalState,
    approval_timestamp,
    normalize_approval_queue_status,
    normalize_approval_scope,
    validate_approval_queue_transition,
    validate_approval_transition,
)
from keystone_agents.schemas.automation import (
    AutomationChannelBinding,
    AutomationFinding,
    AutomationRun,
    AutomationSpec,
)
from keystone_agents.schemas.company_profile import CompanyFeatureRecord, CompanyProfile
from keystone_agents.schemas.contact_context import ContactRecord, CRMAccountContext
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.feedback import FeedbackRecord
from keystone_agents.schemas.memory import MemoryItem, normalize_memory_key
from keystone_agents.schemas.opportunity import OpportunityRecord
from keystone_agents.schemas.outreach import FollowUpScheduleRecord, OutreachTrackingRecord
from keystone_agents.schemas.outreach_examples import (
    OutreachExampleDocument,
    OutreachExampleRetrievalResult,
    RetrievedOutreachExample,
)
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemEvent,
    WorkItemStatus,
)

DEFAULT_DATABASE_URL = "sqlite:///keystone_agents.db"
CURRENT_SCHEMA_VERSION = 12
EASTERN_TIME_ZONE = "America/New_York"
EASTERN = ZoneInfo(EASTERN_TIME_ZONE)
REDACTION_MARKER = "[REDACTED]"
EMAIL_BODY_SUMMARY_CHARS = 180
TOOL_EVENT_SUMMARY_CHARS = 240

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|credential|authorization|auth[_-]?token)", re.I
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.I),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----.*?-----END .*?PRIVATE KEY-----",
        re.I | re.S,
    ),
    re.compile(r"\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(
        r"\b(?:api[_-]?key|secret|token|password|credential)\s*[:=]\s*['\"]?[^'\"\s,;]+",
        re.I,
    ),
)
SENSITIVE_PERSONAL_NOTE_PATTERNS = (
    re.compile(
        r"\bpatient\s+[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)?\b[^.\n]*"
        r"\b(?:diagnos(?:is|ed)|treatment|medication|therapy|depression|anxiety|"
        r"bipolar|schizophrenia|psychiatric)\b[^.\n]*",
        re.I,
    ),
    re.compile(
        r"\b(?:mrn|medical record number|dob|date of birth|ssn|social security)\b"
        r"\s*[:=]?\s*[^,;.\n]*",
        re.I,
    ),
)
EMAIL_BODY_KEYS = {
    "body",
    "email_body",
    "message_body",
    "raw_body",
    "content",
    "full_text",
    "normalized_body",
}
DRAFT_BODY_KEYS = {"draft_reply", "draft_body", "draft_text"}
SENSITIVE_BODY_KEYS = EMAIL_BODY_KEYS | DRAFT_BODY_KEYS
TIMESTAMP_METADATA_TABLES = (
    "agent_runs",
    "emails",
    "companies",
    "opportunities",
    "outreach_drafts",
    "approvals",
    "approval_queue",
    "sources",
    "feedback",
    "tool_events",
    "agent_run_logs",
    "contacts",
    "crm_contexts",
    "follow_up_schedules",
    "outreach_tracking",
    "email_style_profiles",
    "memory_items",
    "outreach_examples",
    "work_items",
    "work_item_events",
    "work_item_artifacts",
    "automation_specs",
    "automation_runs",
    "automation_channel_bindings",
    "automation_findings",
)

OUTREACH_EMAIL_BODY_STORAGE_NOTE = (
    "Outbound outreach email bodies are stored after redaction because the local SQLite record "
    "is the approval artifact reviewers need before any draft can be copied to a live system. "
    "Inbound email bodies and triage draft replies are stored only as a hash plus summary."
)

SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

FEEDBACK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    approval_id TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    source_agent TEXT NOT NULL DEFAULT '',
    review_stage TEXT NOT NULL DEFAULT 'approval_review',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_feedback_object_type ON feedback(object_type);
CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(rating);
CREATE INDEX IF NOT EXISTS idx_feedback_created_date_et ON feedback(created_date_et);
"""

TOOL_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    input_hash TEXT NOT NULL DEFAULT '',
    input_summary TEXT NOT NULL DEFAULT '',
    output_hash TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    dry_run INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'success',
    error TEXT,
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tool_events_tool_name ON tool_events(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_events_agent_name ON tool_events(agent_name);
CREATE INDEX IF NOT EXISTS idx_tool_events_run_id ON tool_events(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_events_status ON tool_events(status);
CREATE INDEX IF NOT EXISTS idx_tool_events_created_date_et
ON tool_events(created_date_et);
"""

AGENT_RUN_LOGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    step_name TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '',
    input_summary TEXT NOT NULL DEFAULT '',
    output_hash TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    dry_run INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'success',
    error TEXT,
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_run_logs_run_id ON agent_run_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_run_logs_step_name ON agent_run_logs(step_name);
CREATE INDEX IF NOT EXISTS idx_agent_run_logs_agent_name ON agent_run_logs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_run_logs_status ON agent_run_logs(status);
CREATE INDEX IF NOT EXISTS idx_agent_run_logs_created_date_et
ON agent_run_logs(created_date_et);
"""

APPROVAL_QUEUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS approval_queue (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    draft_text TEXT,
    source_agent TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    approval_status TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT,
    reviewer_notes TEXT,
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    updated_at_utc TEXT,
    expires_at_utc TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_approval_queue_status
ON approval_queue(approval_status);

CREATE INDEX IF NOT EXISTS idx_approval_queue_object
ON approval_queue(object_type, object_id);
"""

CONTACT_CRM_CONTEXT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    role_title TEXT NOT NULL DEFAULT '',
    contact_email TEXT,
    linkedin_url TEXT,
    source TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'pending',
    approval_scope TEXT NOT NULL DEFAULT 'drafting',
    notes TEXT NOT NULL DEFAULT '',
    contact_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contacts_company_name ON contacts(company_name);
CREATE INDEX IF NOT EXISTS idx_contacts_approval_state ON contacts(approval_state);

CREATE TABLE IF NOT EXISTS crm_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL DEFAULT '',
    account_stage TEXT NOT NULL DEFAULT '',
    account_owner TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'pending',
    approval_scope TEXT NOT NULL DEFAULT 'drafting',
    notes TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_crm_contexts_company_name ON crm_contexts(company_name);
CREATE INDEX IF NOT EXISTS idx_crm_contexts_approval_state ON crm_contexts(approval_state);
"""

FOLLOW_UP_SCHEDULES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS follow_up_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL DEFAULT '',
    contact_name TEXT,
    related_draft_id TEXT NOT NULL DEFAULT '',
    proposed_date TEXT NOT NULL DEFAULT '',
    sequence_number INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'recommended',
    rationale TEXT NOT NULL DEFAULT '',
    approval_required INTEGER NOT NULL DEFAULT 1,
    schedule_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_follow_up_schedules_company_name
ON follow_up_schedules(company_name);

CREATE INDEX IF NOT EXISTS idx_follow_up_schedules_related_draft_id
ON follow_up_schedules(related_draft_id);

CREATE INDEX IF NOT EXISTS idx_follow_up_schedules_status
ON follow_up_schedules(status);

CREATE INDEX IF NOT EXISTS idx_follow_up_schedules_proposed_date
ON follow_up_schedules(proposed_date);
"""

OUTREACH_TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS outreach_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'email',
    lifecycle_status TEXT NOT NULL DEFAULT 'not_started',
    outreach_sent INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL DEFAULT '',
    sent_by TEXT NOT NULL DEFAULT '',
    sent_via TEXT NOT NULL DEFAULT '',
    reply_received INTEGER NOT NULL DEFAULT 0,
    reply_received_at TEXT NOT NULL DEFAULT '',
    reply_summary TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'unknown',
    outcome_notes TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL DEFAULT '',
    tracking_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outreach_tracking_draft_id
ON outreach_tracking(draft_id);

CREATE INDEX IF NOT EXISTS idx_outreach_tracking_company_name
ON outreach_tracking(company_name);

CREATE INDEX IF NOT EXISTS idx_outreach_tracking_lifecycle_status
ON outreach_tracking(lifecycle_status);

CREATE INDEX IF NOT EXISTS idx_outreach_tracking_outcome
ON outreach_tracking(outcome);

CREATE INDEX IF NOT EXISTS idx_outreach_tracking_sent_reply
ON outreach_tracking(outreach_sent, reply_received);
"""

EMAIL_STYLE_PROFILES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS email_style_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'pending',
    approval_scope TEXT NOT NULL DEFAULT 'drafting',
    profile_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_email_style_profiles_profile_id
ON email_style_profiles(profile_id);

CREATE INDEX IF NOT EXISTS idx_email_style_profiles_approval_state
ON email_style_profiles(approval_state);
"""

MEMORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS memory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL,
    object_type TEXT NOT NULL DEFAULT 'other',
    object_id TEXT NOT NULL DEFAULT '',
    object_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    approval_state TEXT NOT NULL DEFAULT 'pending',
    confidence REAL NOT NULL DEFAULT 0,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_for_prompt INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    supersedes_memory_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    memory_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memory_items_type ON memory_items(memory_type);
CREATE INDEX IF NOT EXISTS idx_memory_items_object ON memory_items(object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_memory_items_object_key ON memory_items(object_key);
CREATE INDEX IF NOT EXISTS idx_memory_items_approval_state ON memory_items(approval_state);
CREATE INDEX IF NOT EXISTS idx_memory_items_safe_for_prompt ON memory_items(safe_for_prompt);

CREATE TABLE IF NOT EXISTS memory_index (
    memory_id INTEGER PRIMARY KEY,
    memory_type TEXT NOT NULL,
    object_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    safe_text TEXT NOT NULL DEFAULT '',
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at_utc TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_index_type ON memory_index(memory_type);
CREATE INDEX IF NOT EXISTS idx_memory_index_object_key ON memory_index(object_key);
"""

OUTREACH_EXAMPLES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS outreach_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    example_id TEXT NOT NULL UNIQUE,
    source_thread_id_hash TEXT NOT NULL DEFAULT '',
    source_label TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'email',
    outreach_stage TEXT NOT NULL DEFAULT 'initial_outreach',
    outcome TEXT NOT NULL DEFAULT 'success',
    company_type TEXT NOT NULL DEFAULT '',
    opportunity_type TEXT NOT NULL DEFAULT '',
    template_id TEXT,
    style_profile_id TEXT,
    approved_for_drafting INTEGER NOT NULL DEFAULT 0,
    raw_body_included INTEGER NOT NULL DEFAULT 0,
    retrieval_text TEXT NOT NULL DEFAULT '',
    document_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outreach_examples_example_id
ON outreach_examples(example_id);

CREATE INDEX IF NOT EXISTS idx_outreach_examples_approved
ON outreach_examples(approved_for_drafting);

CREATE INDEX IF NOT EXISTS idx_outreach_examples_company_type
ON outreach_examples(company_type);

CREATE INDEX IF NOT EXISTS idx_outreach_examples_opportunity_type
ON outreach_examples(opportunity_type);

CREATE VIRTUAL TABLE IF NOT EXISTS outreach_example_fts
USING fts5(example_id UNINDEXED, retrieval_text);
"""

WORK_ITEMS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    request_text TEXT NOT NULL DEFAULT '',
    target_json TEXT NOT NULL DEFAULT '{}',
    current_route TEXT NOT NULL DEFAULT 'orchestrator',
    work_item_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    updated_at_utc TEXT NOT NULL DEFAULT '',
    last_agent TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_work_items_kind ON work_items(kind);
CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_work_items_current_route ON work_items(current_route);
CREATE INDEX IF NOT EXISTS idx_work_items_created_date_et ON work_items(created_date_et);

CREATE TABLE IF NOT EXISTS work_item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    summary TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_work_item_events_work_item_id
ON work_item_events(work_item_id);

CREATE TABLE IF NOT EXISTS work_item_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    source_agent TEXT NOT NULL DEFAULT '',
    approval_state TEXT NOT NULL DEFAULT 'pending',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_work_item_artifacts_work_item_id
ON work_item_artifacts(work_item_id);

CREATE INDEX IF NOT EXISTS idx_work_item_artifacts_type
ON work_item_artifacts(artifact_type, artifact_id);
"""

AUTOMATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS automation_specs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'enabled',
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    workflow TEXT NOT NULL DEFAULT '',
    target_agent TEXT NOT NULL DEFAULT '',
    default_channel TEXT NOT NULL DEFAULT '',
    spec_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    updated_at_utc TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_automation_specs_status
ON automation_specs(status);

CREATE INDEX IF NOT EXISTS idx_automation_specs_name
ON automation_specs(name);

CREATE TABLE IF NOT EXISTS automation_channel_bindings (
    id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    destination_type TEXT NOT NULL DEFAULT 'slack',
    purpose TEXT NOT NULL DEFAULT 'review',
    binding_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_automation_channel_bindings_automation
ON automation_channel_bindings(automation_id);

CREATE INDEX IF NOT EXISTS idx_automation_channel_bindings_channel
ON automation_channel_bindings(channel_name, channel_id);

CREATE TABLE IF NOT EXISTS automation_runs (
    id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    automation_name TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'dry_run',
    work_item_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    approval_count INTEGER NOT NULL DEFAULT 0,
    failure_summary TEXT NOT NULL DEFAULT '',
    next_safe_action TEXT NOT NULL DEFAULT '',
    run_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    completed_at_utc TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_automation_runs_automation
ON automation_runs(automation_id);

CREATE INDEX IF NOT EXISTS idx_automation_runs_status
ON automation_runs(status);

CREATE INDEX IF NOT EXISTS idx_automation_runs_work_item
ON automation_runs(work_item_id);

CREATE TABLE IF NOT EXISTS automation_findings (
    id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    finding_type TEXT NOT NULL DEFAULT 'status',
    severity TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    finding_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_automation_findings_automation
ON automation_findings(automation_id);

CREATE INDEX IF NOT EXISTS idx_automation_findings_run
ON automation_findings(run_id);

CREATE INDEX IF NOT EXISTS idx_automation_findings_severity
ON automation_findings(severity);
"""

INITIAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_summary TEXT NOT NULL DEFAULT '',
    output_json TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL DEFAULT '',
    dry_run INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    error TEXT,
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    needs_reply INTEGER NOT NULL DEFAULT 0,
    urgency TEXT NOT NULL DEFAULT '',
    triage_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    company_url TEXT,
    profile_json TEXT NOT NULL DEFAULT '{}',
    consulting_fit_score INTEGER,
    confidence_score REAL,
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_company TEXT NOT NULL,
    opportunity_type TEXT NOT NULL DEFAULT '',
    priority_score INTEGER,
    status TEXT NOT NULL DEFAULT 'candidate',
    opportunity_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outreach_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL DEFAULT '',
    contact_name TEXT,
    email_subject TEXT NOT NULL DEFAULT '',
    email_body TEXT NOT NULL DEFAULT '',
    approval_state TEXT NOT NULL DEFAULT 'pending',
    draft_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    scope TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decision_at_et TEXT NOT NULL DEFAULT '',
    decision_date_et TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    source_agent TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    rating TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL DEFAULT '',
    created_at_et TEXT NOT NULL DEFAULT '',
    created_date_et TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "initial_storage_schema", INITIAL_SCHEMA_SQL),
    (2, "time_metadata_columns", "SELECT 1;"),
    (3, "tool_events", TOOL_EVENTS_TABLE_SQL),
    (4, "local_contact_crm_context", CONTACT_CRM_CONTEXT_TABLE_SQL),
    (5, "agent_run_logs", AGENT_RUN_LOGS_TABLE_SQL),
    (6, "follow_up_schedules", FOLLOW_UP_SCHEDULES_TABLE_SQL),
    (7, "email_style_profiles", EMAIL_STYLE_PROFILES_TABLE_SQL),
    (8, "memory_items", MEMORY_TABLE_SQL),
    (9, "outreach_tracking", OUTREACH_TRACKING_TABLE_SQL),
    (10, "outreach_examples", OUTREACH_EXAMPLES_TABLE_SQL),
    (11, "feedback_metadata_columns", "SELECT 1;"),
    (12, "work_items", WORK_ITEMS_TABLE_SQL),
)


def database_url_from_env() -> str:
    """Return the configured database URL, defaulting to local SQLite."""

    return default_database_url()


def sqlite_path_from_url(database_url: str | Path | None = None) -> str:
    """Resolve a SQLite URL or local path to a sqlite3 database path."""

    if database_url is None:
        database_url = database_url_from_env()
    if isinstance(database_url, Path):
        return str(database_url)

    value = str(database_url)
    if value == ":memory:":
        return value
    if not value.startswith("sqlite:"):
        return value

    parsed = urlparse(value)
    if parsed.scheme != "sqlite":
        raise ValueError("Only sqlite database URLs are supported.")
    if parsed.netloc:
        raise ValueError("SQLite DATABASE_URL must not include a network host.")
    if parsed.path in {"", "/"}:
        raise ValueError("SQLite DATABASE_URL must include a database path.")
    if parsed.path == "/:memory:":
        return ":memory:"

    path = unquote(parsed.path)
    if value.startswith("sqlite:////"):
        return "/" + path.lstrip("/")
    return path.lstrip("/")


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Expected Pydantic model or mapping, got {type(value).__name__}.")


def _redact_string(value: str) -> str:
    redacted = value
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    return redacted


def _redact_sensitive_personal_note(value: str) -> str:
    redacted = _redact_string(value)
    for pattern in SENSITIVE_PERSONAL_NOTE_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    return redacted


def redact_secrets(value: Any, *, summarize_email_content: bool = False) -> Any:
    """Recursively redact secret-like values and optionally summarize email bodies."""

    if isinstance(value, BaseModel):
        return redact_secrets(
            value.model_dump(mode="json"), summarize_email_content=summarize_email_content
        )
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lookup = key_text.lower()
            if SECRET_KEY_RE.search(key_text):
                result[key_text] = REDACTION_MARKER
                continue
            if (
                summarize_email_content
                and key_lookup in SENSITIVE_BODY_KEYS
                and isinstance(item, str)
            ):
                result[f"{key_text}_hash"] = stable_hash(item)
                result[f"{key_text}_summary"] = _redact_string(
                    " ".join(item.split())[:EMAIL_BODY_SUMMARY_CHARS]
                )
                continue
            result[key_text] = redact_secrets(item, summarize_email_content=summarize_email_content)
        return result
    if isinstance(value, list):
        return [
            redact_secrets(item, summarize_email_content=summarize_email_content) for item in value
        ]
    if isinstance(value, tuple):
        return [
            redact_secrets(item, summarize_email_content=summarize_email_content) for item in value
        ]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def stable_json(value: Any, *, summarize_email_content: bool = False) -> str:
    """Serialize values deterministically after redaction."""

    return json.dumps(
        redact_secrets(value, summarize_email_content=summarize_email_content),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def stable_hash(value: Any) -> str:
    """Return a stable SHA-256 hash without storing the original payload."""

    if isinstance(value, str):
        payload = value
    else:
        payload = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_safe_value(value: Any) -> Any:
    """Return a value suitable for tool event hashing and short summaries."""

    if isinstance(value, BaseModel):
        return _audit_safe_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lookup = key_text.lower()
            if SECRET_KEY_RE.search(key_text):
                result[key_text] = REDACTION_MARKER
                continue
            if key_lookup in SENSITIVE_BODY_KEYS and isinstance(item, str):
                result[f"{key_text}_hash"] = stable_hash(_redact_string(item))
                result[f"{key_text}_length"] = len(item)
                continue
            result[key_text] = _audit_safe_value(item)
        return result
    if isinstance(value, list | tuple):
        return [_audit_safe_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def summarize_tool_event_payload(value: Any) -> str:
    """Return a short, redacted tool event summary without full body fields."""

    if value is None or value == "":
        return ""
    safe_value = _audit_safe_value(value)
    if safe_value is None or safe_value == "":
        return ""
    if isinstance(safe_value, str):
        text = safe_value
    else:
        text = json.dumps(safe_value, ensure_ascii=True, sort_keys=True, default=str)
    return " ".join(_redact_string(text).split())[:TOOL_EVENT_SUMMARY_CHARS]


def _coerce_utc_datetime(value: datetime | str | None = None) -> datetime:
    if value is None or value == "":
        return datetime.now(UTC).replace(microsecond=0)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC, microsecond=0)
        return value.astimezone(UTC).replace(microsecond=0)
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _time_metadata(value: datetime | str | None = None) -> dict[str, str]:
    utc_value = _coerce_utc_datetime(value)
    et_value = utc_value.astimezone(EASTERN).replace(microsecond=0)
    return {
        "utc": _iso_z(utc_value),
        "et": et_value.isoformat(),
        "date_et": et_value.date().isoformat(),
    }


def _optional_iso_z(value: datetime | str | None) -> str | None:
    if value is None or value == "":
        return None
    return _iso_z(_coerce_utc_datetime(value))


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    loaded = json.loads(value)
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded]


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        return {}
    return dict(loaded)


def _memory_query_tokens(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", str(query or "").lower()) if len(token) > 1]


def _score_memory_row(
    *,
    tokens: list[str],
    object_key: str,
    row: dict[str, Any],
) -> int:
    title = str(row.get("index_title") or row.get("title") or "").lower()
    summary = str(row.get("index_summary") or row.get("summary") or "").lower()
    safe_text = str(row.get("index_safe_text") or "").lower()
    score = 0
    if object_key and str(row.get("object_key") or "") == object_key:
        score += 20
    if not tokens:
        return score + 1
    for token in tokens:
        if token in title:
            score += 10
        if token in summary:
            score += 5
        score += min(safe_text.count(token), 5)
    return score


def _fts_query_tokens(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", str(query or "").lower()) if len(token) > 1]


def _fts_match_query(query: str) -> str:
    return " OR ".join(f'"{token}"' for token in _fts_query_tokens(query))


def _score_outreach_example_row(tokens: list[str], row: dict[str, Any]) -> int:
    text = " ".join(
        [
            str(row.get("retrieval_text") or ""),
            str(row.get("company_type") or ""),
            str(row.get("opportunity_type") or ""),
            str(row.get("outreach_stage") or ""),
            str(row.get("outcome") or ""),
        ]
    ).lower()
    if not tokens:
        return 1
    score = 0
    for token in tokens:
        score += min(text.count(token), 8)
    return score


def _outreach_example_match_reason(
    tokens: list[str],
    row: dict[str, Any],
    *,
    company_type: str | None,
    opportunity_type: str | None,
) -> str:
    reasons: list[str] = []
    if tokens and _score_outreach_example_row(tokens, row) > 0:
        reasons.append("semantic token match")
    if company_type and str(row.get("company_type") or "") == company_type:
        reasons.append("company type match")
    if opportunity_type and str(row.get("opportunity_type") or "") == opportunity_type:
        reasons.append("opportunity type match")
    template_id = str(row.get("template_id") or "").lower()
    if template_id and any(token in template_id for token in tokens):
        reasons.append("template match")
    outreach_stage = str(row.get("outreach_stage") or "").lower()
    if outreach_stage and any(token in outreach_stage for token in tokens):
        reasons.append("stage match")
    return ", ".join(dict.fromkeys(reasons)) or "approved sanitized example"


def _retrieved_outreach_example(
    document: OutreachExampleDocument,
    *,
    match_score: int,
    match_reason: str,
) -> RetrievedOutreachExample:
    return RetrievedOutreachExample(
        example_id=document.example_id,
        match_score=max(0, match_score),
        match_reason=match_reason,
        conversation_pattern=document.conversation_pattern,
        effective_phrases=document.effective_phrases,
        cta_pattern=document.cta_pattern,
        follow_up_pattern=document.follow_up_pattern,
        reply_pattern=document.reply_pattern,
        lessons_learned=document.lessons_learned,
        template_id=document.template_id,
        style_profile_id=document.style_profile_id,
        raw_body_included=False,
    )


def _approval_queue_item_from_row(row: sqlite3.Row) -> ApprovalQueueItem:
    return ApprovalQueueItem(
        id=str(row["id"]),
        object_type=str(row["object_type"]),
        object_id=row["object_id"],
        title=str(row["title"]),
        summary=str(row["summary"]),
        draft_text=row["draft_text"],
        source_agent=str(row["source_agent"]),
        risk_flags=_json_list(row["risk_flags_json"]),
        approval_status=str(row["approval_status"]),
        reviewer=row["reviewer"],
        reviewer_notes=row["reviewer_notes"],
        created_at=str(row["created_at_utc"]),
        updated_at=row["updated_at_utc"],
        expires_at=row["expires_at_utc"],
        metadata=_json_dict(row["metadata_json"]),
    )


class SQLiteStore:
    """SQLite-backed storage for local audit logs and dry-run artifacts."""

    def __init__(self, database_url: str | Path | None = None) -> None:
        self.database_url = str(database_url or database_url_from_env())
        self.path = sqlite_path_from_url(database_url)
        self._memory_connection: sqlite3.Connection | None = None
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @classmethod
    def from_env(cls) -> SQLiteStore:
        return cls(database_url_from_env())

    def connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            if self._memory_connection is None:
                self._memory_connection = sqlite3.connect(self.path)
                self._memory_connection.row_factory = sqlite3.Row
            return self._memory_connection
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(SCHEMA_MIGRATIONS_TABLE_SQL)
            applied_versions = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, name, sql in SCHEMA_MIGRATIONS:
                if version in applied_versions:
                    continue
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (version, name),
                )
            self._ensure_schema_compatibility(connection)

    def _ensure_schema_compatibility(self, connection: sqlite3.Connection) -> None:
        """Add columns needed by newer storage metadata to older DBs."""

        connection.executescript(FEEDBACK_TABLE_SQL)
        connection.executescript(APPROVAL_QUEUE_TABLE_SQL)
        connection.executescript(TOOL_EVENTS_TABLE_SQL)
        connection.executescript(AGENT_RUN_LOGS_TABLE_SQL)
        connection.executescript(CONTACT_CRM_CONTEXT_TABLE_SQL)
        connection.executescript(FOLLOW_UP_SCHEDULES_TABLE_SQL)
        connection.executescript(OUTREACH_TRACKING_TABLE_SQL)
        connection.executescript(EMAIL_STYLE_PROFILES_TABLE_SQL)
        connection.executescript(MEMORY_TABLE_SQL)
        connection.executescript(OUTREACH_EXAMPLES_TABLE_SQL)
        connection.executescript(WORK_ITEMS_TABLE_SQL)
        connection.executescript(AUTOMATIONS_TABLE_SQL)

        outreach_columns = self._column_names(connection, "outreach_drafts")
        if "approval_state" not in outreach_columns:
            connection.execute(
                "ALTER TABLE outreach_drafts ADD COLUMN approval_state TEXT NOT NULL "
                "DEFAULT 'pending'"
            )

        approval_columns = self._column_names(connection, "approvals")
        if "decision" not in approval_columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN decision TEXT NOT NULL DEFAULT 'pending'"
            )
        if "scope" not in approval_columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN scope TEXT NOT NULL DEFAULT 'send'"
            )
        if "timestamp" not in approval_columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN timestamp TEXT NOT NULL DEFAULT ''"
            )
        if "risk_flags_json" not in approval_columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN risk_flags_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "source_agent" not in approval_columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN source_agent TEXT NOT NULL DEFAULT ''"
            )

        feedback_columns = self._column_names(connection, "feedback")
        if "approval_id" not in feedback_columns:
            connection.execute(
                "ALTER TABLE feedback ADD COLUMN approval_id TEXT NOT NULL DEFAULT ''"
            )
        if "risk_flags_json" not in feedback_columns:
            connection.execute(
                "ALTER TABLE feedback ADD COLUMN risk_flags_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "source_agent" not in feedback_columns:
            connection.execute(
                "ALTER TABLE feedback ADD COLUMN source_agent TEXT NOT NULL DEFAULT ''"
            )
        if "review_stage" not in feedback_columns:
            connection.execute(
                "ALTER TABLE feedback ADD COLUMN review_stage TEXT NOT NULL DEFAULT "
                "'approval_review'"
            )
        self._ensure_time_metadata_columns(connection)

    def _ensure_time_metadata_columns(self, connection: sqlite3.Connection) -> None:
        for table in TIMESTAMP_METADATA_TABLES:
            columns = self._column_names(connection, table)
            for column in ("created_at_utc", "created_at_et", "created_date_et"):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_created_date_et "
                f"ON {table}(created_date_et)"
            )

        approval_columns = self._column_names(connection, "approvals")
        if "decision_at_et" not in approval_columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN decision_at_et TEXT NOT NULL DEFAULT ''"
            )
        if "decision_date_et" not in approval_columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN decision_date_et TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_approvals_decision_date_et "
            "ON approvals(decision_date_et)"
        )

    def _column_names(self, connection: sqlite3.Connection, table: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _initialize(self) -> None:
        self.initialize()

    def table_names(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def migration_versions(self) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        return [int(row["version"]) for row in rows]

    def current_schema_version(self) -> int:
        return max(self.migration_versions(), default=0)

    def fetch_all(self, table: str) -> list[dict[str, Any]]:
        if table not in self.table_names():
            raise ValueError(f"Unknown table: {table}")
        with self.connect() as connection:
            columns = self._column_names(connection, table)
            if "id" in columns:
                order_column = "id"
            elif "version" in columns:
                order_column = "version"
            else:
                order_column = "rowid"
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY {order_column}").fetchall()
        return [dict(row) for row in rows]

    def fetch_by_created_date_et(self, table: str, date_et: str) -> list[dict[str, Any]]:
        """Fetch rows created on an America/New_York calendar date."""

        if table not in self.table_names():
            raise ValueError(f"Unknown table: {table}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_et):
            raise ValueError("date_et must use YYYY-MM-DD format.")
        with self.connect() as connection:
            columns = self._column_names(connection, table)
            if "created_date_et" not in columns:
                raise ValueError(f"Table does not support ET date filtering: {table}")
            if "id" in columns:
                order_column = "id"
            elif "version" in columns:
                order_column = "version"
            else:
                order_column = "rowid"
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE created_date_et = ? ORDER BY {order_column}",
                (date_et,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self, table: str) -> int:
        if table not in self.table_names():
            raise ValueError(f"Unknown table: {table}")
        with self.connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"])

    def save_work_item(self, work_item: WorkItem) -> str:
        """Upsert one WorkItem payload."""

        item = WorkItem.model_validate(_as_dict(work_item)).touch()
        created = _time_metadata()
        existing = self.get_work_item(item.id)
        target_json = stable_json(item.target.model_dump(mode="json"))
        payload_json = stable_json(item.model_dump(mode="json"))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO work_items
                    (
                        id, kind, status, title, request_text, target_json,
                        current_route, work_item_json, confidence, created_at_utc,
                        created_at_et, created_date_et, updated_at_utc, last_agent,
                        archived
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    status = excluded.status,
                    title = excluded.title,
                    request_text = excluded.request_text,
                    target_json = excluded.target_json,
                    current_route = excluded.current_route,
                    work_item_json = excluded.work_item_json,
                    confidence = excluded.confidence,
                    updated_at_utc = excluded.updated_at_utc,
                    last_agent = excluded.last_agent,
                    archived = excluded.archived
                """,
                (
                    item.id,
                    item.kind.value,
                    item.status.value,
                    _redact_string(item.title),
                    _redact_string(item.request_text),
                    target_json,
                    item.current_route.value,
                    payload_json,
                    item.confidence,
                    existing.created_at if existing is not None else created["utc"],
                    created["et"] if existing is None else "",
                    created["date_et"] if existing is None else "",
                    item.updated_at,
                    item.last_agent,
                    int(item.status == WorkItemStatus.ARCHIVED),
                ),
            )
        return item.id

    def get_work_item(self, work_item_id: str) -> WorkItem | None:
        """Load one WorkItem by id."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT work_item_json FROM work_items WHERE id = ?",
                (work_item_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkItem.model_validate(_json_dict(row["work_item_json"]))

    def list_work_items(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[WorkItem]:
        """List recent WorkItems."""

        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT work_item_json FROM work_items {where} "
                "ORDER BY updated_at_utc DESC, id DESC LIMIT ?",
                (*params, max(1, min(500, int(limit)))),
            ).fetchall()
        return [WorkItem.model_validate(_json_dict(row["work_item_json"])) for row in rows]

    def save_work_item_event(self, work_item_id: str, event: WorkItemEvent) -> int:
        """Append an audit event for a WorkItem."""

        event = WorkItemEvent.model_validate(_as_dict(event))
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO work_item_events
                    (
                        work_item_id, event_type, actor, summary, metadata_json,
                        created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_item_id,
                    event.event_type,
                    event.actor,
                    _redact_string(event.summary),
                    stable_json(event.metadata),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_work_item_events(self, work_item_id: str) -> list[WorkItemEvent]:
        """Return WorkItem events in insertion order."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_item_events WHERE work_item_id = ? ORDER BY id",
                (work_item_id,),
            ).fetchall()
        return [
            WorkItemEvent(
                event_type=str(row["event_type"]),
                actor=str(row["actor"]),
                summary=str(row["summary"]),
                metadata=_json_dict(row["metadata_json"]),
                created_at=str(row["created_at_utc"] or row["created_at"]),
            )
            for row in rows
        ]

    def save_work_item_artifact(
        self,
        work_item_id: str,
        artifact: WorkItemArtifactRef,
    ) -> int:
        """Attach one artifact reference to a WorkItem."""

        artifact = WorkItemArtifactRef.model_validate(_as_dict(artifact))
        metadata = {**artifact.metadata, "selected": artifact.selected}
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO work_item_artifacts
                    (
                        work_item_id, artifact_type, artifact_id, source_agent,
                        approval_state, title, summary, metadata_json,
                        created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_item_id,
                    artifact.artifact_type,
                    artifact.artifact_id,
                    artifact.source_agent,
                    artifact.approval_state,
                    _redact_string(artifact.title),
                    _redact_string(artifact.summary),
                    stable_json(metadata),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_work_item_artifacts(self, work_item_id: str) -> list[WorkItemArtifactRef]:
        """Return artifact refs attached to a WorkItem."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_item_artifacts WHERE work_item_id = ? ORDER BY id",
                (work_item_id,),
            ).fetchall()
        return [
            WorkItemArtifactRef(
                artifact_type=str(row["artifact_type"]),
                artifact_id=str(row["artifact_id"]),
                source_agent=str(row["source_agent"]),
                approval_state=str(row["approval_state"]),
                title=str(row["title"]),
                summary=str(row["summary"]),
                metadata=_json_dict(row["metadata_json"]),
                selected=bool(_json_dict(row["metadata_json"]).get("selected", False)),
                created_at=str(row["created_at_utc"] or row["created_at"]),
            )
            for row in rows
        ]

    def save_automation_spec(self, spec: AutomationSpec) -> str:
        """Upsert an automation spec."""

        item = AutomationSpec.model_validate(_as_dict(spec))
        created = _time_metadata()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM automation_specs WHERE id = ?",
                (item.id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO automation_specs
                    (
                        id, name, status, trigger_type, workflow, target_agent,
                        default_channel, spec_json, created_at_utc, created_at_et,
                        created_date_et, updated_at_utc
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    status = excluded.status,
                    trigger_type = excluded.trigger_type,
                    workflow = excluded.workflow,
                    target_agent = excluded.target_agent,
                    default_channel = excluded.default_channel,
                    spec_json = excluded.spec_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    item.id,
                    _redact_string(item.name),
                    item.status.value,
                    item.trigger_type.value,
                    _redact_string(item.workflow),
                    _redact_string(item.target_agent),
                    _redact_string(item.default_channel),
                    stable_json(item.model_dump(mode="json")),
                    created["utc"] if existing is None else item.created_at,
                    created["et"] if existing is None else "",
                    created["date_et"] if existing is None else "",
                    item.updated_at,
                ),
            )
        return item.id

    def get_automation_spec(self, automation_id: str) -> AutomationSpec | None:
        """Load one automation spec by id."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT spec_json FROM automation_specs WHERE id = ?",
                (automation_id,),
            ).fetchone()
        if row is None:
            return None
        return AutomationSpec.model_validate(_json_dict(row["spec_json"]))

    def list_automation_specs(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[AutomationSpec]:
        """List automation specs."""

        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT spec_json FROM automation_specs {where} ORDER BY name LIMIT ?",
                (*params, max(1, min(500, int(limit)))),
            ).fetchall()
        return [AutomationSpec.model_validate(_json_dict(row["spec_json"])) for row in rows]

    def save_automation_channel_binding(self, binding: AutomationChannelBinding) -> str:
        """Upsert an automation channel binding."""

        item = AutomationChannelBinding.model_validate(_as_dict(binding))
        created = _time_metadata()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM automation_channel_bindings WHERE id = ?",
                (item.id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO automation_channel_bindings
                    (
                        id, automation_id, channel_id, channel_name, destination_type,
                        purpose, binding_json, created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    automation_id = excluded.automation_id,
                    channel_id = excluded.channel_id,
                    channel_name = excluded.channel_name,
                    destination_type = excluded.destination_type,
                    purpose = excluded.purpose,
                    binding_json = excluded.binding_json
                """,
                (
                    item.id,
                    item.automation_id,
                    _redact_string(item.channel_id),
                    _redact_string(item.channel_name),
                    item.destination_type,
                    item.purpose,
                    stable_json(item.model_dump(mode="json")),
                    created["utc"] if existing is None else item.created_at,
                    created["et"] if existing is None else "",
                    created["date_et"] if existing is None else "",
                ),
            )
        return item.id

    def list_automation_channel_bindings(
        self,
        *,
        automation_id: str | None = None,
        channel: str | None = None,
        limit: int = 100,
    ) -> list[AutomationChannelBinding]:
        """List automation channel bindings."""

        clauses: list[str] = []
        params: list[Any] = []
        if automation_id:
            clauses.append("automation_id = ?")
            params.append(automation_id)
        if channel:
            clauses.append("(channel_name = ? OR channel_id = ?)")
            params.extend([channel.lstrip("#"), channel])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT binding_json FROM automation_channel_bindings {where} "
                "ORDER BY channel_name, id LIMIT ?",
                (*params, max(1, min(500, int(limit)))),
            ).fetchall()
        return [
            AutomationChannelBinding.model_validate(_json_dict(row["binding_json"])) for row in rows
        ]

    def save_automation_run(self, run: AutomationRun) -> str:
        """Save one automation run."""

        item = AutomationRun.model_validate(_as_dict(run))
        created = _time_metadata()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_runs
                    (
                        id, automation_id, automation_name, stage, status,
                        work_item_id, channel, approval_count, failure_summary,
                        next_safe_action, run_json, created_at_utc, created_at_et,
                        created_date_et, completed_at_utc
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    automation_id = excluded.automation_id,
                    automation_name = excluded.automation_name,
                    stage = excluded.stage,
                    status = excluded.status,
                    work_item_id = excluded.work_item_id,
                    channel = excluded.channel,
                    approval_count = excluded.approval_count,
                    failure_summary = excluded.failure_summary,
                    next_safe_action = excluded.next_safe_action,
                    run_json = excluded.run_json,
                    completed_at_utc = excluded.completed_at_utc
                """,
                (
                    item.id,
                    item.automation_id,
                    _redact_string(item.automation_name),
                    _redact_string(item.stage),
                    item.status.value,
                    item.work_item_id,
                    _redact_string(item.channel),
                    item.approval_count,
                    _redact_string(item.failure_summary),
                    _redact_string(item.next_safe_action),
                    stable_json(item.model_dump(mode="json"), summarize_email_content=True),
                    item.started_at or created["utc"],
                    created["et"],
                    created["date_et"],
                    item.completed_at,
                ),
            )
        return item.id

    def list_automation_runs(
        self,
        *,
        automation_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[AutomationRun]:
        """List recent automation runs."""

        clauses: list[str] = []
        params: list[Any] = []
        if automation_id:
            clauses.append("automation_id = ?")
            params.append(automation_id)
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT run_json FROM automation_runs {where} "
                "ORDER BY created_at_utc DESC, id DESC LIMIT ?",
                (*params, max(1, min(500, int(limit)))),
            ).fetchall()
        return [AutomationRun.model_validate(_json_dict(row["run_json"])) for row in rows]

    def save_automation_finding(self, finding: AutomationFinding) -> str:
        """Save or update one automation finding."""

        item = AutomationFinding.model_validate(_as_dict(finding))
        created = _time_metadata()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_findings
                    (
                        id, automation_id, run_id, channel, finding_type, severity,
                        title, summary, recommendation, finding_json, created_at_utc,
                        created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    automation_id = excluded.automation_id,
                    run_id = excluded.run_id,
                    channel = excluded.channel,
                    finding_type = excluded.finding_type,
                    severity = excluded.severity,
                    title = excluded.title,
                    summary = excluded.summary,
                    recommendation = excluded.recommendation,
                    finding_json = excluded.finding_json
                """,
                (
                    item.id,
                    item.automation_id,
                    item.run_id,
                    _redact_string(item.channel),
                    item.finding_type,
                    item.severity.value,
                    _redact_string(item.title),
                    _redact_string(item.summary),
                    _redact_string(item.recommendation),
                    stable_json(item.model_dump(mode="json")),
                    item.created_at or created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
        return item.id

    def list_automation_findings(
        self,
        *,
        automation_id: str | None = None,
        run_id: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[AutomationFinding]:
        """List automation findings."""

        clauses: list[str] = []
        params: list[Any] = []
        if automation_id:
            clauses.append("automation_id = ?")
            params.append(automation_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if severity and severity != "all":
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT finding_json FROM automation_findings {where} "
                "ORDER BY created_at_utc DESC, id DESC LIMIT ?",
                (*params, max(1, min(500, int(limit)))),
            ).fetchall()
        return [AutomationFinding.model_validate(_json_dict(row["finding_json"])) for row in rows]

    def save(self, kind: str, payload: dict[str, Any]) -> int:
        """Backward-compatible generic save, stored as an agent run."""

        return self.save_agent_run(
            agent_name=f"generic:{kind}",
            input_payload={"kind": kind},
            input_summary=kind,
            output=payload,
            status="saved",
            dry_run=True,
        )

    def list_records(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.fetch_all("agent_runs")
        if kind is None:
            return rows
        prefix = f"generic:{kind}"
        return [row for row in rows if row["agent_name"] == prefix]

    def save_tool_event(
        self,
        *,
        tool_name: str,
        agent_name: str | None = None,
        run_id: str | int | None = None,
        input_payload: Any | None = None,
        input_hash: str | None = None,
        input_summary: str = "",
        output: Any | None = None,
        output_hash: str | None = None,
        output_summary: str = "",
        dry_run: bool = True,
        status: str = "success",
        error: str | None = None,
        timestamp: datetime | str | None = None,
    ) -> int:
        """Save a redacted, reconstructable audit event for a tool invocation."""

        safe_input = _audit_safe_value(
            input_payload if input_payload is not None else input_summary
        )
        safe_output = _audit_safe_value(output if output is not None else output_summary)
        resolved_input_summary = input_summary or summarize_tool_event_payload(input_payload)
        resolved_output_summary = output_summary or summarize_tool_event_payload(output)
        resolved_input_hash = input_hash or stable_hash(safe_input)
        resolved_output_hash = output_hash or (
            stable_hash(safe_output) if output is not None or output_summary else ""
        )
        created = _time_metadata(timestamp)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_events
                    (
                        tool_name, agent_name, run_id, input_hash, input_summary,
                        output_hash, output_summary, dry_run, status, error,
                        created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(tool_name),
                    _redact_string(agent_name or ""),
                    _redact_string(str(run_id)) if run_id is not None else None,
                    _redact_string(resolved_input_hash),
                    summarize_tool_event_payload(resolved_input_summary),
                    _redact_string(resolved_output_hash),
                    summarize_tool_event_payload(resolved_output_summary),
                    int(bool(dry_run)),
                    _redact_string(status),
                    summarize_tool_event_payload(error) if error else None,
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_tool_events(
        self,
        *,
        tool_name: str | None = None,
        agent_name: str | None = None,
        run_id: str | int | None = None,
        status: str | None = None,
        dry_run: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List saved tool audit events with optional exact-match filters."""

        conditions: list[str] = []
        params: list[str | int] = []
        for column, value in (
            ("tool_name", tool_name),
            ("agent_name", agent_name),
            ("run_id", str(run_id) if run_id is not None else None),
            ("status", status),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(_redact_string(str(value)))
        if dry_run is not None:
            conditions.append("dry_run = ?")
            params.append(int(bool(dry_run)))

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tool_events {where} ORDER BY id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_agent_run_log(
        self,
        *,
        step_name: str,
        agent_name: str | None = None,
        run_id: str | int | None = None,
        input_payload: Any | None = None,
        input_hash: str | None = None,
        input_summary: str = "",
        output: Any | None = None,
        output_hash: str | None = None,
        output_summary: str = "",
        dry_run: bool = True,
        status: str = "success",
        error: str | None = None,
        timestamp: datetime | str | None = None,
    ) -> int:
        """Save a redacted, reconstructable agent step log."""

        safe_input = _audit_safe_value(
            input_payload if input_payload is not None else input_summary
        )
        safe_output = _audit_safe_value(output if output is not None else output_summary)
        resolved_input_summary = input_summary or summarize_tool_event_payload(input_payload)
        resolved_output_summary = output_summary or summarize_tool_event_payload(output)
        resolved_input_hash = input_hash or stable_hash(safe_input)
        resolved_output_hash = output_hash or (
            stable_hash(safe_output) if output is not None or output_summary else ""
        )
        created = _time_metadata(timestamp)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_run_logs
                    (
                        run_id, step_name, agent_name, input_hash, input_summary,
                        output_hash, output_summary, dry_run, status, error,
                        created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(str(run_id)) if run_id is not None else None,
                    _redact_string(step_name),
                    _redact_string(agent_name or ""),
                    _redact_string(resolved_input_hash),
                    summarize_tool_event_payload(resolved_input_summary),
                    _redact_string(resolved_output_hash),
                    summarize_tool_event_payload(resolved_output_summary),
                    int(bool(dry_run)),
                    _redact_string(status),
                    summarize_tool_event_payload(error) if error else None,
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_agent_run_logs(
        self,
        *,
        run_id: str | int | None = None,
        step_name: str | None = None,
        agent_name: str | None = None,
        status: str | None = None,
        dry_run: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List saved agent step logs with optional exact-match filters."""

        conditions: list[str] = []
        params: list[str | int] = []
        for column, value in (
            ("run_id", str(run_id) if run_id is not None else None),
            ("step_name", step_name),
            ("agent_name", agent_name),
            ("status", status),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(_redact_string(str(value)))
        if dry_run is not None:
            conditions.append("dry_run = ?")
            params.append(int(bool(dry_run)))

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_run_logs {where} ORDER BY id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_feedback(
        self,
        feedback: Any | None = None,
        *,
        object_type: str | None = None,
        object_id: str | int | None = None,
        approval_id: str | None = None,
        rating: str | None = None,
        tags: list[str] | None = None,
        risk_flags: list[str] | None = None,
        source_agent: str | None = None,
        review_stage: str | None = None,
        notes: str = "",
        created_at: str | None = None,
    ) -> int:
        """Save human feedback on an agent output."""

        payload = _as_dict(feedback) if feedback is not None else {}
        updates = {
            "object_type": object_type,
            "object_id": object_id,
            "approval_id": approval_id,
            "rating": rating,
            "tags": tags,
            "risk_flags": risk_flags,
            "source_agent": source_agent,
            "review_stage": review_stage,
            "notes": notes if notes else None,
            "created_at": created_at,
        }
        payload.update({key: value for key, value in updates.items() if value is not None})
        record = FeedbackRecord.model_validate(payload)
        created = _time_metadata(record.created_at)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO feedback
                    (
                        object_type, object_id, approval_id, rating, tags_json, notes,
                        risk_flags_json, source_agent, review_stage, created_at_utc,
                        created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.object_type),
                    _redact_string(record.object_id),
                    _redact_string(record.approval_id or ""),
                    _redact_string(record.rating),
                    stable_json(record.tags),
                    _redact_string(record.notes),
                    stable_json(record.risk_flags),
                    _redact_string(record.source_agent),
                    _redact_string(record.review_stage),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_feedback(
        self,
        *,
        object_type: str | None = None,
        rating: str | None = None,
        tag: str | None = None,
        approval_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List human feedback records with optional filters."""

        conditions: list[str] = []
        params: list[str] = []
        if object_type:
            conditions.append("object_type = ?")
            params.append(object_type)
        if rating:
            conditions.append("rating = ?")
            params.append(rating)
        if approval_id:
            conditions.append("approval_id = ?")
            params.append(approval_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM feedback {where} ORDER BY id",
                params,
            ).fetchall()

        records = [self._feedback_row_to_dict(dict(row)) for row in rows]
        if tag:
            normalized_tag = str(tag).strip().lower().replace(" ", "_")
            records = [record for record in records if normalized_tag in record["tags"]]
        return records

    def summarize_feedback_by_tag(
        self,
        *,
        object_type: str | None = None,
        rating: str | None = None,
    ) -> dict[str, int]:
        """Return feedback tag counts in descending frequency order."""

        counts: dict[str, int] = {}
        for record in self.list_feedback(object_type=object_type, rating=rating):
            for tag in record["tags"]:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _feedback_row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            tags = json.loads(str(row.get("tags_json") or "[]"))
        except json.JSONDecodeError:
            tags = []
        if not isinstance(tags, list):
            tags = []
        try:
            risk_flags = json.loads(str(row.get("risk_flags_json") or "[]"))
        except json.JSONDecodeError:
            risk_flags = []
        if not isinstance(risk_flags, list):
            risk_flags = []
        return {
            "id": int(row["id"]) if row.get("id") is not None else None,
            "object_type": str(row.get("object_type") or ""),
            "object_id": str(row.get("object_id") or ""),
            "approval_id": str(row.get("approval_id") or "") or None,
            "source_agent": str(row.get("source_agent") or ""),
            "review_stage": str(row.get("review_stage") or "approval_review"),
            "rating": str(row.get("rating") or ""),
            "tags": [str(tag) for tag in tags if str(tag).strip()],
            "risk_flags": [str(flag) for flag in risk_flags if str(flag).strip()],
            "notes": str(row.get("notes") or ""),
            "created_at": str(row.get("created_at_utc") or row.get("created_at") or ""),
        }

    def save_agent_run(
        self,
        *,
        agent_name: str,
        input_payload: Any | None = None,
        input_hash: str | None = None,
        input_summary: str = "",
        output: Any | None = None,
        output_json: str | None = None,
        model: str = "",
        dry_run: bool = True,
        status: str = "success",
        error: str | None = None,
    ) -> int:
        resolved_hash = input_hash or stable_hash(input_payload or input_summary)
        resolved_output = (
            output_json
            if output_json is not None
            else stable_json(output or {}, summarize_email_content=True)
        )
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_runs
                    (
                        agent_name, input_hash, input_summary, output_json, model,
                        dry_run, status, error, created_at_utc, created_at_et,
                        created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(agent_name),
                    resolved_hash,
                    _redact_string(input_summary),
                    _redact_string(resolved_output),
                    _redact_string(model),
                    int(bool(dry_run)),
                    _redact_string(status),
                    _redact_string(error) if error else None,
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def save_email(self, triage: Any, *, gmail_message_id: str | None = None) -> int:
        payload = _as_dict(triage)
        safe_payload = redact_secrets(payload, summarize_email_content=True)
        if isinstance(safe_payload, dict) and "draft_reply_summary" in safe_payload:
            safe_payload["draft_reply_preview"] = safe_payload.pop("draft_reply_summary")
        if isinstance(safe_payload, dict) and safe_payload.get("draft_reply"):
            draft_reply = str(safe_payload.pop("draft_reply"))
            safe_payload["draft_reply_hash"] = stable_hash(draft_reply)
            safe_payload["draft_reply_preview"] = _redact_string(
                " ".join(draft_reply.split())[:EMAIL_BODY_SUMMARY_CHARS]
            )
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO emails
                    (
                        gmail_message_id, sender_email, subject, category, needs_reply,
                        urgency, triage_json, created_at_utc, created_at_et,
                        created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(str(gmail_message_id or payload.get("message_id") or "")),
                    _redact_string(str(payload.get("sender_email") or "")),
                    _redact_string(str(payload.get("subject") or "")),
                    _redact_string(str(payload.get("category") or "")),
                    int(bool(payload.get("needs_reply"))),
                    _redact_string(str(payload.get("priority") or payload.get("urgency") or "")),
                    stable_json(safe_payload),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def save_contact(self, contact: Any) -> int:
        """Save one local contact context record without any live CRM call."""

        record = ContactRecord.model_validate(_as_dict(contact))
        payload = record.model_dump(mode="json")
        safe_notes = _redact_sensitive_personal_note(record.notes)
        payload["notes"] = safe_notes
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO contacts
                    (
                        company_name, contact_name, role_title, contact_email,
                        linkedin_url, source, source_id, source_url, confidence,
                        approval_state, approval_scope, notes, contact_json,
                        created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.company_name),
                    _redact_string(record.contact_name),
                    _redact_string(record.role_title),
                    _redact_string(record.contact_email) if record.contact_email else None,
                    _redact_string(record.linkedin_url) if record.linkedin_url else None,
                    _redact_string(record.source),
                    _redact_string(record.source_id),
                    _redact_string(record.source_url),
                    record.confidence,
                    _redact_string(record.approval_state.value),
                    _redact_string(record.approval_scope.value),
                    safe_notes,
                    stable_json(payload),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_contacts(
        self,
        *,
        company_name: str | None = None,
        approved_only: bool = False,
    ) -> list[ContactRecord]:
        """Load local contact context records from SQLite."""

        conditions: list[str] = []
        params: list[str] = []
        if company_name:
            conditions.append("company_name = ?")
            params.append(_redact_string(company_name))
        if approved_only:
            conditions.append("approval_state = ?")
            params.append(ApprovalState.APPROVED_FOR_DRAFTING.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT contact_json FROM contacts {where} ORDER BY id",
                params,
            ).fetchall()
        return [ContactRecord.model_validate(_json_dict(row["contact_json"])) for row in rows]

    def save_crm_account_context(self, context: Any) -> int:
        """Save one local CRM/account context record without any live CRM call."""

        record = CRMAccountContext.model_validate(_as_dict(context))
        payload = record.model_dump(mode="json")
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO crm_contexts
                    (
                        company_name, account_stage, account_owner, source, source_id,
                        source_url, confidence, approval_state, approval_scope, notes,
                        context_json, created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.company_name),
                    _redact_string(record.account_stage),
                    _redact_string(record.account_owner),
                    _redact_string(record.source),
                    _redact_string(record.source_id),
                    _redact_string(record.source_url),
                    record.confidence,
                    _redact_string(record.approval_state.value),
                    _redact_string(record.approval_scope.value),
                    _redact_string(record.notes),
                    stable_json(payload),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_crm_account_contexts(
        self,
        *,
        company_name: str | None = None,
        approved_only: bool = False,
    ) -> list[CRMAccountContext]:
        """Load local CRM/account context records from SQLite."""

        conditions: list[str] = []
        params: list[str] = []
        if company_name:
            conditions.append("company_name = ?")
            params.append(_redact_string(company_name))
        if approved_only:
            conditions.append("approval_state = ?")
            params.append(ApprovalState.APPROVED_FOR_DRAFTING.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT context_json FROM crm_contexts {where} ORDER BY id",
                params,
            ).fetchall()
        return [CRMAccountContext.model_validate(_json_dict(row["context_json"])) for row in rows]

    def save_email_style_profile(self, profile: Any) -> int:
        """Save one aggregate email style profile without storing raw sent-email bodies."""

        record = EmailStyleProfile.model_validate(_as_dict(profile))
        payload = record.model_dump(mode="json")
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO email_style_profiles
                    (
                        profile_id, source, source_id, source_url, confidence,
                        approval_state, approval_scope, profile_json,
                        created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.profile_id),
                    _redact_string(record.source),
                    _redact_string(record.source_id),
                    _redact_string(record.source_url),
                    record.confidence,
                    _redact_string(record.approval_state.value),
                    _redact_string(record.approval_scope.value),
                    stable_json(payload, summarize_email_content=True),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_email_style_profiles(
        self,
        *,
        profile_id: str | None = None,
        approved_only: bool = False,
    ) -> list[EmailStyleProfile]:
        """Load aggregate email style profiles from local SQLite storage."""

        conditions: list[str] = []
        params: list[str] = []
        if profile_id:
            conditions.append("profile_id = ?")
            params.append(_redact_string(profile_id))
        if approved_only:
            conditions.append("approval_state = ?")
            params.append(ApprovalState.APPROVED_FOR_DRAFTING.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT profile_json FROM email_style_profiles {where} ORDER BY id",
                params,
            ).fetchall()
        return [EmailStyleProfile.model_validate(_json_dict(row["profile_json"])) for row in rows]

    def save_memory_item(self, item: Any) -> int:
        """Save one approved or pending local memory item and update its search index."""

        record = MemoryItem.model_validate(_as_dict(item))
        created = _time_metadata(record.created_at)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_items
                    (
                        memory_type, object_type, object_id, object_key, title, summary,
                        content_json, source_ids_json, approval_state, confidence,
                        sensitivity, safe_for_prompt, expires_at, supersedes_memory_id,
                        metadata_json, memory_json, created_at_utc, created_at_et,
                        created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.memory_type),
                    _redact_string(record.object_type),
                    _redact_string(record.object_id),
                    _redact_string(record.object_key),
                    _redact_string(record.title),
                    _redact_string(record.summary),
                    stable_json(record.content, summarize_email_content=True),
                    stable_json(record.source_ids),
                    _redact_string(record.approval_state.value),
                    record.confidence,
                    _redact_string(record.sensitivity),
                    1 if record.safe_for_prompt else 0,
                    _redact_string(record.expires_at or "") or None,
                    record.supersedes_memory_id,
                    stable_json(record.metadata, summarize_email_content=True),
                    "{}",
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            row_id = int(cursor.lastrowid)
            stored = record.model_copy(update={"id": row_id, "created_at": created["utc"]})
            stored_payload = stored.model_dump(mode="json")
            stored_payload.pop("secrets_included", None)
            connection.execute(
                "UPDATE memory_items SET memory_json = ? WHERE id = ?",
                (stable_json(stored_payload, summarize_email_content=True), row_id),
            )
            self._upsert_memory_index(connection, stored)
            return row_id

    def _upsert_memory_index(
        self,
        connection: sqlite3.Connection,
        record: MemoryItem,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_index
                (
                    memory_id, memory_type, object_key, title, summary, safe_text,
                    source_ids_json, updated_at_utc
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                memory_type = excluded.memory_type,
                object_key = excluded.object_key,
                title = excluded.title,
                summary = excluded.summary,
                safe_text = excluded.safe_text,
                source_ids_json = excluded.source_ids_json,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                record.id,
                _redact_string(record.memory_type),
                _redact_string(record.object_key),
                _redact_string(record.title),
                _redact_string(record.summary),
                _redact_string(record.index_text()),
                stable_json(record.source_ids),
                _time_metadata()["utc"],
            ),
        )

    def list_memory_items(
        self,
        *,
        memory_type: str | None = None,
        object_type: str | None = None,
        object_key: str | None = None,
        approved_only: bool = False,
        safe_for_prompt: bool | None = None,
    ) -> list[MemoryItem]:
        """List local memory records with optional safety and object filters."""

        conditions: list[str] = []
        params: list[Any] = []
        if memory_type:
            conditions.append("memory_type = ?")
            params.append(_redact_string(memory_type))
        if object_type:
            conditions.append("object_type = ?")
            params.append(_redact_string(object_type))
        if object_key:
            conditions.append("object_key = ?")
            params.append(_redact_string(normalize_memory_key(object_key)))
        if approved_only:
            approved_states = [
                ApprovalState.APPROVED_FOR_RESEARCH.value,
                ApprovalState.APPROVED_FOR_DRAFTING.value,
                ApprovalState.APPROVED_FOR_EXTERNAL_USE.value,
                ApprovalState.APPROVED_FOR_SEND.value,
            ]
            conditions.append(f"approval_state IN ({', '.join('?' for _ in approved_states)})")
            params.extend(approved_states)
        if safe_for_prompt is not None:
            conditions.append("safe_for_prompt = ?")
            params.append(1 if safe_for_prompt else 0)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_items {where} ORDER BY id",
                params,
            ).fetchall()
        return [self._memory_item_from_row(dict(row)) for row in rows]

    def retrieve_memory(
        self,
        query: str = "",
        *,
        object_key: str | None = None,
        memory_types: list[str] | None = None,
        limit: int = 5,
        approved_only: bool = True,
        safe_for_prompt: bool = True,
    ) -> list[MemoryItem]:
        """Retrieve prompt-safe local memory with deterministic lexical ranking."""

        conditions: list[str] = []
        params: list[Any] = []
        if object_key:
            conditions.append("i.object_key = ?")
            params.append(_redact_string(normalize_memory_key(object_key)))
        if memory_types:
            resolved_types = [_redact_string(memory_type) for memory_type in memory_types]
            conditions.append(f"i.memory_type IN ({', '.join('?' for _ in resolved_types)})")
            params.extend(resolved_types)
        if approved_only:
            approved_states = [
                ApprovalState.APPROVED_FOR_RESEARCH.value,
                ApprovalState.APPROVED_FOR_DRAFTING.value,
                ApprovalState.APPROVED_FOR_EXTERNAL_USE.value,
                ApprovalState.APPROVED_FOR_SEND.value,
            ]
            conditions.append(f"m.approval_state IN ({', '.join('?' for _ in approved_states)})")
            params.extend(approved_states)
        if safe_for_prompt:
            conditions.append("m.safe_for_prompt = 1")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, i.title AS index_title, i.summary AS index_summary,
                       i.safe_text AS index_safe_text
                FROM memory_items m
                JOIN memory_index i ON i.memory_id = m.id
                """
                + f" {where} ORDER BY m.id",
                params,
            ).fetchall()

        tokens = _memory_query_tokens(query)
        ranked: list[tuple[int, float, int, MemoryItem]] = []
        normalized_object_key = normalize_memory_key(object_key or "")
        for row in rows:
            row_dict = dict(row)
            item = self._memory_item_from_row(row_dict)
            score = _score_memory_row(
                tokens=tokens,
                object_key=normalized_object_key,
                row=row_dict,
            )
            if tokens and score <= 0:
                continue
            ranked.append((score, item.confidence, item.id or 0, item))

        ranked.sort(key=lambda value: (-value[0], -value[1], -value[2]))
        bounded_limit = max(0, min(int(limit or 5), 25))
        return [item for _, _, _, item in ranked[:bounded_limit]]

    def save_outreach_example_document(self, document: Any) -> str:
        """Save one sanitized outreach example and update its local FTS index."""

        record = OutreachExampleDocument.model_validate(_as_dict(document))
        if record.raw_body_included:
            raise ValueError("outreach examples must not store raw Gmail bodies.")
        payload = record.model_dump(mode="json")
        created = _time_metadata(record.created_at)
        retrieval_text = _redact_sensitive_personal_note(record.safe_retrieval_text())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO outreach_examples
                    (
                        example_id, source_thread_id_hash, source_label, channel,
                        outreach_stage, outcome, company_type, opportunity_type,
                        template_id, style_profile_id, approved_for_drafting,
                        raw_body_included, retrieval_text, document_json,
                        created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(example_id) DO UPDATE SET
                    source_thread_id_hash = excluded.source_thread_id_hash,
                    source_label = excluded.source_label,
                    channel = excluded.channel,
                    outreach_stage = excluded.outreach_stage,
                    outcome = excluded.outcome,
                    company_type = excluded.company_type,
                    opportunity_type = excluded.opportunity_type,
                    template_id = excluded.template_id,
                    style_profile_id = excluded.style_profile_id,
                    approved_for_drafting = excluded.approved_for_drafting,
                    raw_body_included = excluded.raw_body_included,
                    retrieval_text = excluded.retrieval_text,
                    document_json = excluded.document_json,
                    created_at_utc = excluded.created_at_utc,
                    created_at_et = excluded.created_at_et,
                    created_date_et = excluded.created_date_et
                """,
                (
                    _redact_string(record.example_id),
                    _redact_string(record.source_thread_id_hash),
                    _redact_string(record.source_label),
                    _redact_string(record.channel),
                    _redact_string(record.outreach_stage),
                    _redact_string(record.outcome),
                    _redact_string(record.company_type),
                    _redact_string(record.opportunity_type),
                    _redact_string(record.template_id or "") or None,
                    _redact_string(record.style_profile_id or "") or None,
                    int(record.approved_for_drafting),
                    0,
                    retrieval_text,
                    stable_json(payload, summarize_email_content=True),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            connection.execute(
                "DELETE FROM outreach_example_fts WHERE example_id = ?",
                (_redact_string(record.example_id),),
            )
            connection.execute(
                "INSERT INTO outreach_example_fts (example_id, retrieval_text) VALUES (?, ?)",
                (_redact_string(record.example_id), retrieval_text),
            )
        return record.example_id

    def list_outreach_example_documents(
        self,
        *,
        approved_only: bool = False,
        company_type: str | None = None,
        opportunity_type: str | None = None,
        limit: int | None = None,
    ) -> list[OutreachExampleDocument]:
        """List sanitized outreach examples. Retrieval should use approved records only."""

        conditions: list[str] = []
        params: list[str | int] = []
        if approved_only:
            conditions.append("approved_for_drafting = 1")
        if company_type:
            conditions.append("company_type = ?")
            params.append(_redact_string(company_type))
        if opportunity_type:
            conditions.append("opportunity_type = ?")
            params.append(_redact_string(opportunity_type))
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(int(limit), 0))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT document_json FROM outreach_examples {where} ORDER BY id{limit_clause}",
                params,
            ).fetchall()
        return [
            OutreachExampleDocument.model_validate(_json_dict(row["document_json"])) for row in rows
        ]

    def retrieve_outreach_examples(
        self,
        query: str = "",
        *,
        company_type: str | None = None,
        opportunity_type: str | None = None,
        limit: int = 5,
        approved_only: bool = True,
    ) -> OutreachExampleRetrievalResult:
        """Retrieve approved sanitized outreach examples using local SQLite FTS first."""

        if not approved_only:
            raise ValueError("Outreach example retrieval requires approved_only=True.")
        bounded_limit = max(0, min(int(limit or 5), 25))
        tokens = _fts_query_tokens(query)
        match_query = _fts_match_query(query)
        conditions = ["approved_for_drafting = 1"]
        params: list[Any] = []
        if company_type:
            conditions.append("company_type = ?")
            params.append(_redact_string(company_type))
        if opportunity_type:
            conditions.append("opportunity_type = ?")
            params.append(_redact_string(opportunity_type))
        fts_conditions = [f"e.{condition}" for condition in conditions]

        rows: list[dict[str, Any]] = []
        if match_query and bounded_limit:
            try:
                with self.connect() as connection:
                    fetched = connection.execute(
                        """
                        SELECT e.*, bm25(outreach_example_fts) AS fts_rank
                        FROM outreach_example_fts
                        JOIN outreach_examples e
                          ON e.example_id = outreach_example_fts.example_id
                        WHERE outreach_example_fts MATCH ?
                        """
                        + f" AND {' AND '.join(fts_conditions)}"
                        + " ORDER BY fts_rank, e.id DESC LIMIT ?",
                        [match_query, *params, bounded_limit],
                    ).fetchall()
                rows = [dict(row) for row in fetched]
            except sqlite3.Error:
                rows = []

        if not rows and bounded_limit:
            where = f"WHERE {' AND '.join(conditions)}"
            with self.connect() as connection:
                fetched = connection.execute(
                    f"SELECT * FROM outreach_examples {where} ORDER BY id DESC",
                    params,
                ).fetchall()
            ranked: list[tuple[int, int, dict[str, Any]]] = []
            for row in fetched:
                row_dict = dict(row)
                score = _score_outreach_example_row(tokens, row_dict)
                if tokens and score <= 0:
                    continue
                ranked.append((score, int(row_dict.get("id") or 0), row_dict))
            ranked.sort(key=lambda item: (-item[0], -item[1]))
            rows = [row for _, _, row in ranked[:bounded_limit]]

        examples: list[OutreachExampleDocument] = []
        records: list[RetrievedOutreachExample] = []
        for row in rows[:bounded_limit]:
            try:
                document = OutreachExampleDocument.model_validate(
                    _json_dict(row.get("document_json"))
                )
                score = _score_outreach_example_row(tokens, row)
                reason = _outreach_example_match_reason(
                    tokens,
                    row,
                    company_type=company_type,
                    opportunity_type=opportunity_type,
                )
                record = _retrieved_outreach_example(
                    document,
                    match_score=score,
                    match_reason=reason,
                )
            except ValueError:
                continue
            examples.append(document)
            records.append(record)
        guidance = (
            "Use approved sanitized examples as style and structure guidance only: "
            + "; ".join(f"{record.example_id} ({record.match_reason})" for record in records)
            if records
            else "No approved sanitized outreach examples matched the request."
        )
        return OutreachExampleRetrievalResult(
            query=query,
            approved_only=True,
            examples=examples,
            records=records,
            guidance=guidance,
            raw_body_included=False,
            embeddings_used=False,
            external_vector_db_used=False,
            send_enabled=False,
        )

    def _memory_item_from_row(self, row: dict[str, Any]) -> MemoryItem:
        payload = _json_dict(row.get("memory_json"))
        if not payload:
            payload = {
                "id": row.get("id"),
                "memory_type": row.get("memory_type"),
                "object_type": row.get("object_type"),
                "object_id": row.get("object_id"),
                "object_key": row.get("object_key"),
                "title": row.get("title"),
                "summary": row.get("summary"),
                "content": _json_dict(row.get("content_json")),
                "source_ids": _json_list(row.get("source_ids_json")),
                "approval_state": row.get("approval_state"),
                "confidence": row.get("confidence") or 0,
                "sensitivity": row.get("sensitivity") or "internal",
                "safe_for_prompt": bool(row.get("safe_for_prompt")),
                "created_at": row.get("created_at_utc") or row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "supersedes_memory_id": row.get("supersedes_memory_id"),
                "metadata": _json_dict(row.get("metadata_json")),
            }
        return MemoryItem.model_validate(payload)

    def save_follow_up_schedule(self, schedule: Any) -> int:
        """Save one data-only follow-up recommendation without scheduling anything."""

        record = FollowUpScheduleRecord.model_validate(_as_dict(schedule))
        payload = record.model_dump(mode="json")
        created = _time_metadata(record.created_at)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO follow_up_schedules
                    (
                        company_name, contact_name, related_draft_id, proposed_date,
                        sequence_number, status, rationale, approval_required,
                        schedule_json, created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.company_name),
                    _redact_string(record.contact_name) if record.contact_name else None,
                    _redact_string(record.related_draft_id),
                    _redact_string(record.proposed_date),
                    record.sequence_number,
                    _redact_string(record.status),
                    _redact_string(record.rationale),
                    int(record.approval_required),
                    stable_json(payload),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_follow_up_schedules(
        self,
        *,
        company_name: str | None = None,
        related_draft_id: str | int | None = None,
        status: str | None = None,
    ) -> list[FollowUpScheduleRecord]:
        """Load local data-only follow-up recommendations from SQLite."""

        conditions: list[str] = []
        params: list[str] = []
        if company_name:
            conditions.append("company_name = ?")
            params.append(_redact_string(company_name))
        if related_draft_id is not None:
            conditions.append("related_draft_id = ?")
            params.append(_redact_string(str(related_draft_id)))
        if status:
            conditions.append("status = ?")
            params.append(_redact_string(status))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT schedule_json FROM follow_up_schedules {where} ORDER BY id",
                params,
            ).fetchall()
        return [
            FollowUpScheduleRecord.model_validate(_json_dict(row["schedule_json"])) for row in rows
        ]

    def save_outreach_tracking(self, tracking: Any) -> int:
        """Save one manual outreach lifecycle snapshot without sending anything."""

        record = OutreachTrackingRecord.model_validate(_as_dict(tracking))
        payload = record.model_dump(mode="json")
        created = _time_metadata(record.created_at)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO outreach_tracking
                    (
                        draft_id, company_name, contact_name, channel, lifecycle_status,
                        outreach_sent, sent_at, sent_by, sent_via, reply_received,
                        reply_received_at, reply_summary, outcome, outcome_notes,
                        next_step, last_checked_at, tracking_json, created_at_utc,
                        created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.draft_id),
                    _redact_string(record.company_name),
                    _redact_string(record.contact_name),
                    _redact_string(record.channel),
                    _redact_string(record.lifecycle_status),
                    int(record.outreach_sent),
                    _redact_string(record.sent_at),
                    _redact_string(record.sent_by),
                    _redact_string(record.sent_via),
                    int(record.reply_received),
                    _redact_string(record.reply_received_at),
                    _redact_string(record.reply_summary),
                    _redact_string(record.outcome),
                    _redact_string(record.outcome_notes),
                    _redact_string(record.next_step),
                    _redact_string(record.last_checked_at),
                    stable_json(payload),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def list_outreach_tracking(
        self,
        *,
        draft_id: str | int | None = None,
        company_name: str | None = None,
        lifecycle_status: str | None = None,
        outcome: str | None = None,
        limit: int | None = None,
    ) -> list[OutreachTrackingRecord]:
        """Load local manual outreach lifecycle snapshots from SQLite."""

        conditions: list[str] = []
        params: list[str | int] = []
        if draft_id is not None:
            conditions.append("draft_id = ?")
            params.append(_redact_string(str(draft_id)))
        if company_name:
            conditions.append("company_name = ?")
            params.append(_redact_string(company_name))
        if lifecycle_status:
            conditions.append("lifecycle_status = ?")
            params.append(_redact_string(lifecycle_status))
        if outcome:
            conditions.append("outcome = ?")
            params.append(_redact_string(outcome))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(int(limit), 0))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT tracking_json FROM outreach_tracking {where} ORDER BY id{limit_clause}",
                params,
            ).fetchall()
        return [
            OutreachTrackingRecord.model_validate(_json_dict(row["tracking_json"])) for row in rows
        ]

    def save_company(self, profile: Any) -> int:
        payload = _as_dict(profile)
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO companies
                    (
                        company_name, company_url, profile_json, consulting_fit_score,
                        confidence_score, created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(str(payload.get("name") or payload.get("company_name") or "")),
                    _redact_string(str(payload.get("website") or payload.get("company_url") or ""))
                    or None,
                    stable_json(payload),
                    payload.get("consulting_fit_score"),
                    payload.get("confidence_score"),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            company_id = int(cursor.lastrowid)
        for source in payload.get("sources") or []:
            source_payload = _as_dict(source) if not isinstance(source, Mapping) else dict(source)
            self.save_source(
                object_type="company",
                object_id=company_id,
                title=str(source_payload.get("title") or ""),
                url=str(source_payload.get("url") or ""),
                snippet="; ".join(
                    str(item) for item in source_payload.get("supported_claims") or []
                ),
            )
        return company_id

    def load_company_profile(self, company_id: int) -> CompanyProfile:
        """Load one saved company profile payload from SQLite."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT profile_json FROM companies WHERE id = ?",
                (int(company_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"Company profile not found: {company_id}")
        return CompanyProfile.model_validate(_json_dict(row["profile_json"]))

    def load_company_features(self, company_id: int) -> list[CompanyFeatureRecord]:
        """Load structured company features from one saved company profile."""

        return self.load_company_profile(company_id).features

    def load_opportunity_record(self, opportunity_id: int) -> OpportunityRecord:
        """Load one saved opportunity payload from SQLite."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT opportunity_json FROM opportunities WHERE id = ?",
                (int(opportunity_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"Opportunity record not found: {opportunity_id}")
        return OpportunityRecord.model_validate(_json_dict(row["opportunity_json"]))

    def save_opportunity(self, opportunity: Any, *, status: str = "candidate") -> int:
        payload = _as_dict(opportunity)
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO opportunities
                    (
                        target_company, opportunity_type, priority_score, status,
                        opportunity_json, created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(
                        str(payload.get("company_name") or payload.get("target_company") or "")
                    ),
                    _redact_string(
                        str(payload.get("opportunity_type") or payload.get("title") or "")
                    ),
                    payload.get("priority_score")
                    if payload.get("priority_score") is not None
                    else (
                        round(float(payload["score"]) * 100)
                        if isinstance(payload.get("score"), int | float)
                        else None
                    ),
                    _redact_string(status),
                    stable_json(payload),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            opportunity_id = int(cursor.lastrowid)
        for source in payload.get("sources") or []:
            source_payload = _as_dict(source) if not isinstance(source, Mapping) else dict(source)
            self.save_source(
                object_type="opportunity",
                object_id=opportunity_id,
                title=str(source_payload.get("title") or ""),
                url=str(source_payload.get("url") or ""),
                snippet=str(source_payload.get("supported_signal") or ""),
            )
        return opportunity_id

    def save_opportunity_scout_result(self, result: Any, *, status: str = "candidate") -> list[int]:
        payload = _as_dict(result)
        return [
            self.save_opportunity(record, status=status) for record in payload.get("records") or []
        ]

    def save_orchestrator_result(
        self,
        result: Any,
        *,
        input_payload: Any | None = None,
        input_summary: str = "orchestrator route",
        model: str = "fixture",
        dry_run: bool = True,
        status: str = "success",
    ) -> int:
        return self.save_agent_run(
            agent_name="orchestrator",
            input_payload=input_payload,
            input_summary=input_summary,
            output=result,
            model=model,
            dry_run=dry_run,
            status=status,
        )

    def save_outreach_draft(self, draft: Any) -> int:
        payload = _as_dict(draft)
        email_body = _redact_string(str(payload.get("email_body") or payload.get("body") or ""))
        safe_payload = redact_secrets(payload, summarize_email_content=True)
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO outreach_drafts
                    (
                        company_name, contact_name, email_subject, email_body,
                        approval_state, draft_json, created_at_utc, created_at_et,
                        created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(str(payload.get("company_name") or "")),
                    _redact_string(
                        str(payload.get("contact_name") or payload.get("recipient") or "")
                    )
                    or None,
                    _redact_string(
                        str(payload.get("email_subject") or payload.get("subject") or "")
                    ),
                    email_body,
                    _redact_string(
                        str(
                            payload.get("approval_state")
                            or payload.get("approval_status")
                            or "pending"
                        )
                    ),
                    stable_json(safe_payload),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def save_approval(
        self,
        *,
        object_type: str,
        object_id: str | int,
        decision: ApprovalState | str | None = None,
        scope: ApprovalScope | str = ApprovalScope.SEND,
        reviewer: str = "",
        timestamp: str | None = None,
        notes: str = "",
        previous_state: ApprovalState | str | None = None,
        approval_status: str | None = None,
        risk_flags: list[str] | None = None,
        source_agent: str = "",
    ) -> int:
        resolved_decision = decision if decision is not None else approval_status
        record = ApprovalDecisionRecord(
            object_type=object_type,
            object_id=object_id,
            decision=resolved_decision or ApprovalState.PENDING,
            scope=scope,
            reviewer=reviewer,
            timestamp=timestamp or approval_timestamp(),
            notes=notes,
            risk_flags=risk_flags or [],
            source_agent=source_agent,
        )
        if previous_state is not None:
            validate_approval_transition(previous_state, record.decision)
        created = _time_metadata()
        decision_time = _time_metadata(record.timestamp)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO approvals
                    (
                        object_type, object_id, decision, scope, reviewer, timestamp,
                        decision_at_et, decision_date_et, notes, risk_flags_json,
                        source_agent, created_at_utc, created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(record.object_type),
                    _redact_string(record.object_id),
                    _redact_string(record.decision.value),
                    _redact_string(record.scope.value),
                    _redact_string(record.reviewer),
                    _redact_string(record.timestamp),
                    decision_time["et"],
                    decision_time["date_et"],
                    _redact_string(record.notes),
                    stable_json(record.risk_flags),
                    _redact_string(record.source_agent),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)

    def latest_approval(
        self,
        *,
        object_type: str,
        object_id: str | int,
        scope: ApprovalScope | str | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent approval decision for one local workflow object."""

        conditions = ["object_type = ?", "object_id = ?"]
        params = [_redact_string(object_type), _redact_string(object_id)]
        if scope is not None:
            resolved_scope = normalize_approval_scope(scope)
            conditions.append("scope = ?")
            params.append(_redact_string(resolved_scope.value))
        where = " AND ".join(conditions)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM approvals WHERE {where} ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return dict(row) if row is not None else None

    def save_approval_item(self, item: ApprovalQueueItem | Mapping[str, Any]) -> str:
        """Create or replace a human approval queue item."""

        record = ApprovalQueueItem.model_validate(item)
        existing = self.get_approval_item(record.id)
        if existing is not None:
            validate_approval_queue_transition(
                existing.approval_status,
                record.approval_status,
            )
        created = _time_metadata(record.created_at)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_queue
                    (
                        id, object_type, object_id, title, summary, draft_text,
                        source_agent, risk_flags_json, approval_status, reviewer,
                        reviewer_notes, created_at_utc, created_at_et, created_date_et,
                        updated_at_utc, expires_at_utc, metadata_json
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    object_type = excluded.object_type,
                    object_id = excluded.object_id,
                    title = excluded.title,
                    summary = excluded.summary,
                    draft_text = excluded.draft_text,
                    source_agent = excluded.source_agent,
                    risk_flags_json = excluded.risk_flags_json,
                    approval_status = excluded.approval_status,
                    reviewer = excluded.reviewer,
                    reviewer_notes = excluded.reviewer_notes,
                    created_at_utc = excluded.created_at_utc,
                    created_at_et = excluded.created_at_et,
                    created_date_et = excluded.created_date_et,
                    updated_at_utc = excluded.updated_at_utc,
                    expires_at_utc = excluded.expires_at_utc,
                    metadata_json = excluded.metadata_json
                """,
                (
                    _redact_string(record.id),
                    _redact_string(record.object_type.value),
                    _redact_string(record.object_id) if record.object_id else None,
                    _redact_string(record.title),
                    _redact_string(record.summary),
                    _redact_string(record.draft_text) if record.draft_text else None,
                    _redact_string(record.source_agent),
                    stable_json(record.risk_flags),
                    _redact_string(record.approval_status.value),
                    _redact_string(record.reviewer) if record.reviewer else None,
                    _redact_string(record.reviewer_notes) if record.reviewer_notes else None,
                    created["utc"],
                    created["et"],
                    created["date_et"],
                    _optional_iso_z(record.updated_at),
                    _optional_iso_z(record.expires_at),
                    stable_json(redact_secrets(record.metadata, summarize_email_content=True)),
                ),
            )
        return record.id

    def list_approval_items(
        self,
        status: ApprovalQueueStatus | str | None = ApprovalQueueStatus.PENDING,
        *,
        object_type: str | None = None,
        source_agent: str | None = None,
    ) -> list[ApprovalQueueItem]:
        """Return approval queue items with optional filters."""

        conditions: list[str] = []
        params: list[str] = []
        if status is not None:
            resolved_status = normalize_approval_queue_status(status)
            conditions.append("approval_status = ?")
            params.append(resolved_status.value)
        if object_type:
            conditions.append("object_type = ?")
            params.append(_redact_string(object_type))
        if source_agent:
            conditions.append("source_agent = ?")
            params.append(_redact_string(source_agent))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM approval_queue {where} ORDER BY created_at_utc, id",
                params,
            ).fetchall()
        return [_approval_queue_item_from_row(row) for row in rows]

    def get_pending_approvals(
        self,
        status: ApprovalQueueStatus | str | None = ApprovalQueueStatus.PENDING,
    ) -> list[ApprovalQueueItem]:
        """Return approval queue items, pending by default."""

        return self.list_approval_items(status=status)

    def get_approval_item(self, approval_id: str) -> ApprovalQueueItem | None:
        """Fetch one approval queue item by id."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_queue WHERE id = ?",
                (_redact_string(approval_id),),
            ).fetchone()
        if row is None:
            return None
        return _approval_queue_item_from_row(row)

    def update_approval_status(
        self,
        approval_id: str,
        status: ApprovalQueueStatus | str,
        *,
        reviewer: str | None = None,
        notes: str | None = None,
    ) -> ApprovalQueueItem:
        """Update review status and reviewer metadata for one queue item."""

        existing = self.get_approval_item(approval_id)
        if existing is None:
            raise KeyError(f"Approval queue item not found: {approval_id}")
        resolved_status = normalize_approval_queue_status(status)
        validate_approval_queue_transition(existing.approval_status, resolved_status)
        updated_at = _iso_z(datetime.now(UTC).replace(microsecond=0))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approval_queue
                SET
                    approval_status = ?,
                    reviewer = COALESCE(?, reviewer),
                    reviewer_notes = COALESCE(?, reviewer_notes),
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (
                    resolved_status.value,
                    _redact_string(reviewer) if reviewer is not None else None,
                    _redact_string(notes) if notes is not None else None,
                    updated_at,
                    _redact_string(approval_id),
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Approval queue item not found: {approval_id}")

        item = self.get_approval_item(approval_id)
        if item is None:
            raise KeyError(f"Approval queue item not found: {approval_id}")
        return item

    def archive_approval_item(
        self,
        approval_id: str,
        *,
        reviewer: str | None = None,
        notes: str | None = None,
    ) -> ApprovalQueueItem:
        """Archive one approval queue item."""

        return self.update_approval_status(
            approval_id,
            ApprovalQueueStatus.ARCHIVED,
            reviewer=reviewer,
            notes=notes,
        )

    def save_source(
        self,
        *,
        object_type: str,
        object_id: int,
        title: str,
        url: str,
        snippet: str = "",
    ) -> int:
        created = _time_metadata()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sources
                    (
                        object_type, object_id, title, url, snippet, created_at_utc,
                        created_at_et, created_date_et
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _redact_string(object_type),
                    int(object_id),
                    _redact_string(title),
                    _redact_string(url),
                    _redact_string(snippet),
                    created["utc"],
                    created["et"],
                    created["date_et"],
                ),
            )
            return int(cursor.lastrowid)
