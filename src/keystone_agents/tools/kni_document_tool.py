"""Local-only KNI document search tools with sensitive-use guardrails."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool

KNI_DOC_SEARCH_ENABLED_ENV = "KEYSTONE_KNI_DOC_SEARCH_ENABLED"
KNI_DOC_ROOT_PATH_ENV = "KEYSTONE_KNI_DOC_ROOT_PATH"
KNI_DOC_INDEX_PATH_ENV = "KEYSTONE_KNI_DOC_INDEX_PATH"
KNI_DOC_PDF_TEXT_COMMAND_ENV = "KEYSTONE_KNI_DOC_PDF_TEXT_COMMAND"
KNI_DOC_CONTENT_SCAN_MODE_ENV = "KEYSTONE_KNI_DOC_CONTENT_SCAN_MODE"
KNI_DOC_AUTO_REFRESH_ENV = "KEYSTONE_KNI_DOC_AUTO_REFRESH"
KNI_DOC_REFRESH_TIMEOUT_ENV = "KEYSTONE_KNI_DOC_REFRESH_TIMEOUT_SECONDS"
KNI_DOC_MODEL_CONTEXT_ALLOWED_ENV = "KEYSTONE_KNI_DOC_MODEL_CONTEXT_ALLOWED"

DEFAULT_KNI_DOC_ROOT_PATH = ""
DEFAULT_KNI_DOC_INDEX_PATH = ""
DEFAULT_PDF_TEXT_COMMAND = "pdftotext"
REFRESH_COMMAND = "python3 -m kni_integrations.cli doc-index"
REFRESH_WORKDIR = ""
MAX_SEARCH_RESULTS = 12
MAX_READ_CHARS = 24_000
DEFAULT_READ_CHARS = 6_000
PDF_TIMEOUT_SECONDS = 20
DEFAULT_REFRESH_TIMEOUT_SECONDS = 90

TEXT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".html",
        ".json",
        ".md",
        ".txt",
        ".tsv",
        ".xml",
        ".yaml",
        ".yml",
    }
)
DOCX_EXTENSIONS = frozenset({".docx"})
PDF_EXTENSIONS = frozenset({".pdf"})
READABLE_EXTENSIONS = TEXT_EXTENSIONS | DOCX_EXTENSIONS | PDF_EXTENSIONS

BLOCKED_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)\.[^/]+", re.I),
    re.compile(r"(^|/)(\.run|\.codex_tmp|\.docx_qa|\.playwright-mcp)(/|$)", re.I),
    re.compile(r"(^|/)00_Admin/Licenses_Credentials(/|$)", re.I),
    re.compile(r"(^|/)01_Finance/(Accounting/Tracker_Exports|Banking_Payments|Taxes)(/|$)", re.I),
    re.compile(r"(^|/)01_Finance/Expenses(/|$)", re.I),
    re.compile(r"(^|/)kni-finance-ops-local/(data|backups|exports|\.run)(/|$)", re.I),
    re.compile(r"(^|/)(token|oauth|credentials?|secrets?|private[-_ ]?key)(/|$)", re.I),
    re.compile(r"(^|/)[^/]*(?:\.env|oauth|token|secret|private[-_ ]?key)[^/]*$", re.I),
    re.compile(r"\.(sqlite|sqlite3|db|log|pyc|zip)$", re.I),
    re.compile(r"(^|/)[^/]*(?:ein|w-?9|tax[-_ ]?id|ssn|social[-_ ]?security)[^/]*$", re.I),
    re.compile(r"(^|/)[^/]*(?:paymentconfirmation|payment confirmation|receipt)[^/]*$", re.I),
    re.compile(r"(^|/)[^/]*Invoice[A-Z]?\s+-\s+Keystone[^/]*$", re.I),
)

BLOCK_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sensitive_identifier", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "bank_payment",
        re.compile(
            r"\b(?:routing\s+number|account\s+number|bank\s+account|wire\s+transfer|"
            r"ach\s+(?:routing|account)|payment\s+setup)\b",
            re.I,
        ),
    ),
    (
        "tax_identity",
        re.compile(r"\b(?:ein|employer identification number|tax id|w-?9|ssn)\b", re.I),
    ),
    (
        "credentials",
        re.compile(
            r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:api[-_ ]?key|access[_-]?token|"
            r"refresh[_-]?token|client[_-]?secret|oauth[_-]?token|password)\b|"
            r"\b(?:sk|xox[baprs]|ghp)_[A-Za-z0-9_\-]{12,})",
            re.I,
        ),
    ),
    (
        "phi_patient_identifier",
        re.compile(
            r"\b(?:mrn|medical record number|patient id|patient name|date of birth|dob)\b",
            re.I,
        ),
    ),
)

REDACTABLE_CONTENT_REASONS = frozenset(
    {"bank_payment", "tax_identity", "sensitive_identifier"}
)

REVIEW_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "legal_contract",
        re.compile(r"\b(?:contract|agreement|nda|msa|sow|baa|dua|legal|terms)\b", re.I),
    ),
    ("finance_tax", re.compile(r"\b(?:finance|invoice|payment|tax|rate card)\b", re.I)),
    (
        "insurance_policy",
        re.compile(
            r"\b(?:insurance|insurance\s+policy|coverage|coi|certificate\s+of\s+insurance|"
            r"peo|(?:general|professional|cyber|commercial)\s+liability)\b",
            re.I,
        ),
    ),
    (
        "privacy_policy",
        re.compile(r"\b(?:privacy|hipaa|phi|patient data|confidentiality)\b", re.I),
    ),
)


@dataclass(frozen=True)
class KNIDocumentConfig:
    enabled: bool
    root_path: Path
    index_path: Path
    pdf_text_command: str
    content_scan_mode: str
    model_context_allowed: bool
    auto_refresh: bool
    refresh_timeout_seconds: int


@dataclass(frozen=True)
class KNIDocumentRecord:
    relative_path: str
    title: str
    extension: str
    modified_at: str
    preview_text: str
    searchable_path: str
    searchable_content: str


@dataclass(frozen=True)
class SensitivityDecision:
    status: str
    blocked: bool
    review_required: bool
    review_reasons: tuple[str, ...]


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def _truthy(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(1, parsed)


def kni_document_config(env: dict[str, str] | None = None) -> KNIDocumentConfig:
    env_map = os.environ if env is None else env
    return KNIDocumentConfig(
        enabled=_truthy(env_map.get(KNI_DOC_SEARCH_ENABLED_ENV), default=False),
        root_path=Path(env_map.get(KNI_DOC_ROOT_PATH_ENV, DEFAULT_KNI_DOC_ROOT_PATH)).expanduser(),
        index_path=Path(env_map.get(KNI_DOC_INDEX_PATH_ENV, DEFAULT_KNI_DOC_INDEX_PATH)).expanduser(),
        pdf_text_command=env_map.get(KNI_DOC_PDF_TEXT_COMMAND_ENV, DEFAULT_PDF_TEXT_COMMAND),
        content_scan_mode=env_map.get(KNI_DOC_CONTENT_SCAN_MODE_ENV, "block").strip().lower()
        or "block",
        model_context_allowed=_truthy(
            env_map.get(KNI_DOC_MODEL_CONTEXT_ALLOWED_ENV),
            default=True,
        ),
        auto_refresh=_truthy(env_map.get(KNI_DOC_AUTO_REFRESH_ENV), default=True),
        refresh_timeout_seconds=_positive_int(
            env_map.get(KNI_DOC_REFRESH_TIMEOUT_ENV),
            default=DEFAULT_REFRESH_TIMEOUT_SECONDS,
        ),
    )


def _refresh_diagnostic(config: KNIDocumentConfig, reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "refresh_command": REFRESH_COMMAND,
        "refresh_workdir": REFRESH_WORKDIR,
        "root_path": str(config.root_path),
        "index_path": str(config.index_path),
    }


def _refresh_index_if_enabled(config: KNIDocumentConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "command": REFRESH_COMMAND,
        "workdir": REFRESH_WORKDIR,
    }
    if not config.enabled or not config.auto_refresh:
        payload["reason"] = "disabled"
        return payload
    workdir = Path(REFRESH_WORKDIR)
    if not workdir.exists():
        payload["reason"] = "missing_refresh_workdir"
        return payload
    payload["attempted"] = True
    try:
        completed = subprocess.run(
            ["python3", "-m", "kni_integrations.cli", "doc-index"],
            cwd=str(workdir),
            check=False,
            capture_output=True,
            text=True,
            timeout=config.refresh_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        payload["reason"] = "timeout"
        return payload
    except OSError as exc:
        payload["reason"] = type(exc).__name__
        return payload
    payload["ok"] = completed.returncode == 0
    payload["returncode"] = completed.returncode
    if completed.returncode != 0:
        stderr = completed.stderr or ""
        if "attempt to write a readonly database" in stderr.lower():
            payload["reason"] = "readonly_index"
            payload["stderr_tail"] = "SQLite index is readable but not writable in this runtime."
        else:
            payload["reason"] = "nonzero_exit"
            payload["stderr_tail"] = stderr[-500:]
    return payload


def _connect_readonly(index_path: Path) -> sqlite3.Connection:
    uri = f"file:{index_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_index_records(config: KNIDocumentConfig) -> tuple[list[KNIDocumentRecord], str]:
    if not config.enabled:
        return [], "disabled"
    if not config.root_path.exists():
        return [], "missing_root"
    if not config.index_path.exists():
        return [], "missing_index"
    try:
        with _connect_readonly(config.index_path) as connection:
            rows = connection.execute(
                """
                SELECT relative_path, title, extension, modified_at, preview_text,
                       searchable_path, searchable_content
                FROM local_documents
                """
            ).fetchall()
    except sqlite3.Error:
        return [], "unreadable_index"

    records = [
        KNIDocumentRecord(
            relative_path=str(row[0]),
            title=str(row[1]),
            extension=str(row[2]),
            modified_at=str(row[3]),
            preview_text=str(row[4]),
            searchable_path=str(row[5]),
            searchable_content=str(row[6]),
        )
        for row in rows
    ]
    return records, "ready"


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _path_block_reasons(relative_path: str) -> tuple[str, ...]:
    normalized = relative_path.replace("\\", "/")
    reasons: list[str] = []
    for pattern in BLOCKED_PATH_PATTERNS:
        if pattern.search(normalized):
            reasons.append("blocked_path")
            break
    return tuple(reasons)


def _content_block_reasons(text: str) -> tuple[str, ...]:
    reasons: list[str] = []
    for reason, pattern in BLOCK_CONTENT_PATTERNS:
        if pattern.search(text):
            reasons.append(reason)
    return tuple(dict.fromkeys(reasons))


def _redact_sensitive_lines(text: str) -> tuple[str, tuple[str, ...], int]:
    reasons: list[str] = []
    kept_lines: list[str] = []
    redacted_count = 0
    for line in text.splitlines():
        line_reasons = [
            reason
            for reason, pattern in BLOCK_CONTENT_PATTERNS
            if reason in REDACTABLE_CONTENT_REASONS and pattern.search(line)
        ]
        if line_reasons:
            reasons.extend(line_reasons)
            redacted_count += 1
            continue
        kept_lines.append(line)
    redacted = "\n".join(kept_lines).strip()
    return redacted, tuple(dict.fromkeys(reasons)), redacted_count


def _review_reasons(relative_path: str, text: str = "") -> tuple[str, ...]:
    haystack = re.sub(r"[^A-Za-z0-9]+", " ", f"{relative_path} {text[:4000]}")
    reasons: list[str] = []
    for reason, pattern in REVIEW_PATH_PATTERNS:
        if pattern.search(haystack):
            reasons.append(reason)
    return tuple(dict.fromkeys(reasons))


def assess_kni_document_sensitivity(relative_path: str, text: str = "") -> SensitivityDecision:
    block_reasons = [*_path_block_reasons(relative_path), *_content_block_reasons(text)]
    review_reasons = list(_review_reasons(relative_path, text))
    if block_reasons:
        review_reasons.extend(reason for reason in block_reasons if reason not in review_reasons)
        return SensitivityDecision(
            status="blocked",
            blocked=True,
            review_required=True,
            review_reasons=tuple(dict.fromkeys(review_reasons)),
        )
    return SensitivityDecision(
        status="allowed",
        blocked=False,
        review_required=bool(review_reasons),
        review_reasons=tuple(review_reasons),
    )


def _record_payload(
    record: KNIDocumentRecord,
    *,
    snippet: str,
    sensitivity: SensitivityDecision,
    model_context_allowed: bool,
) -> dict[str, Any]:
    return {
        "relative_path": record.relative_path,
        "title": record.title,
        "extension": record.extension,
        "modified_at": record.modified_at,
        "snippet": snippet,
        "sensitivity_status": sensitivity.status,
        "review_required": sensitivity.review_required,
        "review_reasons": list(sensitivity.review_reasons),
        "local_only": True,
        "model_context_allowed": model_context_allowed,
        "send_enabled": False,
    }


def _record_file_payload(record: KNIDocumentRecord) -> dict[str, Any]:
    sensitivity = assess_kni_document_sensitivity(record.relative_path, record.preview_text)
    return {
        "relative_path": record.relative_path,
        "title": record.title,
        "extension": record.extension,
        "modified_at": record.modified_at,
        "sensitivity_status": sensitivity.status,
        "review_required": sensitivity.review_required,
        "review_reasons": list(sensitivity.review_reasons),
        "local_only": True,
        "model_context_allowed": False if sensitivity.blocked else True,
        "send_enabled": False,
    }


def _score_record(record: KNIDocumentRecord, terms: list[str]) -> int:
    path = f"{record.relative_path} {record.title}".lower()
    content = f"{record.searchable_content} {record.preview_text}".lower()
    score = 0
    for term in terms:
        if term in path:
            score += 6
        if term in content:
            score += 3
    return score


def _snippet_for_record(record: KNIDocumentRecord, terms: list[str]) -> str:
    text = record.preview_text or record.relative_path
    lowered = text.lower()
    indexes = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    if indexes:
        index = min(indexes)
        start = max(0, index - 240)
        end = min(len(text), index + 600)
        return _normalize_text(text[start:end])
    return _normalize_text(text[:600] or record.relative_path)


def list_kni_document_sources_impl(env: dict[str, str] | None = None) -> dict[str, Any]:
    config = kni_document_config(env=env)
    records, status = _load_index_records(config)
    top_level_counts = Counter(
        record.relative_path.split("/", 1)[0] if "/" in record.relative_path else record.relative_path
        for record in records
    )
    indexed_at = ""
    if config.index_path.exists():
        try:
            with _connect_readonly(config.index_path) as connection:
                row = connection.execute("SELECT MAX(indexed_at) FROM local_documents").fetchone()
                indexed_at = str(row[0] or "") if row else ""
        except sqlite3.Error:
            indexed_at = ""
    return {
        "mode": "kni_document_sources",
        "enabled": config.enabled,
        "status": status,
        "root_path": str(config.root_path),
        "index_path": str(config.index_path),
        "indexed_count": len(records),
        "indexed_at": indexed_at,
        "top_level_counts": dict(top_level_counts.most_common(20)),
        "local_only": True,
        "model_context_allowed": config.model_context_allowed,
        "send_enabled": False,
        "guardrails": [
            "Blocks bank/payment account details, tax identity records, PHI identifiers, "
            "credentials, local databases, logs, runtime data, and traversal attempts.",
            "Guarded snippets and capped/redacted reads may enter live model context only "
            "when KEYSTONE_KNI_DOC_MODEL_CONTEXT_ALLOWED=true.",
            "Legal, finance, tax, insurance, privacy, and policy context is review-required.",
            "Local KNI context is not approval to send, post, publish, submit, or share externally.",
        ],
        "diagnostic": None if status == "ready" else _refresh_diagnostic(config, status),
    }


def search_kni_documents_impl(
    query: str,
    *,
    max_results: int = 5,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    config = kni_document_config(env=env)
    normalized = _normalize_text(query).lower()
    if not normalized:
        raise ValueError("query is required.")
    refresh = _refresh_index_if_enabled(config)
    records, status = _load_index_records(config)
    if status != "ready":
        return {
            "mode": "kni_document_search",
            "query": query,
            "status": status,
            "matches": [],
            "blocked_result_count": 0,
            "local_only": True,
            "model_context_allowed": config.model_context_allowed,
            "send_enabled": False,
            "refresh": refresh,
            "diagnostic": _refresh_diagnostic(config, status),
        }
    terms = normalized.split()
    limit = max(1, min(max_results, MAX_SEARCH_RESULTS))
    scored: list[tuple[int, KNIDocumentRecord]] = []
    for record in records:
        score = _score_record(record, terms)
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda item: (-item[0], item[1].relative_path.lower()))

    matches: list[dict[str, Any]] = []
    blocked_count = 0
    for _score, record in scored:
        snippet = _snippet_for_record(record, terms)
        sensitivity = assess_kni_document_sensitivity(record.relative_path, snippet)
        if sensitivity.blocked:
            blocked_count += 1
            continue
        matches.append(
            _record_payload(
                record,
                snippet=snippet,
                sensitivity=sensitivity,
                model_context_allowed=config.model_context_allowed,
            )
        )
        if len(matches) >= limit:
            break

    return {
        "mode": "kni_document_search",
        "query": query,
        "status": "ready",
        "matches": matches,
        "blocked_result_count": blocked_count,
        "local_only": True,
        "model_context_allowed": config.model_context_allowed,
        "send_enabled": False,
        "refresh": refresh,
        "diagnostic": None,
    }


def _normalize_folder_path(relative_folder_path: str) -> str:
    raw = str(relative_folder_path or "").strip().strip("/")
    if not raw:
        raise ValueError("relative_folder_path is required.")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("relative_folder_path escapes the KNI document root.")
    return raw.replace("\\", "/")


def list_kni_document_folder_impl(
    relative_folder_path: str,
    *,
    max_results: int = 50,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """List indexed local KNI documents under one relative folder."""

    config = kni_document_config(env=env)
    folder_path = _normalize_folder_path(relative_folder_path)
    refresh = _refresh_index_if_enabled(config)
    records, status = _load_index_records(config)
    if status != "ready":
        return {
            "mode": "kni_document_folder",
            "relative_folder_path": folder_path,
            "status": status,
            "files": [],
            "file_count": 0,
            "blocked_file_count": 0,
            "local_only": True,
            "model_context_allowed": config.model_context_allowed,
            "send_enabled": False,
            "refresh": refresh,
            "diagnostic": _refresh_diagnostic(config, status),
        }
    prefix = f"{folder_path.rstrip('/')}/"
    folder_records = [
        record
        for record in records
        if record.relative_path == folder_path or record.relative_path.startswith(prefix)
    ]
    folder_records.sort(key=lambda record: (record.modified_at, record.relative_path.lower()), reverse=True)
    limit = max(1, min(max_results, 200))
    files: list[dict[str, Any]] = []
    blocked_count = 0
    for record in folder_records:
        payload = _record_file_payload(record)
        if payload["sensitivity_status"] == "blocked":
            blocked_count += 1
            continue
        files.append(payload)
        if len(files) >= limit:
            break
    return {
        "mode": "kni_document_folder",
        "relative_folder_path": folder_path,
        "status": "ready",
        "files": files,
        "file_count": len(folder_records),
        "returned_file_count": len(files),
        "blocked_file_count": blocked_count,
        "truncated": len(files) < max(0, len(folder_records) - blocked_count),
        "local_only": True,
        "model_context_allowed": config.model_context_allowed,
        "send_enabled": False,
        "refresh": refresh,
        "diagnostic": None,
    }


def _resolve_kni_document_path(config: KNIDocumentConfig, relative_path: str) -> Path:
    if not relative_path.strip():
        raise ValueError("relative_path is required.")
    raw_relative = Path(relative_path)
    if raw_relative.is_absolute():
        raise ValueError("relative_path must be relative to the KNI document root.")
    root = config.root_path.resolve()
    candidate = (root / raw_relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("relative_path escapes the KNI document root.")
    if not candidate.is_file():
        raise FileNotFoundError(f"KNI document file not found: {relative_path}")
    return candidate


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_docx_file(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not (
                name == "word/document.xml"
                or name.startswith("word/header")
                or name.startswith("word/footer")
            ):
                continue
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            for element in root.iter():
                if element.tag.endswith("}t") or element.tag == "t":
                    if element.text:
                        parts.append(element.text)
    return "\n".join(parts)


def _read_pdf_file(path: Path, *, command: str) -> str:
    command_path = Path(command)
    if not command_path.is_absolute() or not command_path.exists():
        raise FileNotFoundError(f"PDF text extraction command is unavailable: {command}")
    completed = subprocess.run(
        [str(command_path), "-layout", "-q", str(path), "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=PDF_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError("PDF text extraction failed.")
    return completed.stdout


def _read_kni_document_text(path: Path, *, config: KNIDocumentConfig) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return _read_text_file(path)
    if suffix in DOCX_EXTENSIONS:
        return _read_docx_file(path)
    if suffix in PDF_EXTENSIONS:
        return _read_pdf_file(path, command=config.pdf_text_command)
    raise ValueError("Only text, DOCX, and PDF KNI documents can be read into model context.")


def read_kni_document_file_impl(
    relative_path: str,
    *,
    max_chars: int = DEFAULT_READ_CHARS,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    config = kni_document_config(env=env)
    raw_relative = Path(relative_path)
    if raw_relative.is_absolute() or ".." in raw_relative.parts:
        raise ValueError("relative_path escapes the KNI document root.")
    if not config.enabled:
        return {
            "mode": "kni_document_file",
            "relative_path": relative_path,
            "sensitivity_status": "blocked",
            "review_required": True,
            "review_reasons": ["disabled"],
            "local_only": True,
            "model_context_allowed": config.model_context_allowed,
            "send_enabled": False,
            "diagnostic": _refresh_diagnostic(config, "disabled"),
        }
    path_sensitivity = assess_kni_document_sensitivity(relative_path)
    if path_sensitivity.blocked:
        return {
            "mode": "kni_document_file",
            "relative_path": relative_path,
            "sensitivity_status": "blocked",
            "review_required": True,
            "review_reasons": list(path_sensitivity.review_reasons),
            "content": "",
            "truncated": False,
            "local_only": True,
            "model_context_allowed": config.model_context_allowed,
            "send_enabled": False,
        }
    path = _resolve_kni_document_path(config, relative_path)
    if path.suffix.lower() not in READABLE_EXTENSIONS:
        raise ValueError("Only text, DOCX, and PDF KNI documents can be read into model context.")
    text = _read_kni_document_text(path, config=config)
    content_sensitivity = assess_kni_document_sensitivity(relative_path, text)
    if content_sensitivity.blocked:
        non_redactable_reasons = [
            reason
            for reason in content_sensitivity.review_reasons
            if reason
            in {
                "blocked_path",
                "credentials",
                "phi_patient_identifier",
            }
        ]
        if not non_redactable_reasons:
            redacted_text, redacted_reasons, redacted_count = _redact_sensitive_lines(text)
            if redacted_text and redacted_count:
                redacted_review_reasons = tuple(
                    dict.fromkeys((*content_sensitivity.review_reasons, *redacted_reasons))
                )
                limit = max(1, min(max_chars, MAX_READ_CHARS))
                content = redacted_text[:limit]
                return {
                    "mode": "kni_document_file",
                    "relative_path": relative_path,
                    "title": path.stem,
                    "extension": path.suffix.lower().lstrip("."),
                    "sensitivity_status": "redacted",
                    "review_required": True,
                    "review_reasons": list(redacted_review_reasons),
                    "redacted_line_count": redacted_count,
                    "content": content,
                    "truncated": len(redacted_text) > limit,
                    "local_only": True,
                    "model_context_allowed": config.model_context_allowed,
                    "send_enabled": False,
                }
        return {
            "mode": "kni_document_file",
            "relative_path": relative_path,
            "title": path.stem,
            "extension": path.suffix.lower().lstrip("."),
            "sensitivity_status": "blocked",
            "review_required": True,
            "review_reasons": list(content_sensitivity.review_reasons),
            "content": "",
            "truncated": False,
            "local_only": True,
            "model_context_allowed": config.model_context_allowed,
            "send_enabled": False,
        }
    limit = max(1, min(max_chars, MAX_READ_CHARS))
    content = text[:limit]
    return {
        "mode": "kni_document_file",
        "relative_path": relative_path,
        "title": path.stem,
        "extension": path.suffix.lower().lstrip("."),
        "sensitivity_status": content_sensitivity.status,
        "review_required": content_sensitivity.review_required,
        "review_reasons": list(content_sensitivity.review_reasons),
        "content": content,
        "truncated": len(text) > limit,
        "local_only": True,
        "model_context_allowed": config.model_context_allowed,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def list_kni_document_sources() -> str:
    """List local-only KNI document index availability and guardrail state."""

    return _json_payload(list_kni_document_sources_impl())


@function_tool(**keystone_tool_guardrail_kwargs())
def search_kni_documents(query: str, max_results: int = 5) -> str:
    """Search the local KNI document index and return guarded snippets."""

    return _json_payload(search_kni_documents_impl(query, max_results=max_results))


@function_tool(**keystone_tool_guardrail_kwargs())
def list_kni_document_folder(relative_folder_path: str, max_results: int = 50) -> str:
    """List indexed local KNI files under a relative folder path."""

    return _json_payload(
        list_kni_document_folder_impl(
            relative_folder_path,
            max_results=max_results,
        )
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def read_kni_document_file(relative_path: str, max_chars: int = DEFAULT_READ_CHARS) -> str:
    """Read one guarded local KNI text, DOCX, or PDF document into capped context."""

    return _json_payload(read_kni_document_file_impl(relative_path, max_chars=max_chars))
