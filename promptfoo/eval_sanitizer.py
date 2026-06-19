"""Sanitation checks for Promptfoo eval case intake."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROMPTFOO_TEST_PATHS = (
    Path("promptfoo/tests/slack_research.yaml"),
    Path("promptfoo/tests/slack_retrieval_synthesis.yaml"),
    Path("promptfoo/tests/slack_tool_safety.yaml"),
    Path("promptfoo/tests/slack_agent_coverage.yaml"),
    Path("promptfoo/tests/slack_agent_expansion_15.yaml"),
)

_SECRET_PATTERNS = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.IGNORECASE)),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{10,}\b", re.IGNORECASE)),
)
_CONTACT_PATTERNS = (
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("phone", re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")),
)
_RAW_GMAIL_HEADER_PATTERN = re.compile(
    r"\bfrom:\s*.+\bto:\s*.+\bsubject:",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_TERMS = (
    "diagnosis",
    "medical record",
    "mrn",
    "ssn",
    "dob",
    "hipaa",
    "authorization:",
)


def scan_promptfoo_case_files(
    paths: list[str | Path] | tuple[str | Path, ...] = DEFAULT_PROMPTFOO_TEST_PATHS,
    *,
    root: str | Path = ".",
) -> dict[str, Any]:
    """Scan committed Promptfoo case files for high-signal unsafe raw data."""

    root_path = Path(root)
    issues: list[dict[str, str]] = []
    scanned_cases = 0
    for raw_path in paths:
        path = Path(raw_path)
        full_path = path if path.is_absolute() else root_path / path
        if not full_path.exists():
            issues.append(
                {
                    "path": str(path),
                    "case_id": "",
                    "field": "",
                    "kind": "missing_file",
                    "detail": "Promptfoo test file is missing",
                }
            )
            continue
        data = yaml.safe_load(full_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, list):
            cases = data
        else:
            cases = data.get("tests") if isinstance(data, dict) else []
        if not isinstance(cases, list):
            continue
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            vars_ = case.get("vars") if isinstance(case.get("vars"), dict) else {}
            case_id = str(vars_.get("case_id") or case.get("description") or f"case-{index + 1}")
            scanned_cases += 1
            for field_path, value in _case_text_fields(vars_):
                issues.extend(
                    _scan_text(
                        value,
                        path=str(path),
                        case_id=case_id,
                        field=field_path,
                        exempt=_sanitized_exemption(vars_),
                    )
                )
    return {
        "schema": "keystone.promptfoo.sanitation.v1",
        "status": "pass" if not issues else "fail",
        "case_count": scanned_cases,
        "issue_count": len(issues),
        "issues": issues,
    }


def _case_text_fields(vars_: dict[str, Any]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for key in ("user_input", "required_terms", "forbidden_terms", "notes"):
        value = vars_.get(key)
        if isinstance(value, str):
            fields.append((key, value))
        elif isinstance(value, list):
            fields.append((key, " ".join(str(item) for item in value)))
    slack_context = vars_.get("slack_context") if isinstance(vars_.get("slack_context"), dict) else {}
    selected = slack_context.get("selected_message") if isinstance(slack_context.get("selected_message"), dict) else {}
    if isinstance(selected.get("text"), str):
        fields.append(("slack_context.selected_message.text", selected["text"]))
    for idx, message in enumerate(slack_context.get("thread_messages") or []):
        if isinstance(message, dict) and isinstance(message.get("text"), str):
            fields.append((f"slack_context.thread_messages[{idx}].text", message["text"]))
    for idx, source in enumerate(vars_.get("sources") or []):
        if isinstance(source, dict):
            for key in ("title", "snippet", "excerpt", "text"):
                if isinstance(source.get(key), str):
                    fields.append((f"sources[{idx}].{key}", source[key]))
    return fields


def _scan_text(
    text: str,
    *,
    path: str,
    case_id: str,
    field: str,
    exempt: bool,
) -> list[dict[str, str]]:
    if exempt:
        return []
    issues: list[dict[str, str]] = []
    for kind, pattern in (*_SECRET_PATTERNS, *_CONTACT_PATTERNS):
        if pattern.search(text):
            issues.append(
                {
                    "path": path,
                    "case_id": case_id,
                    "field": field,
                    "kind": kind,
                    "detail": "Matched high-signal private data pattern",
                }
            )
    if _RAW_GMAIL_HEADER_PATTERN.search(text):
        issues.append(
            {
                "path": path,
                "case_id": case_id,
                "field": field,
                "kind": "raw_gmail_header",
                "detail": "Matched raw Gmail header shape",
            }
        )
    lowered = text.lower()
    for term in _SENSITIVE_TERMS:
        if term in lowered:
            issues.append(
                {
                    "path": path,
                    "case_id": case_id,
                    "field": field,
                    "kind": "sensitive_term",
                    "detail": f"Matched sensitive term: {term}",
                }
            )
    return issues


def _sanitized_exemption(vars_: dict[str, Any]) -> bool:
    value = vars_.get("sanitized_fixture_exemption")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "approved"}
    return False
