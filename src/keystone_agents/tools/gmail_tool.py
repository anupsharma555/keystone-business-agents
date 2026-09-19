"""Gmail API integration boundary.

Live Gmail support includes reads, labels, drafts, and a separately gated exact
test-draft send path. Ordinary agent email sending remains unavailable.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import mimetypes
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import requests

from keystone_agents.context_env import context_env_value
from keystone_agents.guardrails import (
    enforce_public_source_output_guardrails,
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
    keystone_tool_guardrail_kwargs,
    redact_secret_like_text,
)
from keystone_agents.provider_read import (
    ProviderReadContextError,
    current_provider_read_context,
    record_provider_read_result,
)
from keystone_agents.receipts.journal import durable_provider_tool, record_provider_observation
from keystone_agents.schemas.email_triage import (
    GMAIL_MANAGED_LABELS,
    GMAIL_PRIMARY_LABEL_SET,
    GmailAttachmentMetadata,
    GmailBodyEvidence,
    GmailLinkRecord,
    GmailMessageEnvelope,
    GmailQuotationEvidence,
    normalize_managed_gmail_labels,
)
from keystone_agents.sdk import ToolGuardrailViolation, function_tool

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


def _record_gmail_draft_observation(draft_id: str, operation: str) -> None:
    if draft_id:
        record_provider_observation({
            "status": "observed", "provider": "gmail", "operation": operation,
            "draft_id": draft_id, "provider_write": True,
            "verification": {"passed": False, "status": "pending_readback"},
        })
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_GOOGLE_CREDENTIALS_FILE = "credentials.json"
DEFAULT_GOOGLE_TOKEN_FILE = "token.json"
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
)
GMAIL_LIVE_OPERATIONS = (
    "read",
    "label",
    "mailbox_state",
    "create_draft",
    "update_draft",
    "send_test_draft",
)
GmailMailboxStateOperation = Literal[
    "add_label",
    "remove_label",
    "archive",
    "unarchive",
    "mark_read",
    "mark_unread",
    "star",
    "unstar",
    "mark_important",
    "mark_not_important",
    "trash",
    "restore",
]
GMAIL_MAILBOX_STATE_OPERATIONS = (
    "add_label",
    "remove_label",
    "archive",
    "unarchive",
    "mark_read",
    "mark_unread",
    "star",
    "unstar",
    "mark_important",
    "mark_not_important",
    "trash",
    "restore",
)
GmailOAuthReadiness = Literal["disabled", "ready", "misconfigured"]
GMAIL_OAUTH_REAUTH_COMMAND = ".venv/bin/python scripts/gmail_oauth_login.py --force"
GMAIL_SYSTEM_LABEL_IDS = {
    "INBOX",
    "UNREAD",
    "STARRED",
    "IMPORTANT",
    "SENT",
    "DRAFT",
    "TRASH",
    "SPAM",
}
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>'\"`]+", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")
HTML_BLOCKQUOTE_RE = re.compile(r"<blockquote\b", re.IGNORECASE)
HTML_REPLY_CLASS_MARKERS = (
    "gmail_quote",
    "gmail_attr",
    "moz-cite-prefix",
    "protonmail_quote",
    "yahoo_quoted",
)
PLAIN_RENDERING_STUB_RE = re.compile(
    r"(?:\b(?:view|read|open|display)\b.{0,100}\b(?:html|browser|online|web|rich[- ]text|"
    r"compatible)\b|\b(?:html|rich[- ]text)\b.{0,80}\b(?:version|reader|view)\b)",
    re.IGNORECASE | re.DOTALL,
)
QUOTE_DELIMITER_RE = re.compile(
    r"^(?:On .+ wrote:|From:\s.+|-----Original Message-----|Begin forwarded message:|"
    r"_{6,})$",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?:\n+|(?<=[.!?])\s+)")
THREAD_ACTION_RE = re.compile(
    r"\b(?:please|can you|could you|need you to|needs to|action item|follow up|"
    r"reply|send|share|review|confirm|provide|schedule|let me know)\b",
    re.IGNORECASE,
)
THREAD_DEADLINE_RE = re.compile(
    r"\b(?:today|tomorrow|tonight|this week|next week|next month|by eod|"
    r"by end of day|by cob|by close of business|deadline|due|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|jan(?:uary)?|feb(?:ruary)?|"
    r"mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
    re.IGNORECASE,
)
PROMOTIONAL_CTA_MARKERS = (
    "communication preferences",
    "discover partnering requests",
    "learn more",
    "log in",
    "make sure your profile",
    "our platform",
    "partner listing",
    "quick-start guide",
    "receive direct feedback",
    "start by creating",
    "submit a short",
    "unsubscribe",
    "we built",
)
SUSPICIOUS_LINK_TERMS = (
    "login",
    "verify",
    "password",
    "credential",
    "reset",
    "account-suspension",
    "account_suspension",
)
SHORTENER_DOMAINS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "ow.ly",
    "buff.ly",
    "rebrand.ly",
}
GMAIL_BODY_EVIDENCE_MAX_PARTS = 8
GMAIL_BODY_EVIDENCE_MAX_CHARS = 12_000
GMAIL_BODY_PART_MAX_CHARS = 6_000
RISKY_ATTACHMENT_EXTENSIONS = {
    ".exe",
    ".js",
    ".vbs",
    ".scr",
    ".bat",
    ".cmd",
    ".ps1",
    ".jar",
    ".msi",
}
MACRO_ATTACHMENT_EXTENSIONS = {".docm", ".xlsm", ".pptm"}
ARCHIVE_ATTACHMENT_EXTENSIONS = {".zip", ".rar", ".7z", ".gz", ".tar"}


class GmailConfigurationError(RuntimeError):
    """Raised when live Gmail is requested without usable OAuth credentials."""


class GmailAPIError(RuntimeError):
    """Raised when the Gmail API request fails."""


def _clean_label_names(labels: list[str]) -> list[str]:
    return normalize_managed_gmail_labels([label for label in labels if label.strip()])


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _decode_base64_url_bytes(data: str | None) -> bytes | None:
    if not data:
        return b""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("utf-8"))
    except (binascii.Error, ValueError):
        return None


def _decode_base64_url(data: str | None) -> str:
    decoded = _decode_base64_url_bytes(data)
    if decoded is None:
        return ""
    return decoded.decode("utf-8", errors="replace")


def _text_part_charset(part: Mapping[str, Any]) -> str:
    headers = part.get("headers") if isinstance(part.get("headers"), list) else []
    content_type = _header(headers, "Content-Type")
    match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.I)
    return match.group(1).strip() if match else "utf-8"


def _decode_text_part(
    part: Mapping[str, Any],
    data: str | None,
) -> tuple[str, bool, str]:
    raw = _decode_base64_url_bytes(data)
    if raw is None:
        return "", False, "The MIME body was not valid base64url data."
    charset = _text_part_charset(part)
    try:
        return raw.decode(charset, errors="strict"), True, ""
    except LookupError:
        return (
            raw.decode("utf-8", errors="replace"),
            False,
            f"Declared charset {charset!r} is unsupported; UTF-8 replacement decoding was used.",
        )
    except UnicodeDecodeError:
        return (
            raw.decode(charset, errors="replace"),
            False,
            f"MIME bytes were invalid for declared charset {charset!r}; replacement "
            "decoding was used.",
        )


class _HTMLTextExtractor(HTMLParser):
    """Minimal stdlib HTML-to-text converter for Gmail snippets."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in {"script", "style", "blockquote"}:
            self._skip_depth += 1
            return
        if tag_lower in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")
        if tag_lower == "a":
            href = dict(attrs).get("href")
            if href:
                self.parts.append(f" {href} ")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in {"script", "style", "blockquote"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag_lower in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


class _HTMLLinkExtractor(HTMLParser):
    """Collect href values without rendering or fetching anything."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


class _HTMLSourceExtractor(HTMLParser):
    """Preserve bounded HTML structure and quotation provenance without rendering."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, str]] = []
        self.frames: list[dict[str, str]] = []
        self.tables: list[dict[str, int]] = []
        self.table_count = 0
        self.structure_annotations = False
        self.content_complete = True
        self.limitations: list[str] = []
        self.unread_media_count = 0
        self._skip_depth = 0

    def _quote_kind(self) -> str:
        for frame in reversed(self.frames):
            if frame.get("quote_kind"):
                return frame["quote_kind"]
        return "direct"

    def _append(self, value: str) -> None:
        if value and not self._skip_depth:
            self.events.append((self._quote_kind(), value))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag_lower in {"script", "style"}:
            self._skip_depth += 1
            self.frames.append({"tag": tag_lower, "skip": "true"})
            return
        if self._skip_depth:
            self.frames.append({"tag": tag_lower})
            return

        marker_text = " ".join(
            [values.get("class", ""), values.get("id", "")]
        ).lower()
        reply_marker = any(marker in marker_text for marker in HTML_REPLY_CLASS_MARKERS)
        ancestor_quote_kind = self._quote_kind()
        quote_kind = ""
        if tag_lower == "blockquote":
            quote_kind = (
                "reply_history"
                if ancestor_quote_kind == "reply_history"
                or reply_marker
                or values.get("type", "").lower() == "cite"
                else "editorial_source"
            )
        elif reply_marker and tag_lower in {"div", "section"}:
            quote_kind = "reply_history"
        frame = {
            "tag": tag_lower,
            "quote_kind": quote_kind,
            "href": values.get("href", "") if tag_lower == "a" else "",
        }
        self.frames.append(frame)

        if tag_lower in {
            "br",
            "p",
            "div",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
        }:
            self._append("\n")
        if tag_lower in {"img", "video", "audio", "object", "svg", "canvas"}:
            self._append("\n[[inline visual or media content not read]]\n")
            self.content_complete = False
            self.unread_media_count += 1
        if tag_lower == "table":
            self.table_count += 1
            self.tables.append({"index": self.table_count, "row": 0, "cell": 0})
            self.structure_annotations = True
            self._append(f"\n[[table {self.table_count} start]]\n")
        elif tag_lower == "tr" and self.tables:
            table = self.tables[-1]
            table["row"] += 1
            table["cell"] = 0
            self._append(f"\n[[table {table['index']} row {table['row']}]]\n")
        elif tag_lower in {"th", "td"} and self.tables:
            table = self.tables[-1]
            table["cell"] += 1
            row_span = values.get("rowspan", "1") or "1"
            column_span = values.get("colspan", "1") or "1"
            self._append(
                "\n"
                f"[[table {table['index']} cell row={table['row']} "
                f"cell_index={table['cell']} header={str(tag_lower == 'th').lower()} "
                f"row_span={row_span} column_span={column_span}]]\n"
            )
            frame["cell_content_start"] = str(len(self.events))

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        frame_index = next(
            (
                index
                for index in range(len(self.frames) - 1, -1, -1)
                if self.frames[index].get("tag") == tag_lower
            ),
            None,
        )
        frame = self.frames[frame_index] if frame_index is not None else {}
        if frame.get("skip") == "true":
            self._skip_depth = max(0, self._skip_depth - 1)
        if not self._skip_depth:
            if tag_lower in {"th", "td"} and frame.get("cell_content_start"):
                content_start = int(frame["cell_content_start"])
                content = _compact_source_text(
                    "".join(value for _kind, value in self.events[content_start:])
                )
                if not content:
                    self._append("[[blank table cell]]")
            if tag_lower == "a" and frame.get("href"):
                self._append(f" [[link: {frame['href']}]]")
            if tag_lower in {
                "p",
                "div",
                "li",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "blockquote",
            }:
                self._append("\n")
            if tag_lower == "table" and self.tables:
                table = self.tables.pop()
                self._append(f"\n[[table {table['index']} end]]\n")
        if frame_index is not None:
            del self.frames[frame_index:]

    def handle_data(self, data: str) -> None:
        self._append(data)


@dataclass(frozen=True)
class _GmailPayloadExtraction:
    normalized_body: str
    body_evidence: list[GmailBodyEvidence]
    links: list[GmailLinkRecord]
    attachments: list[GmailAttachmentMetadata]
    quote_stripped: bool
    body_content_status: Literal["complete", "partial", "conflicting", "empty"]
    body_content_complete: bool
    limitations: list[str]


def _compact_source_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r", "").splitlines()]
    return "\n".join(line for line in lines if line).strip().replace("\u2014", "-")


def _bounded_source_text(value: str, *, limit: int) -> tuple[str, bool]:
    text = _compact_source_text(value)
    if len(text) <= limit:
        return text, False
    marker = "\n[[truncated: bounded Gmail source evidence]]"
    if limit <= 0:
        return "", bool(text)
    if limit <= len(marker):
        return marker[:limit], True
    return text[: limit - len(marker)].rstrip() + marker, True


def _quoted_segments_from_events(
    events: list[tuple[str, str]],
) -> list[GmailQuotationEvidence]:
    grouped: list[tuple[str, str]] = []
    for kind, value in events:
        if kind == "direct":
            continue
        if grouped and grouped[-1][0] == kind:
            grouped[-1] = (kind, grouped[-1][1] + value)
        else:
            grouped.append((kind, value))
    output: list[GmailQuotationEvidence] = []
    for kind, value in grouped:
        text, truncated = _bounded_source_text(value, limit=4_000)
        if not text:
            continue
        attribution = ""
        attribution_status: Literal[
            "not_applicable", "explicit", "inferred_reply_marker", "unknown"
        ]
        if kind == "reply_history":
            first_line = text.splitlines()[0]
            if QUOTE_DELIMITER_RE.match(first_line):
                attribution = first_line
                attribution_status = "explicit"
            else:
                attribution_status = "inferred_reply_marker"
        elif kind == "editorial_source":
            attribution_status = "not_applicable"
        else:
            attribution_status = "unknown"
        output.append(
            GmailQuotationEvidence(
                quote_kind=kind,
                text=text,
                attribution=attribution,
                attribution_status=attribution_status,
                truncated=truncated,
            )
        )
    return output[:20]


def _source_text_from_events(events: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    active_kind = "direct"
    for kind, value in events:
        if kind != active_kind:
            if active_kind != "direct":
                parts.append(f"\n[[end {active_kind.replace('_', ' ')}]]\n")
            if kind != "direct":
                parts.append(f"\n[[{kind.replace('_', ' ')}]]\n")
            active_kind = kind
        parts.append(value)
    if active_kind != "direct":
        parts.append(f"\n[[end {active_kind.replace('_', ' ')}]]\n")
    return "".join(parts)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", value))
    return html.unescape(" ".join(" ".join(parser.parts).split()))


def _text_for_normalization(value: str) -> tuple[str, list[str], bool]:
    """Convert possible HTML to plain text and collect hrefs without fetching."""

    if not HTML_TAG_RE.search(value):
        return value, [], False
    return _html_to_text(value), _links_from_html(value), bool(HTML_BLOCKQUOTE_RE.search(value))


def _links_from_html(value: str) -> list[str]:
    parser = _HTMLLinkExtractor()
    try:
        parser.feed(value)
    except Exception:
        return []
    return parser.links


def _clean_url(value: str) -> str:
    url = html.unescape(value.strip())
    while url and url[-1] in ".,;:!?)])}":
        url = url[:-1]
    if url.startswith("www."):
        url = f"https://{url}"
    return url


def _link_reasons(url: str) -> list[str]:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    lowered = url.lower()
    reasons: list[str] = []
    if parsed.scheme == "http":
        reasons.append("non-https link")
    if domain in SHORTENER_DOMAINS:
        reasons.append("shortened URL")
    if any(term in lowered for term in SUSPICIOUS_LINK_TERMS):
        reasons.append("credential or account-verification language in URL")
    return reasons


def _extract_links(*texts: str) -> list[GmailLinkRecord]:
    urls: list[str] = []
    for text in texts:
        urls.extend(_clean_url(match.group(0)) for match in URL_RE.finditer(text or ""))
    records: list[GmailLinkRecord] = []
    seen: set[str] = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        parsed = urlparse(url)
        reasons = _link_reasons(url)
        records.append(
            GmailLinkRecord(
                url=url,
                domain=parsed.netloc.lower(),
                suspicious=bool(reasons),
                reasons=reasons,
            )
        )
    return records


def _attachment_risk_flags(filename: str, mime_type: str, size_bytes: int) -> list[str]:
    lower_name = filename.lower()
    suffix = Path(lower_name).suffix
    flags: list[str] = []
    if suffix in RISKY_ATTACHMENT_EXTENSIONS:
        flags.append("risky_attachment_type")
    if suffix in MACRO_ATTACHMENT_EXTENSIONS:
        flags.append("macro_enabled_attachment")
    if suffix in ARCHIVE_ATTACHMENT_EXTENSIONS:
        flags.append("archive_attachment")
    if "executable" in mime_type.lower():
        flags.append("executable_attachment")
    if size_bytes >= 10_000_000:
        flags.append("large_attachment")
    return list(dict.fromkeys(flags))


def _attachment_metadata(part: dict[str, Any]) -> GmailAttachmentMetadata | None:
    body = part.get("body", {}) if isinstance(part.get("body"), dict) else {}
    filename = str(part.get("filename") or "").strip()
    attachment_id_present = bool(body.get("attachmentId"))
    if not filename and not attachment_id_present:
        return None
    mime_type = str(part.get("mimeType") or "")
    size_bytes = int(body.get("size") or 0)
    return GmailAttachmentMetadata(
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        attachment_id_present=attachment_id_present,
        risk_flags=_attachment_risk_flags(filename, mime_type, size_bytes),
    )


def _gmail_draft_attachment_file(path_value: str) -> tuple[Path, bytes, str]:
    workspace = Path.cwd().resolve()
    configured = context_env_value(
        "KEYSTONE_PRESENTATION_DERIVED_ROOT", "artifacts/presentation-derived"
    ).strip()
    configured_path = Path(configured).expanduser()
    root = (
        configured_path.resolve()
        if configured_path.is_absolute()
        else (workspace / configured_path).resolve()
    )
    candidate = Path(str(path_value or "").strip()).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if target.parent != root:
        raise RuntimeError("Gmail draft attachments are limited to the derived-artifact root.")
    if not target.is_file() or target.suffix.lower() not in {".png", ".pdf"}:
        raise RuntimeError("The exact Gmail draft attachment must be an available PNG or PDF.")
    content = target.read_bytes()
    if not content or len(content) > 10_000_000:
        raise RuntimeError("Gmail draft attachment must be between 1 byte and 10 MB.")
    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return target, content, mime_type


def _walk_payload_parts(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts", []) or []:
        if isinstance(child, dict):
            parts.extend(_walk_payload_parts(child))
    return parts


def _draft_attachment_refs(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return []
    refs: list[dict[str, Any]] = []
    for part in _walk_payload_parts(payload):
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        attachment_id = str(body.get("attachmentId") or "").strip()
        filename = str(part.get("filename") or "").strip()
        if not attachment_id or not filename:
            continue
        refs.append(
            {
                "attachment_id": attachment_id,
                "filename": filename,
                "mime_type": str(part.get("mimeType") or ""),
                "size": int(body.get("size") or 0),
            }
        )
    return refs


def _strip_quoted_reply(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    kept: list[str] = []
    stripped = False
    for line in lines:
        clean_line = line.strip()
        if QUOTE_DELIMITER_RE.match(clean_line):
            stripped = True
            break
        if clean_line.startswith(">"):
            stripped = True
            continue
        kept.append(line)
    normalized = "\n".join(kept).strip()
    return normalized, stripped


def _normalize_body(text: str) -> tuple[str, bool]:
    stripped, quote_stripped = _strip_quoted_reply(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.split()) for line in stripped.splitlines()]
    normalized = "\n".join(line for line in lines if line).strip()
    return normalized.replace("\u2014", "-"), quote_stripped


def _plain_source_events(value: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    reply_history = False
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not reply_history and QUOTE_DELIMITER_RE.match(line):
            reply_history = True
            events.append(("reply_history", line + "\n"))
            continue
        if reply_history:
            events.append(("reply_history", line.lstrip("> ") + "\n"))
        elif line.startswith(">"):
            events.append(("ambiguous", line.lstrip("> ") + "\n"))
        else:
            events.append(("direct", raw_line + "\n"))
    return events


def _body_evidence_from_events(
    *,
    part_path: str,
    mime_type: str,
    container_mime_type: str = "",
    alternative_group: str = "",
    representation: Literal["plain", "html", "other_text"],
    events: list[tuple[str, str]],
    structure_annotations: bool = False,
    content_complete: bool = True,
    limitations: list[str] | None = None,
) -> GmailBodyEvidence:
    direct_text, direct_truncated = _bounded_source_text(
        "".join(value for kind, value in events if kind == "direct"),
        limit=GMAIL_BODY_PART_MAX_CHARS,
    )
    triage_text, triage_truncated = _bounded_source_text(
        "".join(
            value
            for kind, value in events
            if kind in {"direct", "editorial_source"}
        ),
        limit=GMAIL_BODY_PART_MAX_CHARS,
    )
    source_text, source_truncated = _bounded_source_text(
        _source_text_from_events(events),
        limit=GMAIL_BODY_PART_MAX_CHARS,
    )
    truncated = direct_truncated or triage_truncated or source_truncated
    evidence_limitations = list(limitations or [])
    if truncated:
        evidence_limitations.append(
            f"This MIME representation was truncated to {GMAIL_BODY_PART_MAX_CHARS} characters."
        )
    return GmailBodyEvidence(
        part_path=part_path,
        mime_type=mime_type,
        container_mime_type=container_mime_type,
        alternative_group=alternative_group,
        representation=representation,
        role="single_representation",
        direct_text=direct_text,
        triage_text=triage_text,
        source_text=source_text,
        quotations=_quoted_segments_from_events(events),
        structure_annotations=structure_annotations,
        content_complete=content_complete and not truncated,
        truncated=truncated,
        limitations=list(dict.fromkeys(evidence_limitations))[:10],
    )


def _plain_body_evidence(
    part_path: str,
    mime_type: str,
    value: str,
    *,
    container_mime_type: str = "",
    alternative_group: str = "",
    content_complete: bool = True,
    limitations: list[str] | None = None,
) -> GmailBodyEvidence:
    return _body_evidence_from_events(
        part_path=part_path,
        mime_type=mime_type,
        container_mime_type=container_mime_type,
        alternative_group=alternative_group,
        representation="plain" if mime_type == "text/plain" else "other_text",
        events=_plain_source_events(value),
        content_complete=content_complete,
        limitations=limitations,
    )


def _html_body_evidence(
    part_path: str,
    value: str,
    *,
    container_mime_type: str = "",
    alternative_group: str = "",
    decoded_complete: bool = True,
    decoding_limitations: list[str] | None = None,
) -> GmailBodyEvidence:
    parser = _HTMLSourceExtractor()
    limitations: list[str] = list(decoding_limitations or [])
    try:
        parser.feed(value)
        parser.close()
        events = parser.events
        content_complete = parser.content_complete and decoded_complete
        limitations.extend(parser.limitations)
        if parser.unread_media_count:
            limitations.append(
                f"{parser.unread_media_count} inline visual or media element(s) are "
                "present but were not interpreted."
            )
        structure_annotations = parser.structure_annotations
    except Exception:
        events = [("direct", _html_to_text(value))]
        content_complete = False
        structure_annotations = False
        limitations.append(
            "Malformed HTML required a plain-text fallback; quotation and table "
            "structure may be incomplete."
        )
    return _body_evidence_from_events(
        part_path=part_path,
        mime_type="text/html",
        container_mime_type=container_mime_type,
        alternative_group=alternative_group,
        representation="html",
        events=events,
        structure_annotations=structure_annotations,
        content_complete=content_complete,
        limitations=limitations,
    )


def _walk_payload_parts_with_paths(
    part: dict[str, Any],
    path: str = "0",
    *,
    container_mime_type: str = "",
    alternative_group: str = "",
) -> list[tuple[str, dict[str, Any], str, str]]:
    parts = [(path, part, container_mime_type, alternative_group)]
    mime_type = str(part.get("mimeType") or "").lower()
    child_alternative_group = path if mime_type == "multipart/alternative" else alternative_group
    for index, child in enumerate(part.get("parts", []) or [], start=1):
        if isinstance(child, dict):
            parts.extend(
                _walk_payload_parts_with_paths(
                    child,
                    f"{path}.{index}",
                    container_mime_type=mime_type,
                    alternative_group=child_alternative_group,
                )
            )
    return parts


def _comparison_tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+|\$-?\d+(?:\.\d+)?", value.lower()))


def _critical_tokens(value: str) -> set[str]:
    return set(
        re.findall(
            r"\$?-?\d+(?:\.\d+)?|\b(?:not|no|never|only|unless|if|before|after)\b",
            value.lower(),
        )
    )


def _select_body_evidence(
    evidence: list[GmailBodyEvidence],
) -> tuple[list[GmailBodyEvidence], list[int], bool, list[str]]:
    candidates = [index for index, item in enumerate(evidence) if item.triage_text]
    if not candidates:
        return evidence, [], False, []
    updated = list(evidence)
    selected_indices: list[int] = []
    conflict = False
    limitations: list[str] = []
    alternative_groups: dict[str, list[int]] = {}
    for index in candidates:
        group = evidence[index].alternative_group
        if group:
            alternative_groups.setdefault(group, []).append(index)

    grouped_indices = {
        index for indices in alternative_groups.values() for index in indices
    }
    for _group, indices in alternative_groups.items():
        selected = indices[0]
        plain_index = next(
            (index for index in indices if evidence[index].representation == "plain"),
            None,
        )
        html_index = next(
            (index for index in indices if evidence[index].representation == "html"),
            None,
        )
        if plain_index is not None and html_index is not None:
            plain = evidence[plain_index].triage_text
            html_text = evidence[html_index].triage_text
            plain_tokens = _comparison_tokens(plain)
            html_tokens = _comparison_tokens(html_text)
            token_union = plain_tokens | html_tokens
            similarity = (
                len(plain_tokens & html_tokens) / len(token_union) if token_union else 1.0
            )
            critical_mismatch = _critical_tokens(plain) != _critical_tokens(html_text)
            html_materially_richer = bool(
                plain_tokens
                and plain_tokens <= html_tokens
                and len(html_text) > max(len(plain) + 40, int(len(plain) * 1.25))
            )
            plain_is_stub = bool(
                PLAIN_RENDERING_STUB_RE.search(plain)
                and len(html_text) > len(plain)
            )
            if plain_is_stub or html_materially_richer:
                selected = html_index
                limitations.append(
                    "The plain and HTML alternatives differed; the materially richer HTML "
                    "representation was selected for the triage view and both were retained."
                )
            elif similarity < 0.65 or critical_mismatch:
                selected = plain_index
                conflict = True
                limitations.append(
                    "The plain and HTML alternatives contain potentially conflicting "
                    "content; the plain representation remains the triage view and both "
                    "are retained without reconciliation."
                )
            else:
                selected = plain_index
        for index in indices:
            role = (
                "selected_triage_view"
                if index == selected
                else "alternate_representation"
            )
            updated[index] = updated[index].model_copy(update={"role": role})
        selected_indices.append(selected)

    coexisting = [index for index in candidates if index not in grouped_indices]
    if len(coexisting) == 1 and not alternative_groups:
        index = coexisting[0]
        updated[index] = updated[index].model_copy(update={"role": "single_representation"})
        selected_indices.append(index)
    else:
        for index in coexisting:
            updated[index] = updated[index].model_copy(update={"role": "coexisting_section"})
            selected_indices.append(index)

    return updated, sorted(selected_indices), conflict, limitations


def _extract_payload_content(payload: dict[str, Any] | None) -> _GmailPayloadExtraction:
    if not payload:
        return _GmailPayloadExtraction(
            normalized_body="",
            body_evidence=[],
            links=[],
            attachments=[],
            quote_stripped=False,
            body_content_status="empty",
            body_content_complete=False,
            limitations=["The Gmail payload did not contain a readable message body."],
        )

    evidence: list[GmailBodyEvidence] = []
    html_links: list[str] = []
    attachments: list[GmailAttachmentMetadata] = []
    limitations: list[str] = []
    omitted_parts = 0
    for (
        part_path,
        part,
        container_mime_type,
        alternative_group,
    ) in _walk_payload_parts_with_paths(payload):
        attachment = _attachment_metadata(part)
        if attachment is not None:
            attachments.append(attachment)

        mime_type = str(part.get("mimeType") or "").lower()
        body = part.get("body", {}) if isinstance(part.get("body"), dict) else {}
        body_data = body.get("data")
        filename = str(part.get("filename") or "").strip()
        if filename or body.get("attachmentId"):
            continue
        if mime_type.startswith(("image/", "audio/", "video/")):
            if len(evidence) < GMAIL_BODY_EVIDENCE_MAX_PARTS:
                evidence.append(
                    GmailBodyEvidence(
                        part_path=part_path,
                        mime_type=mime_type or "application/octet-stream",
                        container_mime_type=container_mime_type,
                        alternative_group=alternative_group,
                        representation="other_text",
                        role="single_representation",
                        source_text=f"[[{mime_type or 'inline media'} content not read]]",
                        content_complete=False,
                        limitations=[
                            "Inline visual or media content is present but was not interpreted."
                        ],
                    )
                )
            else:
                omitted_parts += 1
            continue
        if not body_data:
            continue
        decoded, decoded_complete, decoding_limitation = _decode_text_part(
            part,
            str(body_data),
        )
        if not decoded:
            if mime_type.startswith("text/"):
                limitations.append(
                    decoding_limitation
                    or f"Text MIME part {part_path} could not be decoded or was empty."
                )
            continue
        if len(evidence) >= GMAIL_BODY_EVIDENCE_MAX_PARTS:
            omitted_parts += 1
            continue
        if mime_type == "text/html":
            evidence.append(
                _html_body_evidence(
                    part_path,
                    decoded,
                    container_mime_type=container_mime_type,
                    alternative_group=alternative_group,
                    decoded_complete=decoded_complete,
                    decoding_limitations=(
                        [decoding_limitation] if decoding_limitation else []
                    ),
                )
            )
            html_links.extend(_links_from_html(decoded))
        elif mime_type == "text/plain" or (
            not part.get("parts") and mime_type.startswith("text/")
        ):
            evidence.append(
                _plain_body_evidence(
                    part_path,
                    mime_type,
                    decoded,
                    container_mime_type=container_mime_type,
                    alternative_group=alternative_group,
                    content_complete=decoded_complete,
                    limitations=[decoding_limitation] if decoding_limitation else [],
                )
            )

    if omitted_parts:
        limitations.append(
            f"{omitted_parts} additional MIME part(s) exceeded the "
            f"{GMAIL_BODY_EVIDENCE_MAX_PARTS}-part evidence limit."
        )
    if attachments:
        limitations.append(
            "Attachment bodies were not ingested; only bounded attachment metadata is available."
        )
    evidence, selected_indices, conflict, selection_limitations = _select_body_evidence(
        evidence
    )
    limitations.extend(selection_limitations)

    total_chars = 0
    bounded_evidence: list[GmailBodyEvidence] = []
    for item in evidence:
        remaining = max(0, GMAIL_BODY_EVIDENCE_MAX_CHARS - total_chars)
        source_text, total_truncated = _bounded_source_text(
            item.source_text,
            limit=min(GMAIL_BODY_PART_MAX_CHARS, remaining),
        ) if remaining else ("", bool(item.source_text))
        total_chars += len(source_text)
        if total_truncated:
            limitations.append(
                f"MIME part {item.part_path} exceeded the total bounded source-evidence limit."
            )
        bounded_evidence.append(
            item.model_copy(
                update={
                    "source_text": source_text,
                    "truncated": item.truncated or total_truncated,
                    "content_complete": item.content_complete and not total_truncated,
                }
            )
        )
    evidence = bounded_evidence

    selected = [
        evidence[index] for index in selected_indices if index < len(evidence)
    ]
    normalized = "\n".join(item.triage_text for item in selected if item.triage_text)
    quote_stripped = bool(
        selected
        and any(
            quote.quote_kind in {"reply_history", "ambiguous"}
            for item in selected
            for quote in item.quotations
        )
    )
    incomplete = bool(
        omitted_parts
        or limitations
        or any(not item.content_complete for item in evidence)
    )
    if not evidence or not any(item.source_text for item in evidence):
        status: Literal["complete", "partial", "conflicting", "empty"] = "empty"
        limitations.append("No readable text representation was available in the Gmail payload.")
    elif conflict:
        status = "conflicting"
    elif incomplete:
        status = "partial"
    else:
        status = "complete"
    links = _extract_links(
        *(item.source_text for item in evidence),
        *html_links,
    )
    return _GmailPayloadExtraction(
        normalized_body=normalized,
        body_evidence=evidence,
        links=links,
        attachments=attachments,
        quote_stripped=quote_stripped,
        body_content_status=status,
        body_content_complete=status == "complete",
        limitations=list(dict.fromkeys(item for item in limitations if item)),
    )


def _payload_raw_text(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in _walk_payload_parts(payload):
        mime_type = str(part.get("mimeType") or "").lower()
        body = part.get("body", {}) if isinstance(part.get("body"), dict) else {}
        body_data = body.get("data")
        if not body_data:
            continue
        decoded, _complete, _limitation = _decode_text_part(part, str(body_data))
        decoded = decoded.strip()
        if not decoded:
            continue
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)
        elif not part.get("parts") and mime_type.startswith("text/"):
            plain_parts.append(decoded)
    if plain_parts:
        return "\n".join(plain_parts).strip()
    return "\n".join(_html_to_text(part) for part in html_parts).strip()


def _labeled_quoted_context(raw_text: str) -> list[str]:
    lines = [line.strip() for line in raw_text.replace("\r", "").splitlines()]
    quote_index = next(
        (index for index, line in enumerate(lines) if QUOTE_DELIMITER_RE.match(line)),
        None,
    )
    if quote_index is None:
        return []
    quoted = lines[quote_index + 1 :]
    labels = {
        "i'm interested in": "Original interest",
        "i’m interested in": "Original interest",
        "message": "Original message",
    }
    stop_labels = {
        "view submission in hubspot",
        "this email was sent to",
        "do you want to stop receiving these emails?",
        "hubspot, inc.",
    }
    extracted: list[str] = []
    for index, line in enumerate(quoted):
        label = labels.get(line.lower())
        if label is None:
            continue
        values: list[str] = []
        for candidate in quoted[index + 1 :]:
            lowered = candidate.lower()
            if lowered in labels or any(lowered.startswith(stop) for stop in stop_labels):
                break
            if candidate:
                values.append(candidate)
            if len(" ".join(values)) >= 500:
                break
        value = _normalize_summary_text(" ".join(values))
        if value:
            extracted.append(f"{label}: {value[:500]}")
    return _unique_nonempty(extracted, limit=4)


def _thread_prior_context(raw_messages: list[Mapping[str, Any]]) -> list[str]:
    context: list[str] = []
    for message in raw_messages:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else None
        context.extend(_labeled_quoted_context(_payload_raw_text(payload)))
    return _unique_nonempty(context, limit=6)


def _payload_text_links_and_attachments(
    payload: dict[str, Any] | None,
) -> tuple[str, list[GmailLinkRecord], list[GmailAttachmentMetadata], bool]:
    extraction = _extract_payload_content(payload)
    return (
        extraction.normalized_body,
        extraction.links,
        extraction.attachments,
        extraction.quote_stripped,
    )


def _extract_text_from_payload(payload: dict[str, Any] | None) -> str:
    text, _, _, _ = _payload_text_links_and_attachments(payload)
    return text


def _header(headers: list[dict[str, Any]], key: str) -> str:
    key_lower = key.lower()
    for header in headers:
        if str(header.get("name", "")).lower() == key_lower:
            return str(header.get("value") or "")
    return ""


def _received_at(data: Mapping[str, Any], headers: list[dict[str, Any]]) -> str:
    internal_date = str(data.get("internalDate") or "").strip()
    if internal_date.isdigit():
        timestamp = int(internal_date) / 1000
        return (
            datetime.fromtimestamp(timestamp, UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    date_header = _header(headers, "Date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return ""


def _thread_summary(snippet: str, body: str) -> str:
    source = " ".join((snippet or body).split())
    if not source:
        return "Thread context unavailable; only the selected message metadata was available."
    if len(source) <= 220:
        return source
    return f"{source[:217].rstrip()}..."


def _normalize_summary_text(value: str) -> str:
    return " ".join(value.replace("\u2014", "-").split()).strip()


def _unique_nonempty(values: list[str], *, limit: int | None = None) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _normalize_summary_text(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique.append(cleaned)
        if limit is not None and len(unique) >= limit:
            break
    return unique


def _thread_sentences(*texts: str) -> list[str]:
    sentences: list[str] = []
    for text in texts:
        for raw in SENTENCE_SPLIT_RE.split(text or ""):
            cleaned = _normalize_summary_text(raw)
            if cleaned:
                sentences.append(cleaned)
    return sentences


def _thread_participants(envelopes: list[GmailMessageEnvelope]) -> list[str]:
    values: list[str] = []
    for envelope in envelopes:
        sender = envelope.sender_name or envelope.sender_email
        if sender:
            values.append(sender)
    return _unique_nonempty(values, limit=6)


def _thread_action_items(envelopes: list[GmailMessageEnvelope]) -> list[str]:
    items: list[str] = []
    for envelope in envelopes:
        for sentence in _thread_sentences(envelope.normalized_body, envelope.snippet):
            if THREAD_ACTION_RE.search(sentence) and not _looks_like_promotional_cta(
                sentence,
                envelope,
            ):
                items.append(sentence)
    return _unique_nonempty(items, limit=5)


def _looks_like_promotional_cta(sentence: str, envelope: GmailMessageEnvelope) -> bool:
    lowered = sentence.lower()
    if any(marker in lowered for marker in PROMOTIONAL_CTA_MARKERS):
        return True
    return bool(
        "CATEGORY_PROMOTIONS" in envelope.prior_labels
        and not lowered.endswith("?")
        and re.search(r"\b(?:discover|submit|profile|listing|platform)\b", lowered)
    )


def _thread_deadlines(envelopes: list[GmailMessageEnvelope]) -> list[str]:
    items: list[str] = []
    for envelope in envelopes:
        for sentence in _thread_sentences(envelope.normalized_body, envelope.snippet):
            if THREAD_DEADLINE_RE.search(sentence):
                items.append(sentence)
    return _unique_nonempty(items, limit=5)


def _thread_open_questions(envelopes: list[GmailMessageEnvelope]) -> list[str]:
    questions: list[str] = []
    for envelope in envelopes:
        for sentence in _thread_sentences(envelope.normalized_body, envelope.snippet):
            if sentence.endswith("?"):
                questions.append(sentence)
    return _unique_nonempty(questions, limit=5)


def _latest_message_closes_exchange(envelopes: list[GmailMessageEnvelope]) -> bool:
    if not envelopes:
        return False
    latest = " ".join(
        [envelopes[-1].normalized_body, envelopes[-1].snippet]
    ).lower()
    return bool(
        re.search(r"\b(?:thank|thanks|many thanks|appreciate)\b", latest)
        and re.search(r"\b(?:reach out|keep in touch|stay in touch)\b", latest)
        and re.search(r"\b(?:collaborat|opportunit|future)\w*\b", latest)
        and "?" not in latest
    )


def _thread_level_limitations(envelopes: list[GmailMessageEnvelope]) -> list[str]:
    limitations = [
        "Read-only thread summary used sanitized Gmail message bodies from the selected thread."
    ]
    if any(
        "CATEGORY_PROMOTIONS" in envelope.prior_labels
        and any(
            marker in " ".join([envelope.normalized_body, envelope.snippet]).lower()
            for marker in PROMOTIONAL_CTA_MARKERS
        )
        for envelope in envelopes
    ):
        limitations.append(
            "Promotional or onboarding CTAs were not treated as operator action items."
        )
    if any(envelope.attachment_metadata for envelope in envelopes):
        limitations.append("Attachments were not ingested; metadata only was screened.")
    if any(
        any(
            "retained separately as labeled source evidence" in limitation
            for limitation in envelope.triage_limitations
        )
        for envelope in envelopes
    ):
        limitations.append(
            "Quoted content excluded from latest-sender summary extraction remains "
            "available as labeled per-message source evidence."
        )
    return limitations


def _thread_overview(
    envelopes: list[GmailMessageEnvelope],
    *,
    chronology_available: bool = True,
) -> tuple[str, list[str], list[str], list[str], list[str]]:
    if not envelopes:
        return (
            "Thread context unavailable; no Gmail messages were returned.",
            [],
            [],
            [],
            [],
        )
    participants = _thread_participants(envelopes)
    action_items = _thread_action_items(envelopes)
    deadlines = _thread_deadlines(envelopes)
    open_questions = _thread_open_questions(envelopes)
    exchange_closed = chronology_available and _latest_message_closes_exchange(envelopes)
    if exchange_closed:
        action_items = []
        open_questions = []
    latest = envelopes[-1] if chronology_available else None
    subject = (
        latest.subject
        if latest is not None and latest.subject
        else next((item.subject for item in envelopes if item.subject), "")
    )
    if not chronology_available:
        fragments = [
            "Thread chronology unavailable because one or more provider message dates "
            "were missing or invalid; no entry was labeled latest or initial."
        ]
        if subject:
            fragments.append(f"Thread about {subject}.")
        if participants:
            fragments.append(f"Participants: {', '.join(participants[:3])}.")
        return (
            _thread_summary("", " ".join(fragments)),
            participants,
            action_items,
            deadlines,
            open_questions,
        )
    assert latest is not None
    latest_points = _thread_sentences(latest.normalized_body, latest.snippet)
    initial_points = _thread_sentences(
        envelopes[0].normalized_body,
        envelopes[0].snippet,
    )
    recent_points = _unique_nonempty(
        [
            *(latest_points[:1]),
            *([] if exchange_closed else initial_points[:1]),
        ],
        limit=2,
    )
    fragments: list[str] = []
    if recent_points:
        fragments.append(f"Latest status: {recent_points[0]}")
        if len(recent_points) > 1:
            fragments.append(f"Initial context: {recent_points[1]}")
    elif latest.thread_summary:
        fragments.append(f"Latest status: {latest.thread_summary}")
    if subject:
        fragments.append(f"Thread about {subject}.")
    if participants:
        fragments.append(f"Participants: {', '.join(participants[:3])}.")
    summary = _thread_summary("", " ".join(fragments))
    return summary, participants, action_items, deadlines, open_questions


def _envelope_suspicious_signals(
    links: list[GmailLinkRecord],
    attachments: list[GmailAttachmentMetadata],
) -> list[str]:
    signals: list[str] = []
    for link in links:
        for reason in link.reasons:
            signals.append(f"Link {link.domain or link.url}: {reason}")
    for attachment in attachments:
        label = attachment.filename or attachment.mime_type or "attachment"
        for flag in attachment.risk_flags:
            signals.append(f"Attachment {label}: {flag}")
    return list(dict.fromkeys(signals))


def gmail_message_envelope_from_api(data: Mapping[str, Any]) -> GmailMessageEnvelope:
    """Normalize a Gmail API message into sanitized, LLM-ready context."""

    payload = data.get("payload", {}) if isinstance(data.get("payload"), dict) else {}
    headers = payload.get("headers", []) if isinstance(payload.get("headers"), list) else []
    extraction = _extract_payload_content(payload)
    body = extraction.normalized_body
    links = extraction.links
    attachments = extraction.attachments
    from_header = _header(headers, "From")
    sender_name, sender_email = parseaddr(from_header)
    snippet = str(data.get("snippet") or "")
    thread_summary = _thread_summary(snippet, body)
    limitations = [
        "Only the selected Gmail message was fetched; full thread history was not ingested."
    ]
    if attachments:
        limitations.append("Attachments were not ingested; metadata only was screened.")
    selected_evidence = [
        item
        for item in extraction.body_evidence
        if item.role
        in {"selected_triage_view", "single_representation", "coexisting_section"}
    ]
    quote_kinds = {
        quote.quote_kind
        for item in selected_evidence
        for quote in item.quotations
    }
    if "reply_history" in quote_kinds:
        limitations.append("Quoted prior replies were stripped before triage.")
    if "ambiguous" in quote_kinds:
        limitations.append(
            "Attribution-uncertain quoted lines were excluded from the latest-sender "
            "triage view."
        )
    if extraction.quote_stripped:
        limitations.append(
            "Quoted or attribution-uncertain content was retained separately as labeled "
            "source evidence."
        )
    limitations.extend(extraction.limitations)
    return GmailMessageEnvelope(
        message_id=str(data.get("id") or ""),
        thread_id=str(data.get("threadId") or ""),
        received_at=_received_at(data, headers),
        sender_name=sender_name,
        sender_email=sender_email or from_header,
        to=_header(headers, "To"),
        subject=_header(headers, "Subject"),
        snippet=snippet,
        prior_labels=[str(label) for label in data.get("labelIds", []) or []],
        normalized_body=body,
        body_evidence=extraction.body_evidence,
        body_content_status=extraction.body_content_status,
        body_content_complete=extraction.body_content_complete,
        extracted_links=links,
        attachment_metadata=attachments,
        thread_summary=thread_summary,
        thread_context=thread_summary,
        thread_message_count=1,
        suspicious_signals=_envelope_suspicious_signals(links, attachments),
        triage_limitations=limitations,
    )


def gmail_message_summary_from_api(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact Gmail metadata/snippet summary without reading the message body."""

    payload = data.get("payload", {}) if isinstance(data.get("payload"), dict) else {}
    headers = payload.get("headers", []) if isinstance(payload.get("headers"), list) else []
    from_header = _header(headers, "From")
    sender_name, sender_email = parseaddr(from_header)
    snippet = str(data.get("snippet") or "")
    summary = _thread_summary(snippet, "")
    return {
        "id": str(data.get("id") or ""),
        "threadId": str(data.get("threadId") or ""),
        "received_at": _received_at(data, headers),
        "from": from_header,
        "to": _header(headers, "To"),
        "sender_name": sender_name,
        "sender_email": sender_email or from_header,
        "subject": _header(headers, "Subject"),
        "snippet": snippet,
        "prior_labels": [str(label) for label in data.get("labelIds", []) or []],
        "labelIds": [str(label) for label in data.get("labelIds", []) or []],
        "thread_summary": summary,
        "thread_context": summary,
        "triage_limitations": ["Search summary only; full Gmail message body was not ingested."],
    }


def gmail_message_envelope_from_dict(message: Mapping[str, Any]) -> GmailMessageEnvelope:
    """Normalize a fixture or legacy flat Gmail message dictionary."""

    if isinstance(message.get("envelope"), Mapping):
        return GmailMessageEnvelope.model_validate(message["envelope"])
    sender_name, sender_email = parseaddr(str(message.get("from") or ""))
    body = str(message.get("normalized_body") or message.get("body") or "")
    attachments = [
        GmailAttachmentMetadata.model_validate(item)
        for item in (message.get("attachment_metadata") or message.get("attachments") or [])
        if isinstance(item, Mapping)
    ]
    body_evidence = [
        GmailBodyEvidence.model_validate(item)
        for item in message.get("body_evidence", []) or []
        if isinstance(item, Mapping)
    ]
    html_links: list[str] = []
    if not body_evidence and body:
        if HTML_TAG_RE.search(body):
            body_evidence = [_html_body_evidence("legacy.body", body)]
            html_links = _links_from_html(body)
        else:
            body_evidence = [_plain_body_evidence("legacy.body", "text/plain", body)]
    selected = [
        item
        for item in body_evidence
        if item.role
        in {"selected_triage_view", "single_representation", "coexisting_section"}
    ]
    if not selected and body_evidence:
        selected = [body_evidence[0]]
    normalized = (
        "\n".join(item.triage_text for item in selected if item.triage_text)
        if any(item.triage_text for item in selected)
        else _normalize_body(body)[0]
    )
    quote_stripped = bool(
        selected
        and any(
            quote.quote_kind in {"reply_history", "ambiguous"}
            for item in selected
            for quote in item.quotations
        )
    )
    links = _extract_links(
        *(item.source_text for item in body_evidence),
        *html_links,
    )
    snippet = str(message.get("snippet") or "")
    supplied_summary = str(message.get("thread_summary") or "")
    supplied_context = str(message.get("thread_context") or "")
    thread_summary_text, _ = _normalize_body(_text_for_normalization(supplied_summary)[0])
    thread_context_text, _ = _normalize_body(_text_for_normalization(supplied_context)[0])
    thread_summary = thread_summary_text or _thread_summary(snippet, normalized)
    limitations = [str(item) for item in message.get("triage_limitations", []) if str(item).strip()]
    quote_kinds = {
        quote.quote_kind for item in selected for quote in item.quotations
    }
    if "reply_history" in quote_kinds:
        limitations.append("Quoted prior replies were stripped before triage.")
    if "ambiguous" in quote_kinds:
        limitations.append(
            "Attribution-uncertain quoted lines were excluded from the latest-sender "
            "triage view."
        )
    if quote_stripped:
        limitations.append(
            "Quoted or attribution-uncertain content was retained separately as labeled "
            "source evidence."
        )
    if attachments:
        limitations.append("Attachments were not ingested; metadata only was screened.")
    return GmailMessageEnvelope(
        message_id=str(message.get("id") or message.get("message_id") or ""),
        thread_id=str(message.get("threadId") or message.get("thread_id") or ""),
        received_at=str(message.get("received_at") or ""),
        sender_name=sender_name or str(message.get("sender_name") or ""),
        sender_email=sender_email or str(message.get("sender_email") or ""),
        to=str(message.get("to") or ""),
        subject=str(message.get("subject") or ""),
        snippet=snippet,
        prior_labels=[
            str(label)
            for label in (message.get("labelIds") or message.get("prior_labels") or [])
            if str(label).strip()
        ],
        normalized_body=normalized,
        body_evidence=body_evidence,
        body_content_status=str(
            message.get("body_content_status")
            or (
                "empty"
                if not body_evidence
                else "partial"
                if any(not item.content_complete for item in body_evidence)
                else "complete"
            )
        ),
        body_content_complete=bool(
            message.get(
                "body_content_complete",
                bool(body_evidence) and all(item.content_complete for item in body_evidence),
            )
        ),
        extracted_links=links,
        attachment_metadata=attachments,
        thread_summary=thread_summary,
        thread_context=thread_context_text or thread_summary,
        thread_message_count=int(message.get("thread_message_count") or 1),
        suspicious_signals=_envelope_suspicious_signals(links, attachments),
        triage_limitations=limitations,
    )


def _body_evidence_projection(
    evidence: list[GmailBodyEvidence],
    *,
    max_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Return bounded sanitized source evidence without raw MIME bytes."""

    output: list[dict[str, Any]] = []
    remaining = max_chars
    truncated = False
    for item in evidence[:GMAIL_BODY_EVIDENCE_MAX_PARTS]:
        source_text, item_truncated = _bounded_source_text(
            item.source_text,
            limit=remaining,
        )
        remaining = max(0, remaining - len(source_text))
        truncated = truncated or item_truncated
        output.append(
            {
                "part_path": item.part_path,
                "mime_type": item.mime_type,
                "container_mime_type": item.container_mime_type,
                "alternative_group": item.alternative_group,
                "representation": item.representation,
                "role": item.role,
                "source_text": source_text,
                "quotations": [
                    {
                        "quote_kind": quote.quote_kind,
                        "text": "",
                        "attribution": quote.attribution,
                        "attribution_status": quote.attribution_status,
                        "truncated": quote.truncated,
                    }
                    for quote in item.quotations
                ],
                "structure_annotations": item.structure_annotations,
                "content_complete": item.content_complete and not item_truncated,
                "truncated": item.truncated or item_truncated,
                "limitations": list(item.limitations),
            }
        )
    if len(evidence) > GMAIL_BODY_EVIDENCE_MAX_PARTS:
        truncated = True
    return output, truncated


def _full_sanitized_body_source_records(
    payload: dict[str, Any] | None,
    evidence: list[GmailBodyEvidence],
) -> list[dict[str, Any]]:
    """Rebuild complete inline text representations before redaction and paging."""

    if not payload:
        return []
    parts_by_path = {
        path: (part, container_mime_type, alternative_group)
        for path, part, container_mime_type, alternative_group in (
            _walk_payload_parts_with_paths(payload)
        )
    }
    records: list[dict[str, Any]] = []
    for item in evidence[:GMAIL_BODY_EVIDENCE_MAX_PARTS]:
        source_text = item.source_text
        full_content_complete = item.content_complete and not item.truncated
        limitations = [
            limitation
            for limitation in item.limitations
            if "truncated to" not in limitation.lower()
        ]
        raw_part = parts_by_path.get(item.part_path)
        if raw_part is not None:
            part, _container_mime_type, _alternative_group = raw_part
            body = part.get("body", {}) if isinstance(part.get("body"), dict) else {}
            body_data = body.get("data")
            filename = str(part.get("filename") or "").strip()
            if body_data and not filename and not body.get("attachmentId"):
                decoded, decoded_complete, decoding_limitation = _decode_text_part(
                    part,
                    str(body_data),
                )
                if decoding_limitation:
                    limitations.append(decoding_limitation)
                mime_type = str(part.get("mimeType") or "").lower()
                if mime_type == "text/html":
                    parser = _HTMLSourceExtractor()
                    try:
                        parser.feed(decoded)
                        parser.close()
                        source_text = _compact_source_text(
                            _source_text_from_events(parser.events)
                        )
                        full_content_complete = (
                            decoded_complete and parser.content_complete
                        )
                        limitations.extend(parser.limitations)
                        if parser.unread_media_count:
                            limitations.append(
                                f"{parser.unread_media_count} inline visual or media "
                                "element(s) are present but were not interpreted."
                            )
                    except Exception:
                        source_text = _compact_source_text(_html_to_text(decoded))
                        full_content_complete = False
                        limitations.append(
                            "Malformed HTML required a plain-text fallback; quotation "
                            "and table structure may be incomplete."
                        )
                elif mime_type.startswith("text/"):
                    source_text = _compact_source_text(
                        _source_text_from_events(_plain_source_events(decoded))
                    )
                    full_content_complete = decoded_complete
        sanitized_text = redact_secret_like_text(source_text)
        enforce_public_source_output_guardrails(
            "gmail_get_message_context_projection_full_source",
            {"source_text": sanitized_text},
        )
        records.append(
            {
                "part_path": item.part_path,
                "mime_type": item.mime_type,
                "container_mime_type": item.container_mime_type,
                "alternative_group": item.alternative_group,
                "representation": item.representation,
                "role": item.role,
                "source_text": sanitized_text,
                "quotations": [
                    {
                        "quote_kind": quote_item.quote_kind,
                        "text": "",
                        "attribution": redact_secret_like_text(
                            quote_item.attribution
                        ),
                        "attribution_status": quote_item.attribution_status,
                        "truncated": quote_item.truncated,
                    }
                    for quote_item in item.quotations
                ],
                "structure_annotations": item.structure_annotations,
                "full_content_complete": full_content_complete,
                "limitations": list(
                    dict.fromkeys(value for value in limitations if value)
                )[:10],
                "redaction_applied": sanitized_text != source_text,
            }
        )
    return records


def _gmail_source_snapshot_sha256(
    *,
    message_id: str,
    thread_id: str,
    history_id: str,
    internal_date: str,
    records: list[dict[str, Any]],
) -> str:
    snapshot = {
        "message_id": message_id,
        "thread_id": thread_id,
        "history_id": history_id,
        "internal_date": internal_date,
        "records": [
            {
                key: value
                for key, value in record.items()
                if key != "redaction_applied"
            }
            for record in records
        ],
    }
    return hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _load_json_file(path: Path, *, description: str) -> dict[str, Any]:
    if not path.exists():
        raise GmailConfigurationError(
            f"{description} not found: {path}. Configure GOOGLE_TOKEN_FILE and "
            "GOOGLE_CREDENTIALS_FILE before using --live-gmail."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GmailConfigurationError(f"{description} is invalid JSON: {path}.") from exc
    if not isinstance(data, dict):
        raise GmailConfigurationError(f"{description} must contain a JSON object: {path}.")
    return data


def _path_from_env(env: Mapping[str, str], name: str, default: str) -> Path:
    value = str(env.get(name) or "").strip()
    return Path(value or default)


def _live_gmail_enabled(env: Mapping[str, str]) -> bool:
    value = str(env.get("KEYSTONE_ENABLE_LIVE_GMAIL") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _safe_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except OSError:
        return None, "unreadable"
    if not isinstance(payload, dict):
        return None, "not_object"
    return payload, None


def _credentials_have_client_config(credentials: dict[str, Any] | None) -> bool:
    if not credentials:
        return False
    client_config = credentials.get("installed") or credentials.get("web") or {}
    if not isinstance(client_config, dict):
        return False
    return bool(client_config.get("client_id") and client_config.get("client_secret"))


def _token_has_auth_material(token_data: dict[str, Any] | None) -> bool:
    if not token_data:
        return False
    return bool(
        token_data.get("token") or token_data.get("access_token") or token_data.get("refresh_token")
    )


def _token_has_refresh_token(token_data: dict[str, Any] | None) -> bool:
    if not token_data:
        return False
    return bool(str(token_data.get("refresh_token") or "").strip())


def _gmail_oauth_reauth_guidance() -> str:
    return (
        "Refresh local Gmail OAuth with "
        f"`{GMAIL_OAUTH_REAUTH_COMMAND}` after confirming "
        "GOOGLE_CREDENTIALS_FILE points to the Desktop OAuth client JSON."
    )


def _token_refresh_failure_detail(response: Any) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    error = str(payload.get("error") or "").strip().lower()
    description = str(payload.get("error_description") or "").strip()
    if error == "invalid_grant":
        return "The saved Gmail refresh token is expired, revoked, or no longer valid."
    if error == "invalid_client":
        return "The Gmail OAuth client credentials do not match the saved token."
    if description:
        return f"Token endpoint detail: {description}"
    if error:
        return f"Token endpoint error: {error}"
    return ""


def gmail_oauth_readiness(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return offline Gmail OAuth readiness without exposing secrets or starting OAuth."""

    source = os.environ if env is None else env
    live_enabled = _live_gmail_enabled(source)
    token_path = _path_from_env(source, "GOOGLE_TOKEN_FILE", DEFAULT_GOOGLE_TOKEN_FILE)
    credentials_path = _path_from_env(
        source,
        "GOOGLE_CREDENTIALS_FILE",
        DEFAULT_GOOGLE_CREDENTIALS_FILE,
    )
    token_present = token_path.is_file()
    credentials_present = credentials_path.is_file()

    details: dict[str, Any] = {
        "readiness": "disabled",
        "live_enabled": live_enabled,
        "configured": False,
        "token_file_path": str(token_path),
        "credentials_file_path": str(credentials_path),
        "token_file_present": token_present,
        "credentials_file_present": credentials_present,
        "token_file_valid": False,
        "credentials_file_valid": False,
        "token_has_access_or_refresh_token": False,
        "token_has_refresh_token": False,
        "credentials_have_client_config": False,
        "allowed_live_operations": list(GMAIL_LIVE_OPERATIONS),
        "send_supported": False,
        "messages": [],
    }
    if not live_enabled:
        details["messages"] = [
            "Live Gmail mode is disabled; OAuth files are optional for dry-run use."
        ]
        return details

    token_data, token_error = _safe_json_object(token_path)
    credentials_data, credentials_error = _safe_json_object(credentials_path)
    details["token_file_valid"] = token_error is None
    details["credentials_file_valid"] = credentials_error is None
    details["token_has_access_or_refresh_token"] = _token_has_auth_material(token_data)
    details["token_has_refresh_token"] = _token_has_refresh_token(token_data)
    details["credentials_have_client_config"] = _credentials_have_client_config(credentials_data)

    messages: list[str] = []
    if token_error:
        messages.append(f"GOOGLE_TOKEN_FILE is {token_error}: {token_path}")
    if credentials_error:
        messages.append(f"GOOGLE_CREDENTIALS_FILE is {credentials_error}: {credentials_path}")
    if token_data is not None and not details["token_has_access_or_refresh_token"]:
        messages.append("Gmail OAuth token file lacks access_token, token, or refresh_token.")
    if token_data is not None and not details["token_has_refresh_token"]:
        messages.append(
            "Gmail OAuth token file lacks refresh_token, so Keystone cannot recover "
            f"from HTTP 401 automatically. {_gmail_oauth_reauth_guidance()}"
        )
    if credentials_data is not None and not details["credentials_have_client_config"]:
        messages.append(
            "Gmail OAuth credentials file lacks installed/web client_id and client_secret."
        )

    ready = not messages
    details["readiness"] = "ready" if ready else "misconfigured"
    details["configured"] = ready
    details["messages"] = messages or ["Live Gmail OAuth files are present and structurally valid."]
    return details


@dataclass
class GmailTool:
    """Draft-only Gmail API wrapper.

    In dry-run mode, methods return inert local results. In live mode, OAuth token
    files are required.
    The access token is never printed or returned.
    """

    live: bool = False
    token_file: str | Path | None = None
    credentials_file: str | Path | None = None
    access_token: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    session: Any = field(default_factory=requests.Session)
    api_base_url: str = GMAIL_API_BASE_URL
    _label_name_to_id: dict[str, str] = field(default_factory=dict, init=False)

    def _token_path(self) -> Path:
        return Path(self.token_file or os.getenv("GOOGLE_TOKEN_FILE", DEFAULT_GOOGLE_TOKEN_FILE))

    def _credentials_path(self) -> Path:
        return Path(
            self.credentials_file
            or os.getenv("GOOGLE_CREDENTIALS_FILE", DEFAULT_GOOGLE_CREDENTIALS_FILE)
        )

    def _oauth_token_data(self) -> dict[str, Any]:
        return _load_json_file(self._token_path(), description="Gmail OAuth token file")

    def _client_credentials(self, token_data: dict[str, Any]) -> tuple[str, str, str]:
        client_id = str(token_data.get("client_id") or "")
        client_secret = str(token_data.get("client_secret") or "")
        token_uri = str(token_data.get("token_uri") or GOOGLE_TOKEN_URL)

        if client_id and client_secret:
            return client_id, client_secret, token_uri

        credentials = _load_json_file(
            self._credentials_path(),
            description="Gmail OAuth credentials file",
        )
        client_config = credentials.get("installed") or credentials.get("web") or {}
        if not isinstance(client_config, dict):
            client_config = {}
        client_id = client_id or str(client_config.get("client_id") or "")
        client_secret = client_secret or str(client_config.get("client_secret") or "")
        token_uri = token_uri or str(client_config.get("token_uri") or GOOGLE_TOKEN_URL)

        if not client_id or not client_secret:
            raise GmailConfigurationError(
                "Gmail OAuth credentials are missing client_id or client_secret. "
                "Download a Desktop OAuth client JSON as credentials.json."
            )
        return client_id, client_secret, token_uri

    def _refresh_access_token(self, token_data: dict[str, Any]) -> str:
        refresh_token = str(token_data.get("refresh_token") or "")
        if not refresh_token:
            raise GmailConfigurationError(
                "Gmail OAuth token file does not contain a refresh token, so Keystone "
                "cannot recover from expired or revoked access tokens. "
                f"{_gmail_oauth_reauth_guidance()}"
            )
        client_id, client_secret, token_uri = self._client_credentials(token_data)
        try:
            response = self.session.post(
                token_uri,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise GmailConfigurationError(
                "Unable to refresh Gmail OAuth token due to a network error. "
                "Retry the command, or reauthorize if the token remains invalid. "
                f"{_gmail_oauth_reauth_guidance()}"
            ) from exc

        if getattr(response, "status_code", 0) >= 400:
            detail = _token_refresh_failure_detail(response)
            raise GmailConfigurationError(
                "Unable to refresh Gmail OAuth token. "
                + (f"{detail} " if detail else "")
                + f"Token endpoint returned HTTP {response.status_code}. "
                + _gmail_oauth_reauth_guidance()
            )
        try:
            refreshed = response.json()
        except ValueError as exc:
            raise GmailConfigurationError(
                f"Gmail OAuth token refresh returned invalid JSON. {_gmail_oauth_reauth_guidance()}"
            ) from exc

        token = str(refreshed.get("access_token") or "")
        if not token:
            raise GmailConfigurationError(
                "Gmail OAuth token refresh returned no access token. "
                f"{_gmail_oauth_reauth_guidance()}"
            )

        updated = {**token_data, **refreshed}
        try:
            self._token_path().write_text(
                json.dumps(updated, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            raise GmailConfigurationError(
                "Refreshed Gmail OAuth token could not be written to disk. "
                "Check GOOGLE_TOKEN_FILE permissions and retry."
            ) from exc
        return token

    def _force_refresh_access_token(self) -> str:
        token = self._refresh_access_token(self._oauth_token_data())
        self.access_token = token
        return token

    def _access_token(self) -> str:
        if self.access_token:
            return self.access_token
        token_data = self._oauth_token_data()
        token = str(token_data.get("token") or token_data.get("access_token") or "")
        if token:
            self.access_token = token
            return token
        self.access_token = self._refresh_access_token(token_data)
        return self.access_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        allow_not_found: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.live:
            raise GmailConfigurationError("Live Gmail mode is disabled.")

        if (os.environ.get("KEYSTONE_CANARY_GMAIL_READ_ONLY") == "true"
            or os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE")):
            from keystone_agents.canary_acceptance import gmail_read_guard

            gmail_read_guard(method, self._token_path(), self.api_base_url)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._access_token()}"
        headers.setdefault("Accept", "application/json")
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")

        url = f"{self.api_base_url.rstrip('/')}/{path.lstrip('/')}"
        response = None
        for attempt in range(2):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
            except requests.Timeout as exc:
                raise GmailAPIError(
                    "Gmail API timed out after "
                    f"{self.timeout_seconds:.1f} seconds during {operation}."
                ) from exc
            except requests.RequestException as exc:
                raise GmailAPIError(f"Gmail API request failed during {operation}.") from exc
            if getattr(response, "status_code", 0) != 401 or attempt == 1:
                break
            try:
                refreshed_token = self._force_refresh_access_token()
            except GmailConfigurationError as exc:
                raise GmailConfigurationError(
                    f"Gmail API returned HTTP 401 during {operation}. {exc}"
                ) from exc
            headers["Authorization"] = f"Bearer {refreshed_token}"

        if getattr(response, "status_code", 0) >= 400:
            if getattr(response, "status_code", 0) == 404 and allow_not_found:
                return {"_not_found": True}
            if getattr(response, "status_code", 0) == 401:
                raise GmailConfigurationError(
                    f"Gmail API returned HTTP 401 during {operation} even after refreshing "
                    "the OAuth access token. " + _gmail_oauth_reauth_guidance()
                )
            raise GmailAPIError(
                f"Gmail API returned HTTP {response.status_code} during {operation}."
            )
        if getattr(response, "status_code", 200) == 204:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise GmailAPIError(f"Gmail API returned invalid JSON during {operation}.") from exc
        return data if isinstance(data, dict) else {}

    def list_recent_messages(
        self,
        label: str | None = None,
        max_results: int = 1,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        enforce_tool_input_guardrails(
            "gmail_list_recent_messages",
            {"label": label, "max_results": max_results, "query": query},
        )
        if max_results < 1:
            raise ValueError("max_results must be at least 1.")
        if not self.live:
            return enforce_tool_output_guardrails("gmail_list_recent_messages", [])

        params: dict[str, Any] = {"maxResults": max_results}
        if label:
            params["labelIds"] = label
        if query:
            params["q"] = query
        data = self._request("GET", "messages", operation="list messages", params=params)
        output = [
            {"id": str(item.get("id") or ""), "threadId": str(item.get("threadId") or "")}
            for item in data.get("messages", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return enforce_tool_output_guardrails("gmail_list_recent_messages", output)

    def count_messages(
        self,
        *,
        label: str | None = None,
        query: str | None = None,
        page_size: int = 500,
        max_pages: int = 50,
    ) -> dict[str, Any]:
        """Count every Gmail message in a bounded read-only query.

        Gmail's ``resultSizeEstimate`` is not an exact count. This helper follows
        ``nextPageToken`` until the result set is exhausted and counts unique
        message IDs. If the defensive page ceiling is reached, ``complete`` is
        false so callers cannot present the partial count as exact.
        """

        enforce_tool_input_guardrails(
            "gmail_count_messages",
            {
                "label": label,
                "query": query,
                "page_size": page_size,
                "max_pages": max_pages,
            },
        )
        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500.")
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100.")
        empty = {
            "message_count": 0,
            "page_count": 0,
            "complete": True,
            "query": str(query or ""),
            "label": str(label or ""),
            "provider_read": self.live,
            "provider_write": False,
        }
        if not self.live:
            return enforce_tool_output_guardrails("gmail_count_messages", empty)

        message_ids: set[str] = set()
        page_token = ""
        seen_page_tokens: set[str] = set()
        page_count = 0
        complete = False
        while page_count < max_pages:
            params: dict[str, Any] = {"maxResults": page_size}
            if label:
                params["labelIds"] = label
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token
            data = self._request(
                "GET",
                "messages",
                operation="count messages",
                params=params,
            )
            page_count += 1
            for item in data.get("messages", []) or []:
                if not isinstance(item, Mapping):
                    continue
                message_id = str(item.get("id") or "").strip()
                if message_id:
                    message_ids.add(message_id)
            next_token = str(data.get("nextPageToken") or "").strip()
            if not next_token:
                complete = True
                break
            if next_token == page_token or next_token in seen_page_tokens:
                break
            seen_page_tokens.add(next_token)
            page_token = next_token

        output = {
            "message_count": len(message_ids),
            "page_count": page_count,
            "complete": complete,
            "query": str(query or ""),
            "label": str(label or ""),
            "provider_read": True,
            "provider_write": False,
        }
        return enforce_tool_output_guardrails("gmail_count_messages", output)

    def project_message_summaries(
        self,
        *,
        requested_fields: list[str],
        label: str | None = None,
        query: str | None = None,
        max_items: int = 50,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> dict[str, Any]:
        """Project selected metadata fields from one complete Gmail result set."""

        allowed = {"subject", "sender", "date", "snippet"}
        fields = list(
            dict.fromkeys(
                str(field or "").strip().lower()
                for field in requested_fields
                if str(field or "").strip().lower() in allowed
            )
        )
        enforce_tool_input_guardrails(
            "gmail_project_message_summaries",
            {
                "requested_fields": fields,
                "label": label,
                "query": query,
                "max_items": max_items,
                "page_size": page_size,
                "max_pages": max_pages,
            },
        )
        if not fields:
            raise ValueError("At least one Gmail projection field is required.")
        if not 1 <= max_items <= 50:
            raise ValueError("max_items must be between 1 and 50.")
        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500.")
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100.")
        empty = {
            "items": [],
            "item_count": 0,
            "page_count": 0,
            "complete": True,
            "requested_fields": fields,
            "query": str(query or ""),
            "label": str(label or ""),
            "provider_read": self.live,
            "provider_write": False,
        }
        if not self.live:
            return enforce_tool_output_guardrails(
                "gmail_project_message_summaries", empty
            )

        message_ids: list[str] = []
        seen_message_ids: set[str] = set()
        page_token = ""
        seen_page_tokens: set[str] = set()
        page_count = 0
        complete = False
        over_limit = False
        while page_count < max_pages:
            params: dict[str, Any] = {"maxResults": min(page_size, max_items + 1)}
            if label:
                params["labelIds"] = label
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token
            data = self._request(
                "GET",
                "messages",
                operation="project message summaries",
                params=params,
            )
            page_count += 1
            for item in data.get("messages", []) or []:
                if not isinstance(item, Mapping):
                    continue
                message_id = str(item.get("id") or "").strip()
                if message_id and message_id not in seen_message_ids:
                    seen_message_ids.add(message_id)
                    message_ids.append(message_id)
                if len(message_ids) > max_items:
                    over_limit = True
                    break
            if over_limit:
                break
            next_token = str(data.get("nextPageToken") or "").strip()
            if not next_token:
                complete = True
                break
            if next_token == page_token or next_token in seen_page_tokens:
                break
            seen_page_tokens.add(next_token)
            page_token = next_token

        if not complete or over_limit:
            output = {
                **empty,
                "page_count": page_count,
                "complete": False,
                "provider_read": True,
            }
            return enforce_tool_output_guardrails(
                "gmail_project_message_summaries", output
            )

        projected_items: list[dict[str, str]] = []
        for message_id in message_ids:
            data = self._request(
                "GET",
                f"messages/{message_id}",
                operation="get message metadata for projection",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date"],
                },
            )
            summary = gmail_message_summary_from_api(data)
            projected: dict[str, str] = {}
            if "subject" in fields:
                projected["subject"] = str(summary.get("subject") or "(no subject)")
            if "sender" in fields:
                projected["sender"] = str(
                    summary.get("sender_name") or summary.get("sender_email") or "Unknown sender"
                )
            if "date" in fields:
                projected["date"] = str(summary.get("received_at") or "Unknown date")
            if "snippet" in fields:
                projected["snippet"] = str(summary.get("snippet") or "")
            projected_items.append(projected)

        output = {
            **empty,
            "items": projected_items,
            "item_count": len(projected_items),
            "page_count": page_count,
            "provider_read": True,
        }
        return enforce_tool_output_guardrails(
            "gmail_project_message_summaries", output
        )

    def search_message_summaries(
        self,
        label: str | None = None,
        max_results: int = 10,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search Gmail and return compact metadata/snippet summaries."""

        enforce_tool_input_guardrails(
            "gmail_search_message_summaries",
            {"label": label, "max_results": max_results, "query": query},
        )
        if max_results < 1:
            raise ValueError("max_results must be at least 1.")
        if not self.live:
            return enforce_tool_output_guardrails("gmail_search_message_summaries", [])

        refs = self.list_recent_messages(label=label, max_results=max_results, query=query)
        summaries: list[dict[str, Any]] = []
        for ref in refs:
            message_id = str(ref.get("id") or "").strip()
            if not message_id:
                continue
            data = self._request(
                "GET",
                f"messages/{message_id}",
                operation="get message metadata",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date"],
                },
            )
            summaries.append(gmail_message_summary_from_api(data))
        return enforce_tool_output_guardrails("gmail_search_message_summaries", summaries)

    def batch_get_messages(
        self,
        message_ids: list[str],
        *,
        skip_blocked: bool = False,
    ) -> list[dict[str, Any]]:
        """Read multiple Gmail messages in full, optionally skipping guardrail-blocked items."""

        clean_ids = [
            str(message_id).strip() for message_id in message_ids if str(message_id).strip()
        ]
        enforce_tool_input_guardrails(
            "gmail_batch_get_messages",
            {"message_ids": clean_ids, "skip_blocked": skip_blocked},
        )
        if not self.live:
            return enforce_tool_output_guardrails("gmail_batch_get_messages", [])

        messages: list[dict[str, Any]] = []
        for message_id in clean_ids:
            try:
                messages.append(self.get_message(message_id))
            except ToolGuardrailViolation:
                if not skip_blocked:
                    raise
        return enforce_tool_output_guardrails("gmail_batch_get_messages", messages)

    def list_threads_by_label_filter(
        self,
        *,
        label_filter: str,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """List Gmail thread ids by label search without modifying Gmail."""

        enforce_tool_input_guardrails(
            "gmail_list_threads_by_label_filter",
            {"label_filter": label_filter, "max_results": max_results},
        )
        label = str(label_filter or "").strip()
        if max_results < 1:
            raise ValueError("max_results must be at least 1.")
        if not label:
            raise ValueError("label_filter is required.")
        if not self.live:
            return enforce_tool_output_guardrails("gmail_list_threads_by_label_filter", [])

        data = self._request(
            "GET",
            "threads",
            operation="list threads by label filter",
            params={"q": f'label:"{label}"', "maxResults": max_results},
        )
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for item in data.get("threads", []) or []:
            if not isinstance(item, Mapping):
                continue
            thread_id = str(item.get("id") or item.get("threadId") or "")
            if not thread_id or thread_id in seen:
                continue
            seen.add(thread_id)
            output.append({"id": thread_id, "threadId": thread_id})
        return enforce_tool_output_guardrails("gmail_list_threads_by_label_filter", output)

    def get_message(self, message_id: str) -> dict[str, Any]:
        enforce_tool_input_guardrails("gmail_get_message", {"message_id": message_id})
        if not self.live:
            return enforce_tool_output_guardrails(
                "gmail_get_message",
                {"status": "dry-run", "message_id": message_id, "message": None},
            )

        data = self._request(
            "GET",
            f"messages/{message_id}",
            operation="get message",
            params={"format": "full"},
        )
        envelope = gmail_message_envelope_from_api(data)
        payload = data.get("payload", {}) if isinstance(data.get("payload"), dict) else {}
        headers = payload.get("headers", []) if isinstance(payload.get("headers"), list) else []
        output = {
            "id": envelope.message_id,
            "threadId": envelope.thread_id,
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": envelope.subject,
            "messageIdHeader": _header(headers, "Message-ID"),
            "snippet": envelope.snippet,
            "body": envelope.normalized_body,
            "normalized_body": envelope.normalized_body,
            "body_evidence": [
                item.model_dump(mode="json") for item in envelope.body_evidence
            ],
            "body_content_status": envelope.body_content_status,
            "body_content_complete": envelope.body_content_complete,
            "received_at": envelope.received_at,
            "prior_labels": envelope.prior_labels,
            "labelIds": envelope.prior_labels,
            "extracted_links": [link.model_dump(mode="json") for link in envelope.extracted_links],
            "attachment_metadata": [
                attachment.model_dump(mode="json") for attachment in envelope.attachment_metadata
            ],
            "thread_summary": envelope.thread_summary,
            "thread_context": envelope.thread_context,
            "suspicious_signals": envelope.suspicious_signals,
            "triage_limitations": envelope.triage_limitations,
            "envelope": envelope.model_dump(mode="json"),
        }
        return enforce_tool_output_guardrails("gmail_get_message", output)

    def get_message_context_projection(
        self,
        message_id: str,
        *,
        body_part_path: str = "",
        body_start_char: int = 0,
        max_body_chars: int = 3_000,
        expected_thread_id: str = "",
        expected_account_identity_sha256: str = "",
        expected_source_snapshot_sha256: str = "",
    ) -> dict[str, Any]:
        """Read one message and immediately return a redacted bounded projection.

        Unlike ``get_message``, this returns at most 6000 characters of sanitized
        selected-message text and a source link, rather than a raw body/MIME field.
        The raw provider response stays inside this adapter boundary.
        """

        enforce_tool_input_guardrails(
            "gmail_get_message_context_projection",
            {
                "message_id": message_id,
                "body_part_path": body_part_path,
                "body_start_char": body_start_char,
                "max_body_chars": max_body_chars,
                "expected_thread_id": expected_thread_id,
                "expected_account_identity_sha256": expected_account_identity_sha256,
                "expected_source_snapshot_sha256": expected_source_snapshot_sha256,
            },
        )
        clean_part_path = str(body_part_path or "").strip()
        bounded_start = max(int(body_start_char or 0), 0)
        bounded_chars = min(max(int(max_body_chars or 1), 1), 3_000)
        if bounded_start and not clean_part_path:
            raise ValueError("body_part_path is required when body_start_char is nonzero.")
        if not self.live:
            return enforce_tool_output_guardrails(
                "gmail_get_message_context_projection",
                {
                    "status": "dry-run",
                    "id": message_id,
                    "threadId": "",
                    "triage_limitations": [
                        "Live Gmail message retrieval is disabled in dry-run mode."
                    ],
                },
            )

        data = self._request(
            "GET",
            f"messages/{message_id}",
            operation="get bounded message context",
            params={"format": "full"},
        )
        envelope = gmail_message_envelope_from_api(data)
        redaction_applied = False

        def safe_text(value: object) -> str:
            nonlocal redaction_applied
            original = str(value or "")
            redacted = redact_secret_like_text(original)
            redaction_applied = redaction_applied or redacted != original
            return redacted

        def safe_list(values: list[str]) -> list[str]:
            return [safe_text(value) for value in values]

        sender_name = safe_text(envelope.sender_name)
        sender_email = safe_text(envelope.sender_email)
        subject = safe_text(envelope.subject)
        snippet = safe_text(envelope.snippet)
        thread_summary = safe_text(envelope.thread_summary)
        context_text = safe_text(envelope.normalized_body or envelope.thread_context)
        # Newsletter padding must not consume the excerpt budget. Preserve isolated
        # joining characters in ordinary words and emoji rather than stripping all.
        context_text = re.sub(r"(?:[\u200b\u200c\u200d\ufeff]\s*){4,}", "\n", context_text)
        thread_context = context_text[:6000]
        suspicious_signals = safe_list(list(envelope.suspicious_signals))
        limitations = safe_list(list(envelope.triage_limitations))
        if len(context_text) > 6000:
            limitations.append("Selected message text was truncated to 6000 characters.")
        account_email = self.current_account_email()
        account_identity_sha256 = hashlib.sha256(
            account_email.strip().lower().encode("utf-8")
        ).hexdigest()
        history_id = str(data.get("historyId") or "")
        full_records = _full_sanitized_body_source_records(
            data.get("payload") if isinstance(data.get("payload"), dict) else {},
            envelope.body_evidence,
        )
        redaction_applied = redaction_applied or any(
            record.get("redaction_applied") is True for record in full_records
        )
        source_snapshot_sha256 = _gmail_source_snapshot_sha256(
            message_id=envelope.message_id,
            thread_id=envelope.thread_id,
            history_id=history_id,
            internal_date=str(data.get("internalDate") or ""),
            records=full_records,
        )
        source_url = (
            "https://mail.google.com/mail/?authuser=" + quote(account_email, safe="")
            + "#all/" + quote(envelope.thread_id or envelope.message_id, safe="")
        )
        extracted_links = []
        for link in envelope.extracted_links[:10]:
            safe_url = safe_text(link.url)
            if safe_url != link.url:
                continue
            extracted_links.append({
                "url": safe_url, "suspicious": link.suspicious,
                "reasons": safe_list(list(link.reasons[:10])),
            })
        source_changed = bool(
            (expected_thread_id and expected_thread_id != envelope.thread_id)
            or (
                expected_account_identity_sha256
                and expected_account_identity_sha256 != account_identity_sha256
            )
            or (
                expected_source_snapshot_sha256
                and expected_source_snapshot_sha256 != source_snapshot_sha256
            )
        )
        identity_output = {
            "id": envelope.message_id,
            "threadId": envelope.thread_id,
            "account_identity_sha256": account_identity_sha256,
            "provider_history_id": history_id,
            "source_snapshot_sha256": source_snapshot_sha256,
            "source_url": source_url,
        }
        if source_changed:
            return enforce_tool_output_guardrails(
                "gmail_get_message_context_projection",
                {
                    "status": "source_changed",
                    **identity_output,
                    "body_evidence": [],
                    "body_content_status": "partial",
                    "body_content_complete": False,
                    "source_restart_required": True,
                    "triage_limitations": [
                        "The selected Gmail account, thread, or sanitized source snapshot "
                        "changed after the prior window. Restart from the first window; "
                        "do not combine source versions."
                    ],
                    "provider_read": True,
                    "send_enabled": False,
                    "raw_message_bodies_returned": False,
                },
            )
        selected_records = (
            [
                record
                for record in full_records
                if record.get("part_path") == clean_part_path
            ]
            if clean_part_path
            else full_records
        )
        if clean_part_path and not selected_records:
            return enforce_tool_output_guardrails(
                "gmail_get_message_context_projection",
                {
                    "status": "source_inaccessible",
                    **identity_output,
                    "body_evidence": [],
                    "body_content_status": "partial",
                    "body_content_complete": False,
                    "source_restart_required": False,
                    "triage_limitations": [
                        "The requested sanitized MIME part is unavailable. Raw MIME, "
                        "attachment bodies, and image content were not substituted."
                    ],
                    "provider_read": True,
                    "send_enabled": False,
                    "raw_message_bodies_returned": False,
                },
            )
        if clean_part_path and selected_records:
            full_count = len(str(selected_records[0].get("source_text") or ""))
            if bounded_start >= full_count and full_count:
                return enforce_tool_output_guardrails(
                    "gmail_get_message_context_projection",
                    {
                        "status": "out_of_range",
                        **identity_output,
                        "body_evidence": [],
                        "body_content_status": "partial",
                        "body_content_complete": False,
                        "source_restart_required": False,
                        "triage_limitations": [
                            "body_start_char is outside the selected sanitized MIME part."
                        ],
                        "provider_read": True,
                        "send_enabled": False,
                        "raw_message_bodies_returned": False,
                    },
                )
        safe_evidence: list[dict[str, Any]] = []
        remaining_initial_chars = 6_000
        for record in selected_records:
            source_text = str(record.get("source_text") or "")
            start = bounded_start if clean_part_path else 0
            available_chars = (
                bounded_chars
                if clean_part_path
                else min(bounded_chars, remaining_initial_chars)
            )
            end = min(len(source_text), start + available_chars)
            has_more = end < len(source_text)
            coverage = {
                "start_char": start,
                "end_char": end,
                "full_char_count": len(source_text),
                "complete": not has_more,
                "has_more": has_more,
            }
            next_request = (
                {
                    "resource_type": "message",
                    "resource_id": envelope.message_id,
                    "body_part_path": str(record.get("part_path") or ""),
                    "body_start_char": end,
                    "max_body_chars": bounded_chars,
                    "expected_thread_id": envelope.thread_id,
                    "expected_account_identity_sha256": account_identity_sha256,
                    "expected_source_snapshot_sha256": source_snapshot_sha256,
                }
                if has_more and len(source_text) > 0
                else None
            )
            item_limitations = safe_list(list(record.get("limitations") or []))
            if has_more:
                item_limitations.append(
                    "This sanitized MIME representation has later text; use the exact "
                    "snapshot-pinned next_request to continue."
                )
            safe_evidence.append(
                {
                    "part_path": record.get("part_path"),
                    "mime_type": record.get("mime_type"),
                    "container_mime_type": record.get("container_mime_type"),
                    "alternative_group": record.get("alternative_group"),
                    "representation": record.get("representation"),
                    "role": record.get("role"),
                    "source_text": source_text[start:end],
                    "quotations": record.get("quotations", []),
                    "structure_annotations": record.get("structure_annotations") is True,
                    "content_complete": (
                        record.get("full_content_complete") is True and not has_more
                    ),
                    "truncated": (
                        record.get("full_content_complete") is not True or has_more
                    ),
                    "coverage": coverage,
                    "next_request": next_request,
                    "limitations": list(dict.fromkeys(item_limitations))[:10],
                }
            )
            if not clean_part_path:
                remaining_initial_chars = max(
                    0,
                    remaining_initial_chars - (end - start),
                )
        evidence_truncated = any(
            item.get("coverage", {}).get("has_more") is True
            for item in safe_evidence
        )
        if evidence_truncated:
            limitations.append(
                "Bounded source evidence was truncated to 6000 characters for model use."
            )
        projected_content_status = envelope.body_content_status
        if redaction_applied and projected_content_status == "complete":
            projected_content_status = "partial"
        output = {
            "status": "read",
            **identity_output,
            "received_at": envelope.received_at,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": subject,
            "snippet": snippet,
            "prior_labels": list(envelope.prior_labels),
            "attachment_metadata": [
                attachment.model_dump(mode="json")
                for attachment in envelope.attachment_metadata
            ],
            "thread_summary": thread_summary,
            "thread_context": thread_context,
            "body_evidence": safe_evidence,
            "body_content_status": (
                "partial" if evidence_truncated else projected_content_status
            ),
            "body_content_complete": (
                bool(safe_evidence)
                and all(item.get("content_complete") is True for item in safe_evidence)
                and not evidence_truncated
                and not redaction_applied
            ),
            "extracted_links": extracted_links,
            "suspicious_signals": suspicious_signals,
            "triage_limitations": [],
            "provider_read": True,
            "send_enabled": False,
            "raw_message_bodies_returned": False,
            "source_restart_required": False,
        }
        if redaction_applied:
            limitations.append(
                "One or more credential-shaped values were redacted from the bounded "
                "message projection before model use."
            )
        output["triage_limitations"] = list(dict.fromkeys(limitations))
        return enforce_tool_output_guardrails(
            "gmail_get_message_context_projection",
            output,
        )

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        """Read and normalize a complete Gmail thread without modifying Gmail.

        The returned message bodies are for immediate local sanitization only. Do not store
        or pass this raw normalized text to prompts.
        """

        enforce_tool_input_guardrails("gmail_get_thread", {"thread_id": thread_id})
        if not self.live:
            return {
                "status": "dry-run",
                "thread_id": thread_id,
                "message_count": 0,
                "subject": "",
                "summary": "Thread summary unavailable in dry-run mode.",
                "thread_context": "",
                "latest_received_at": "",
                "participants": [],
                "action_items": [],
                "deadlines": [],
                "open_questions": [],
                "triage_limitations": ["Live Gmail thread retrieval is disabled in dry-run mode."],
                "prior_context": [],
                "messages": [],
                "send_enabled": False,
                "draft_created": False,
                "labels_modified": False,
            }

        data = self._request(
            "GET",
            f"threads/{thread_id}",
            operation="get thread",
            params={"format": "full"},
        )
        raw_messages = [item for item in data.get("messages", []) if isinstance(item, Mapping)]
        message_pairs = [
            (message, gmail_message_envelope_from_api(message))
            for message in raw_messages
        ]
        chronology_available = bool(message_pairs) and all(
            envelope.received_at for _message, envelope in message_pairs
        )
        if chronology_available:
            try:
                message_pairs.sort(
                    key=lambda pair: datetime.fromisoformat(
                        pair[1].received_at.replace("Z", "+00:00")
                    )
                )
            except (TypeError, ValueError):
                chronology_available = False
        raw_messages = [message for message, _envelope in message_pairs]
        envelopes = [envelope for _message, envelope in message_pairs]
        message_count = len(envelopes)
        summary, participants, action_items, deadlines, open_questions = _thread_overview(
            envelopes,
            chronology_available=chronology_available,
        )
        prior_context = _thread_prior_context(raw_messages)
        newest_first = list(reversed(envelopes)) if chronology_available else envelopes
        thread_context = _thread_summary(
            " ".join(envelope.snippet for envelope in newest_first),
            " ".join(envelope.thread_summary for envelope in newest_first),
        )
        triage_limitations = _thread_level_limitations(envelopes)
        if not chronology_available and envelopes:
            triage_limitations.append(
                "One or more provider message dates were missing or invalid; message "
                "chronology, latest status, and latest_received_at are unavailable."
            )
        latest_received_at = (
            envelopes[-1].received_at if envelopes and chronology_available else ""
        )
        subject = (
            envelopes[-1].subject
            if envelopes and chronology_available and envelopes[-1].subject
            else next((item.subject for item in envelopes if item.subject), "")
        )
        thread_evidence_budget = GMAIL_BODY_EVIDENCE_MAX_CHARS
        evidence_by_message: dict[str, tuple[list[dict[str, Any]], bool]] = {}
        evidence_priority = (
            list(reversed(envelopes)) if chronology_available else envelopes
        )
        for envelope in evidence_priority:
            if thread_evidence_budget <= 0:
                evidence_by_message[envelope.message_id] = ([], bool(envelope.body_evidence))
                continue
            projection, truncated = _body_evidence_projection(
                envelope.body_evidence,
                max_chars=min(3000, thread_evidence_budget),
            )
            thread_evidence_budget -= sum(
                len(str(item.get("source_text") or "")) for item in projection
            )
            evidence_by_message[envelope.message_id] = (projection, truncated)
        messages = []
        for envelope in envelopes:
            body_evidence, body_evidence_truncated = evidence_by_message.get(
                envelope.message_id,
                ([], bool(envelope.body_evidence)),
            )
            message_limitations = list(
                dict.fromkeys([*envelope.triage_limitations, *triage_limitations])
            )
            if body_evidence_truncated:
                message_limitations.append(
                    "This message's source evidence was truncated by the bounded thread budget."
                )
            normalized = envelope.model_copy(
                update={
                    "thread_message_count": message_count,
                    "thread_context": thread_context,
                    "triage_limitations": message_limitations,
                }
            )
            messages.append(
                {
                    "id": normalized.message_id,
                    "threadId": normalized.thread_id,
                    "received_at": normalized.received_at,
                    "sender_name": normalized.sender_name,
                    "sender_email": normalized.sender_email,
                    "from": normalized.sender_email,
                    "to": normalized.to,
                    "subject": normalized.subject,
                    "snippet": normalized.snippet,
                    "prior_labels": normalized.prior_labels,
                    "labelIds": normalized.prior_labels,
                    "body": normalized.normalized_body,
                    "normalized_body": normalized.normalized_body,
                    "body_evidence": body_evidence,
                    "body_content_status": normalized.body_content_status,
                    "body_content_complete": (
                        normalized.body_content_complete and not body_evidence_truncated
                    ),
                    "extracted_links": [
                        link.model_dump(mode="json") for link in normalized.extracted_links
                    ],
                    "attachment_metadata": [
                        attachment.model_dump(mode="json")
                        for attachment in normalized.attachment_metadata
                    ],
                    "thread_summary": normalized.thread_summary,
                    "thread_context": normalized.thread_context,
                    "suspicious_signals": normalized.suspicious_signals,
                    "triage_limitations": normalized.triage_limitations,
                    "envelope": {
                        **normalized.model_dump(mode="json"),
                        "body_evidence": body_evidence,
                        "body_content_complete": (
                            normalized.body_content_complete and not body_evidence_truncated
                        ),
                    },
                }
            )
        return {
            "status": "read",
            "id": str(data.get("id") or thread_id),
            "thread_id": str(data.get("id") or thread_id),
            "message_count": message_count,
            "subject": subject,
            "summary": summary,
            "thread_context": thread_context,
            "latest_received_at": latest_received_at,
            "participants": participants,
            "action_items": action_items,
            "deadlines": deadlines,
            "open_questions": open_questions,
            "triage_limitations": triage_limitations,
            "prior_context": prior_context,
            "messages": messages,
            "send_enabled": False,
            "draft_created": False,
            "labels_modified": False,
        }

    def preview_label_changes(
        self,
        message_id: str,
        labels: list[str],
        *,
        existing_labels: list[str] | None = None,
        cleanup_obsolete: bool = False,
    ) -> dict[str, Any]:
        """Preview Keystone-managed Gmail label changes without modifying Gmail."""

        enforce_tool_input_guardrails(
            "gmail_preview_label_changes",
            {
                "message_id": message_id,
                "labels": labels,
                "existing_labels": existing_labels or [],
                "cleanup_obsolete": cleanup_obsolete,
            },
        )
        clean_labels = _clean_label_names(labels)
        existing = [label.strip() for label in existing_labels or [] if label.strip()]
        managed_existing = [label for label in existing if label in GMAIL_MANAGED_LABELS]
        add_labels = [label for label in clean_labels if label not in existing]
        remove_labels: list[str] = []
        if cleanup_obsolete:
            remove_labels = [
                label
                for label in managed_existing
                if label not in clean_labels
                or (label in GMAIL_PRIMARY_LABEL_SET and label not in clean_labels)
            ]
        output = {
            "status": "dry-run",
            "message_id": message_id,
            "labels": clean_labels,
            "existing_labels": existing,
            "add_labels": add_labels,
            "remove_labels": list(dict.fromkeys(remove_labels)),
            "cleanup_obsolete": cleanup_obsolete,
            "managed_labels": list(GMAIL_MANAGED_LABELS),
        }
        return enforce_tool_output_guardrails("gmail_preview_label_changes", output)

    def apply_labels(
        self,
        message_id: str,
        labels: list[str],
        *,
        existing_labels: list[str] | None = None,
        cleanup_obsolete: bool = False,
        dry_run_preview: bool | None = None,
    ) -> dict[str, Any]:
        enforce_tool_input_guardrails(
            "gmail_apply_labels",
            {
                "message_id": message_id,
                "labels": labels,
                "existing_labels": existing_labels or [],
                "cleanup_obsolete": cleanup_obsolete,
                "dry_run_preview": dry_run_preview,
                "live": self.live,
            },
        )
        preview = self.preview_label_changes(
            message_id,
            labels,
            existing_labels=existing_labels,
            cleanup_obsolete=cleanup_obsolete,
        )
        clean_labels = list(preview["labels"])
        if dry_run_preview is True:
            return preview
        if not self.live:
            return enforce_tool_output_guardrails(
                "gmail_apply_labels",
                {**preview, "status": "dry-run"},
            )

        add_label_ids = [self._label_id_for(label) for label in preview["add_labels"]]
        remove_label_ids = [self._label_id_for(label) for label in preview["remove_labels"]]
        data = self._request(
            "POST",
            f"messages/{message_id}/modify",
            operation="apply labels",
            json={"addLabelIds": add_label_ids, "removeLabelIds": remove_label_ids},
        )
        output = {
            "status": "labels_applied",
            "message_id": message_id,
            "labels": clean_labels,
            "add_labels": preview["add_labels"],
            "remove_labels": preview["remove_labels"],
            "add_label_ids": add_label_ids,
            "remove_label_ids": remove_label_ids,
            "cleanup_obsolete": cleanup_obsolete,
            "gmail_response": data,
        }
        return enforce_tool_output_guardrails("gmail_apply_labels", output)

    def modify_message_state(
        self,
        message_id: str,
        operation: GmailMailboxStateOperation,
        *,
        label: str = "",
        expected_account: str = "",
        approval_reference: str = "",
    ) -> dict[str, Any]:
        """Apply one exact Gmail mailbox-state change and verify provider state."""

        clean_id = str(message_id or "").strip()
        clean_label = str(label or "").strip()
        expected = str(expected_account or "").strip()
        approval = str(approval_reference or "").strip()
        enforce_tool_input_guardrails(
            "gmail_modify_message_state",
            {
                "message_id": clean_id,
                "operation": operation,
                "label": clean_label,
                "expected_account": expected,
                "approval_reference": approval,
                "live": self.live,
            },
        )
        if not clean_id:
            raise ValueError("An exact Gmail message_id is required for mailbox modification.")
        if operation not in GMAIL_MAILBOX_STATE_OPERATIONS:
            raise ValueError(f"Unsupported Gmail mailbox-state operation: {operation}.")
        if operation in {"add_label", "remove_label"} and not clean_label:
            raise ValueError(f"A label is required for Gmail operation '{operation}'.")
        if operation not in {"add_label", "remove_label"} and clean_label:
            raise ValueError(f"Gmail operation '{operation}' does not accept a label.")

        if not self.live:
            return enforce_tool_output_guardrails(
                "gmail_modify_message_state",
                {
                    "status": "dry-run",
                    "message_id": clean_id,
                    "operation": operation,
                    "label": clean_label,
                    "approval_reference_present": bool(approval),
                    "provider_write": False,
                    "verification": {"passed": False, "reason": "live mode disabled"},
                    "send_enabled": False,
                    "sent": False,
                },
            )

        if os.getenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "").strip().lower() != "true":
            raise GmailConfigurationError(
                "Live Gmail mailbox-state writes require "
                "KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES=true."
            )
        if not approval:
            raise ValueError("A non-empty approval_reference is required for Gmail modification.")
        if expected:
            current_account = self.current_account_email()
            if current_account.lower() != expected.lower():
                raise GmailConfigurationError(
                    "Live Gmail modification is configured for "
                    f"{expected}, but OAuth is authenticated as {current_account}."
                )

        before = self.get_message(clean_id)
        before_labels = list(before.get("labelIds") or [])
        target_label = ""
        add_ids: list[str] = []
        remove_ids: list[str] = []
        add_by_operation = {
            "unarchive": "INBOX",
            "mark_unread": "UNREAD",
            "star": "STARRED",
            "mark_important": "IMPORTANT",
        }
        remove_by_operation = {
            "archive": "INBOX",
            "mark_read": "UNREAD",
            "unstar": "STARRED",
            "mark_not_important": "IMPORTANT",
        }
        if operation == "add_label":
            target_label = self._label_id_for(clean_label)
            add_ids = [target_label]
        elif operation == "remove_label":
            target_label = self._existing_label_id_for(clean_label)
            remove_ids = [target_label]
        elif operation in add_by_operation:
            target_label = add_by_operation[operation]
            add_ids = [target_label]
        elif operation in remove_by_operation:
            target_label = remove_by_operation[operation]
            remove_ids = [target_label]

        if operation == "trash":
            self._request(
                "POST", f"messages/{clean_id}/trash", operation="trash exact message", json={}
            )
            target_label = "TRASH"
        elif operation == "restore":
            self._request(
                "POST", f"messages/{clean_id}/untrash", operation="restore exact message", json={}
            )
            target_label = "TRASH"
        else:
            self._request(
                "POST",
                f"messages/{clean_id}/modify",
                operation=f"modify exact message state: {operation}",
                json={"addLabelIds": add_ids, "removeLabelIds": remove_ids},
            )

        after = self.get_message(clean_id)
        after_labels = list(after.get("labelIds") or [])
        expected_present = operation in {
            "add_label",
            "unarchive",
            "mark_unread",
            "star",
            "mark_important",
            "trash",
        }
        passed = (target_label in after_labels) is expected_present
        output = {
            "status": "message_state_modified" if passed else "verification_failed",
            "message_id": clean_id,
            "thread_id": str(after.get("threadId") or before.get("threadId") or ""),
            "gmail_account": expected,
            "operation": operation,
            "label": clean_label,
            "before_label_ids": before_labels,
            "after_label_ids": after_labels,
            "approval_reference_present": True,
            "provider_write": True,
            "verification": {
                "passed": passed,
                "target_label_id": target_label,
                "expected_present": expected_present,
            },
            "send_enabled": False,
            "sent": False,
        }
        if not passed:
            raise GmailAPIError(
                f"Gmail provider read-back did not verify operation '{operation}' "
                f"for message '{clean_id}'."
            )
        return enforce_tool_output_guardrails("gmail_modify_message_state", output)

    def _refresh_label_cache(self) -> None:
        data = self._request("GET", "labels", operation="list labels")
        self._label_name_to_id.clear()
        for label in data.get("labels", []):
            if not isinstance(label, dict):
                continue
            name = str(label.get("name") or "")
            label_id = str(label.get("id") or "")
            if name and label_id:
                self._label_name_to_id[name] = label_id

    def _existing_label_id_for(self, label: str) -> str:
        cleaned = label.strip()
        if not cleaned:
            raise ValueError("Gmail label cannot be empty.")
        if cleaned in GMAIL_SYSTEM_LABEL_IDS or cleaned.startswith("Label_"):
            return cleaned
        if not self._label_name_to_id:
            self._refresh_label_cache()
        label_id = self._label_name_to_id.get(cleaned)
        if not label_id:
            raise GmailAPIError(f"Gmail label '{cleaned}' does not exist.")
        return label_id

    def _label_id_for(self, label: str) -> str:
        cleaned = label.strip()
        if not cleaned:
            raise ValueError("Gmail label cannot be empty.")
        if cleaned in GMAIL_SYSTEM_LABEL_IDS or cleaned.startswith("Label_"):
            return cleaned
        if not self._label_name_to_id:
            self._refresh_label_cache()
        if cleaned in self._label_name_to_id:
            return self._label_name_to_id[cleaned]

        created = self._request(
            "POST",
            "labels",
            operation="create label",
            json={
                "name": cleaned,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        label_id = str(created.get("id") or "")
        if not label_id:
            raise GmailAPIError(f"Gmail API returned no id while creating label '{cleaned}'.")
        self._label_name_to_id[cleaned] = label_id
        return label_id

    @durable_provider_tool("create_gmail_draft_reply")
    def create_draft_reply(
        self, message_id: str, body: str, **legacy_fields: str
    ) -> dict[str, Any]:
        enforce_tool_input_guardrails(
            "gmail_create_draft_reply",
            {"message_id": message_id, "body": body, **legacy_fields},
        )
        if not self.live:
            return enforce_tool_output_guardrails(
                "gmail_create_draft_reply",
                {
                    "status": "dry-run",
                    "message_id": message_id,
                    "to": legacy_fields.get("to", ""),
                    "subject": legacy_fields.get("subject", ""),
                    "body_preview": body[:120],
                    "sent": False,
                    "approval_required": True,
                },
            )

        original = self.get_message(message_id)
        to_header = str(original.get("from") or "")
        to_address = parseaddr(to_header)[1] or to_header
        subject = str(original.get("subject") or "").strip()
        reply_subject = (
            subject if subject.lower().startswith("re:") else f"Re: {subject or ''}".strip()
        )
        message_id_header = str(original.get("messageIdHeader") or "").strip()

        message = EmailMessage()
        message["To"] = to_address
        message["Subject"] = reply_subject or "Re:"
        if message_id_header:
            message["In-Reply-To"] = message_id_header
            message["References"] = message_id_header
        message.set_content(body.strip())

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        data = self._request(
            "POST",
            "drafts",
            operation="create draft reply",
            json={
                "message": {
                    "raw": encoded_message,
                    "threadId": str(original.get("threadId") or ""),
                }
            },
        )
        _record_gmail_draft_observation(str(data.get("id") or ""), "create_draft_reply")
        output = {
            "status": "draft_created",
            "message_id": message_id,
            "draft_id": str(data.get("id") or ""),
            "sent": False,
            "approval_required": True,
        }
        return enforce_tool_output_guardrails("gmail_create_draft_reply", output)

    def current_account_email(self) -> str:
        """Return the authenticated Gmail account for live account scoping."""

        if not self.live:
            return ""
        data = self._request("GET", "profile", operation="get Gmail profile")
        email_address = str(data.get("emailAddress") or "").strip()
        if not email_address:
            raise GmailAPIError("Gmail API profile response did not include emailAddress.")
        return email_address

    @durable_provider_tool("create_gmail_draft")
    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        enforce_tool_input_guardrails(
            "gmail_create_draft",
            {
                "to": to,
                "subject": subject,
                "body": body,
                "expected_account": expected_account or "",
            },
        )
        to_address = to.strip()
        if not to_address:
            raise ValueError("Gmail draft recipient cannot be empty.")
        subject_text = subject.strip()
        body_text = body.strip()
        expected = (expected_account or "").strip()
        if not self.live:
            return enforce_tool_output_guardrails(
                "gmail_create_draft",
                {
                    "status": "dry-run",
                    "message_id": "dry-run-message",
                    "draft_id": "",
                    "to": to_address,
                    "subject": subject_text,
                    "body_preview": body_text[:120],
                    "gmail_account": expected,
                    "sent": False,
                    "approval_required": True,
                },
            )
        current_account = ""
        if expected:
            current_account = self.current_account_email()
            if current_account.lower() != expected.lower():
                raise GmailConfigurationError(
                    "Live Gmail draft creation is configured for "
                    f"{expected}, but OAuth is authenticated as {current_account}."
                )

        message = EmailMessage()
        message["To"] = to_address
        message["Subject"] = subject_text
        message.set_content(body_text)

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        data = self._request(
            "POST",
            "drafts",
            operation="create draft",
            json={"message": {"raw": encoded_message}},
        )
        _record_gmail_draft_observation(str(data.get("id") or ""), "create_draft")
        created_message = data.get("message", {})
        if not isinstance(created_message, Mapping):
            created_message = {}
        output = {
            "status": "draft_created",
            "message_id": str(created_message.get("id") or ""),
            "draft_id": str(data.get("id") or ""),
            "to": to_address,
            "subject": subject_text,
            "body_preview": body_text[:120],
            "gmail_account": current_account or expected,
            "sent": False,
            "approval_required": True,
        }
        return enforce_tool_output_guardrails("gmail_create_draft", output)

    @durable_provider_tool("create_gmail_draft_with_attachment")
    def create_draft_with_attachment(
        self,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        """Create one no-send Gmail draft with one bounded local attachment."""

        return self._write_draft_with_attachment(
            "",
            to,
            subject,
            body,
            attachment_path,
            expected_account=expected_account,
        )

    @durable_provider_tool("update_gmail_draft_with_attachment")
    def update_draft_with_attachment(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        """Replace one no-send Gmail draft while retaining one exact attachment."""

        return self._write_draft_with_attachment(
            draft_id,
            to,
            subject,
            body,
            attachment_path,
            expected_account=expected_account,
        )

    def _write_draft_with_attachment(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None,
    ) -> dict[str, Any]:
        clean_draft_id = draft_id.strip()
        to_address = to.strip()
        if not to_address:
            raise ValueError("Gmail draft recipient cannot be empty.")
        expected = (expected_account or "").strip()
        if not self.live:
            return {
                "status": "dry-run",
                "draft_id": clean_draft_id,
                "message_id": "",
                "to": to_address,
                "subject": subject.strip(),
                "body_preview": body.strip()[:120],
                "attachment_path": str(attachment_path or "").strip(),
                "gmail_account": expected,
                "sent": False,
                "approval_required": True,
            }
        current_account = ""
        if expected:
            current_account = self.current_account_email()
            if current_account.casefold() != expected.casefold():
                raise GmailConfigurationError(
                    "Live Gmail attachment draft is configured for a different account."
                )
        target, content, mime_type = _gmail_draft_attachment_file(attachment_path)
        maintype, subtype = mime_type.split("/", maxsplit=1)
        message = EmailMessage()
        message["To"] = to_address
        message["Subject"] = subject.strip()
        message.set_content(body.strip())
        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=target.name,
        )
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        method = "PUT" if clean_draft_id else "POST"
        endpoint = f"drafts/{clean_draft_id}" if clean_draft_id else "drafts"
        request_payload: dict[str, Any] = {"message": {"raw": encoded_message}}
        if clean_draft_id:
            request_payload["id"] = clean_draft_id
        data = self._request(
            method,
            endpoint,
            operation=(
                "update Gmail draft with attachment"
                if clean_draft_id
                else "create Gmail draft with attachment"
            ),
            json=request_payload,
        )
        _record_gmail_draft_observation(
            str(data.get("id") or clean_draft_id),
            "update_draft_attachment" if clean_draft_id else "create_draft_attachment",
        )
        created_message = data.get("message", {})
        if not isinstance(created_message, Mapping):
            created_message = {}
        return {
            "status": "draft_updated" if clean_draft_id else "draft_created",
            "draft_id": str(data.get("id") or clean_draft_id),
            "message_id": str(created_message.get("id") or ""),
            "to": to_address,
            "subject": subject.strip(),
            "body_preview": body.strip()[:120],
            "attachment_filename": target.name,
            "attachment_size": len(content),
            "attachment_sha256": hashlib.sha256(content).hexdigest(),
            "gmail_account": current_account or expected,
            "sent": False,
            "approval_required": True,
        }

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        """Read one Gmail draft for bounded verification without sending it."""

        clean_draft_id = draft_id.strip()
        if not clean_draft_id:
            raise ValueError("Gmail draft id cannot be empty.")
        if not self.live:
            return {
                "status": "dry-run",
                "draft_id": clean_draft_id,
                "message_id": "",
                "to": "",
                "subject": "",
                "body": "",
                "attachments": [],
                "attachment_count": 0,
                "sent": False,
            }
        data = self._request(
            "GET",
            f"drafts/{clean_draft_id}",
            operation="get Gmail draft",
            params={"format": "full"},
        )
        message = data.get("message", {})
        if not isinstance(message, Mapping):
            message = {}
        envelope = gmail_message_envelope_from_api(message)
        attachments = _draft_attachment_refs(message)
        return {
            "status": "success",
            "draft_id": str(data.get("id") or clean_draft_id),
            "message_id": envelope.message_id,
            "to": envelope.to,
            "subject": envelope.subject,
            "body": envelope.normalized_body,
            "attachments": attachments,
            "attachment_count": len(attachments),
            "sent": False,
        }

    def get_draft_attachment(self, draft_id: str, filename: str) -> dict[str, Any]:
        """Read and hash one exact draft attachment without returning its bytes."""

        clean_draft_id = draft_id.strip()
        clean_filename = filename.strip()
        if not clean_draft_id or not clean_filename:
            raise ValueError("Gmail draft attachment reads require draft_id and filename.")
        if not self.live:
            return {
                "status": "dry-run",
                "draft_id": clean_draft_id,
                "filename": clean_filename,
                "size": 0,
                "sha256": "",
            }
        data = self._request(
            "GET",
            f"drafts/{clean_draft_id}",
            operation="get Gmail draft attachment metadata",
            params={"format": "full"},
        )
        message = data.get("message", {})
        if not isinstance(message, Mapping):
            raise GmailAPIError("Gmail draft response did not contain a message.")
        matches = [
            ref for ref in _draft_attachment_refs(message) if ref["filename"] == clean_filename
        ]
        if len(matches) != 1:
            raise GmailAPIError("Gmail draft attachment target was missing or ambiguous.")
        message_id = str(message.get("id") or "").strip()
        attachment_id = str(matches[0]["attachment_id"])
        attachment = self._request(
            "GET",
            f"messages/{message_id}/attachments/{attachment_id}",
            operation="get Gmail draft attachment bytes",
        )
        encoded = str(attachment.get("data") or "")
        padded = encoded + "=" * (-len(encoded) % 4)
        content = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return {
            "status": "success",
            "draft_id": clean_draft_id,
            "message_id": message_id,
            "filename": clean_filename,
            "mime_type": str(matches[0]["mime_type"]),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def list_recent_drafts(self, *, max_results: int = 20) -> list[dict[str, Any]]:
        """Return bounded draft metadata for deterministic natural-reference resolution."""

        bounded_max = max(1, min(50, int(max_results)))
        if not self.live:
            return []
        data = self._request(
            "GET",
            "drafts",
            operation="list Gmail drafts",
            params={"maxResults": bounded_max},
        )
        refs = data.get("drafts", [])
        if not isinstance(refs, list):
            return []
        drafts: list[dict[str, Any]] = []
        for ref in refs[:bounded_max]:
            if not isinstance(ref, Mapping):
                continue
            draft_id = str(ref.get("id") or "").strip()
            if not draft_id:
                continue
            drafts.append(self.get_draft(draft_id))
        return drafts

    @durable_provider_tool("update_gmail_draft")
    def update_draft(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        """Replace one existing Gmail draft while preserving the no-send boundary."""

        clean_draft_id = draft_id.strip()
        to_address = to.strip()
        if not clean_draft_id:
            raise ValueError("Gmail draft id cannot be empty.")
        if not to_address:
            raise ValueError("Gmail draft recipient cannot be empty.")
        expected = (expected_account or "").strip()
        if not self.live:
            return {
                "status": "dry-run",
                "draft_id": clean_draft_id,
                "message_id": "",
                "to": to_address,
                "subject": subject.strip(),
                "body_preview": body.strip()[:120],
                "gmail_account": expected,
                "sent": False,
                "approval_required": True,
            }
        current_account = ""
        if expected:
            current_account = self.current_account_email()
            if current_account.lower() != expected.lower():
                raise GmailConfigurationError(
                    "Live Gmail draft update is configured for "
                    f"{expected}, but OAuth is authenticated as {current_account}."
                )

        message = EmailMessage()
        message["To"] = to_address
        message["Subject"] = subject.strip()
        message.set_content(body.strip())
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        data = self._request(
            "PUT",
            f"drafts/{clean_draft_id}",
            operation="update Gmail draft",
            json={"id": clean_draft_id, "message": {"raw": encoded_message}},
        )
        _record_gmail_draft_observation(str(data.get("id") or clean_draft_id), "update_draft")
        created_message = data.get("message", {})
        if not isinstance(created_message, Mapping):
            created_message = {}
        return {
            "status": "draft_updated",
            "draft_id": str(data.get("id") or clean_draft_id),
            "message_id": str(created_message.get("id") or ""),
            "to": to_address,
            "subject": subject.strip(),
            "body_preview": body.strip()[:120],
            "gmail_account": current_account or expected,
            "sent": False,
            "approval_required": True,
        }

    @durable_provider_tool("delete_gmail_test_draft")
    def delete_draft(
        self,
        draft_id: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        """Delete one exact Gmail draft; callers own test-marker and approval gates."""

        clean_draft_id = draft_id.strip()
        if not clean_draft_id:
            raise ValueError("Gmail draft id cannot be empty.")
        expected = (expected_account or "").strip()
        if not self.live:
            return {
                "status": "dry-run",
                "draft_id": clean_draft_id,
                "gmail_account": expected,
                "sent": False,
            }
        current_account = ""
        if expected:
            current_account = self.current_account_email()
            if current_account.lower() != expected.lower():
                raise GmailConfigurationError(
                    "Live Gmail draft deletion is configured for "
                    f"{expected}, but OAuth is authenticated as {current_account}."
                )
        self._request(
            "DELETE",
            f"drafts/{clean_draft_id}",
            operation="delete Gmail test draft",
        )
        _record_gmail_draft_observation(clean_draft_id, "delete_test_draft")
        return {
            "status": "draft_deleted",
            "draft_id": clean_draft_id,
            "gmail_account": current_account or expected,
            "sent": False,
        }

    def draft_exists(self, draft_id: str) -> bool:
        """Return whether one exact Gmail draft still exists."""

        clean_draft_id = draft_id.strip()
        if not clean_draft_id:
            raise ValueError("Gmail draft id cannot be empty.")
        if not self.live:
            return False
        data = self._request(
            "GET",
            f"drafts/{clean_draft_id}",
            operation="verify Gmail draft absence",
            params={"format": "minimal"},
            allow_not_found=True,
        )
        return not bool(data.get("_not_found"))

    def send_draft(
        self,
        draft_id: str,
        *,
        expected_account: str,
    ) -> dict[str, Any]:
        """Send one exact existing draft; higher-level code owns all test-send gates."""

        clean_draft_id = draft_id.strip()
        expected = expected_account.strip()
        if not clean_draft_id:
            raise ValueError("Gmail draft send requires an exact draft_id.")
        if not expected:
            raise ValueError("Gmail draft send requires expected_account.")
        if not self.live:
            return {
                "status": "dry-run",
                "draft_id": clean_draft_id,
                "message_id": "",
                "thread_id": "",
                "gmail_account": expected,
                "sent": False,
            }
        current_account = self.current_account_email()
        if current_account.lower() != expected.lower():
            raise GmailConfigurationError(
                "Live Gmail draft send is configured for "
                f"{expected}, but OAuth is authenticated as {current_account}."
            )
        data = self._request(
            "POST",
            "drafts/send",
            operation="send approved Gmail test draft",
            json={"id": clean_draft_id},
        )
        return {
            "status": "sent",
            "draft_id": clean_draft_id,
            "message_id": str(data.get("id") or ""),
            "thread_id": str(data.get("threadId") or ""),
            "gmail_account": current_account,
            "sent": True,
        }

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        """Free-form external email sending remains intentionally unavailable."""

        raise NotImplementedError("Free-form external email sending is not implemented.")


def _gmail_read_tool() -> GmailTool:
    """Reuse one authenticated read client within a bounded agent request."""

    read_context = current_provider_read_context()
    if read_context is None or read_context.plan.provider != "gmail":
        return GmailTool(live=True)
    if not read_context.try_start_attempt():
        raise ProviderReadContextError(
            "Gmail read request exceeded its bounded call or deadline budget."
        )
    return read_context.service(
        "gmail.live_read_tool",
        lambda: GmailTool(live=True),
    )


def list_recent_messages(
    label: str | None = None,
    max_results: int = 1,
    query: str | None = None,
) -> list[dict[str, Any]]:
    result = _gmail_read_tool().list_recent_messages(
        label=label,
        max_results=max_results,
        query=query,
    )
    record_provider_read_result("gmail_list_recent_messages", result)
    return result


def count_messages(
    *,
    label: str | None = None,
    query: str | None = None,
    page_size: int = 500,
    max_pages: int = 50,
) -> dict[str, Any]:
    result = _gmail_read_tool().count_messages(
        label=label,
        query=query,
        page_size=page_size,
        max_pages=max_pages,
    )
    record_provider_read_result("gmail_count_messages", result)
    return result


def project_message_summaries(
    *,
    requested_fields: list[str],
    label: str | None = None,
    query: str | None = None,
    max_items: int = 50,
    page_size: int = 100,
    max_pages: int = 50,
) -> dict[str, Any]:
    result = _gmail_read_tool().project_message_summaries(
        requested_fields=requested_fields,
        label=label,
        query=query,
        max_items=max_items,
        page_size=page_size,
        max_pages=max_pages,
    )
    record_provider_read_result("gmail_project_message_summaries", result)
    return result


def search_message_summaries(
    label: str | None = None,
    max_results: int = 10,
    query: str | None = None,
) -> list[dict[str, Any]]:
    result = _gmail_read_tool().search_message_summaries(
        label=label,
        max_results=max_results,
        query=query,
    )
    record_provider_read_result("gmail_search_message_summaries", result)
    return result


def batch_get_messages(
    message_ids: list[str],
    *,
    skip_blocked: bool = False,
) -> list[dict[str, Any]]:
    result = _gmail_read_tool().batch_get_messages(
        message_ids=message_ids,
        skip_blocked=skip_blocked,
    )
    record_provider_read_result("gmail_batch_get_messages", result)
    return result


def list_threads_by_label_filter(label_filter: str, max_results: int = 5) -> list[dict[str, Any]]:
    result = _gmail_read_tool().list_threads_by_label_filter(
        label_filter=label_filter,
        max_results=max_results,
    )
    record_provider_read_result("gmail_list_threads_by_label_filter", result)
    return result


def get_message(message_id: str) -> dict[str, Any]:
    result = _gmail_read_tool().get_message(message_id=message_id)
    record_provider_read_result("gmail_get_message", result)
    return result


def get_message_context_projection(
    message_id: str,
    *,
    body_part_path: str = "",
    body_start_char: int = 0,
    max_body_chars: int = 3_000,
    expected_thread_id: str = "",
    expected_account_identity_sha256: str = "",
    expected_source_snapshot_sha256: str = "",
) -> dict[str, Any]:
    result = _gmail_read_tool().get_message_context_projection(
        message_id=message_id,
        body_part_path=body_part_path,
        body_start_char=body_start_char,
        max_body_chars=max_body_chars,
        expected_thread_id=expected_thread_id,
        expected_account_identity_sha256=expected_account_identity_sha256,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
    )
    record_provider_read_result("gmail_get_message_context_projection", result)
    return result


def get_thread(thread_id: str) -> dict[str, Any]:
    result = _gmail_read_tool().get_thread(thread_id=thread_id)
    record_provider_read_result("gmail_get_thread", result)
    return result


def get_thread_with_source_url(thread_id: str) -> dict[str, Any]:
    """Read a thread and attach its authenticated mailbox link before projection."""
    tool = _gmail_read_tool()
    result = tool.get_thread(thread_id=thread_id)
    returned_id = str(result.get("thread_id") or result.get("id") or "")
    if result.get("status") == "read" and returned_id == thread_id:
        result["source_url"] = (
            "https://mail.google.com/mail/?authuser="
            + quote(tool.current_account_email(), safe="")
            + "#all/" + quote(returned_id, safe="")
        )
    record_provider_read_result("gmail_get_thread_with_source_url", result)
    return result


def apply_labels(
    message_id: str,
    labels: list[str],
    *,
    existing_labels: list[str] | None = None,
    cleanup_obsolete: bool = False,
    dry_run_preview: bool | None = None,
) -> dict[str, Any]:
    return GmailTool(live=True).apply_labels(
        message_id=message_id,
        labels=labels,
        existing_labels=existing_labels,
        cleanup_obsolete=cleanup_obsolete,
        dry_run_preview=dry_run_preview,
    )


def create_draft_reply(message_id: str, body: str) -> dict[str, Any]:
    return GmailTool(live=True).create_draft_reply(message_id=message_id, body=body)


def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
    """Sending external email is intentionally unavailable."""

    raise NotImplementedError("External email sending is not implemented.")


@function_tool(**keystone_tool_guardrail_kwargs())
def get_gmail_message(message_id: str) -> str:
    """Return one Gmail message in fixture mode. Live Gmail access is only CLI-gated."""

    return _json(GmailTool(live=False).get_message(message_id=message_id))


@function_tool(**keystone_tool_guardrail_kwargs())
def apply_gmail_labels(message_id: str, labels: list[str]) -> str:
    """Record intended Gmail labels in fixture mode. This tool does not send email."""

    return _json(GmailTool(live=False).apply_labels(message_id=message_id, labels=labels))


@function_tool(**keystone_tool_guardrail_kwargs())
def modify_gmail_message_state(
    message_id: str,
    operation: GmailMailboxStateOperation,
    expected_account: str,
    approval_reference: str,
    label: str = "",
    live: bool = False,
) -> str:
    """Modify one exact Gmail message and verify the requested provider state.

    Supported operations are label add/remove, archive/unarchive, read/unread,
    star/unstar, important/not-important, trash, and restore. This tool never
    sends email and requires a dedicated live gate plus scoped approval.
    """

    return _json(
        GmailTool(live=live).modify_message_state(
            message_id,
            operation,
            label=label,
            expected_account=expected_account,
            approval_reference=approval_reference,
        )
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def create_gmail_draft_reply(message_id: str, body: str) -> str:
    """Create a dry-run Gmail reply draft. This tool never sends email."""

    return _json(
        GmailTool(live=False).create_draft_reply(
            message_id=message_id,
            body=body,
        )
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def create_gmail_draft_with_attachment(
    to: str,
    subject: str,
    body: str,
    attachment_path: str,
    expected_account: str,
    approval_reference: str,
    draft_id: str = "",
    live: bool = False,
) -> str:
    """Create or update one approved no-send Gmail draft with one derived artifact."""

    from keystone_agents.gmail_triage.draft_actions import (
        execute_approved_gmail_draft_attachment_action,
    )

    return _json(
        execute_approved_gmail_draft_attachment_action(
            GmailTool(live=live),
            to=to,
            subject=subject,
            body=body,
            attachment_path=attachment_path,
            expected_account=expected_account,
            approval_reference=approval_reference,
            draft_id=draft_id,
        )
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def gmail_test_draft_lifecycle(
    marker: str,
    expected_account: str,
    recipient: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Create, verify, update, and remove one exact KBA_TEST_DRAFT provider draft.

    This is a no-send validation tool. Python requires the exact marker, account,
    recipient, scoped approval, dedicated test-delete gate, same-draft read-back,
    and verified provider absence after cleanup.
    """

    from keystone_agents.gmail_triage.draft_actions import (
        execute_gmail_test_draft_lifecycle,
    )

    return _json(
        execute_gmail_test_draft_lifecycle(
            GmailTool(live=live),
            marker=marker,
            expected_account=expected_account,
            recipient=recipient,
            approval_reference=approval_reference,
        )
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def send_gmail_test_draft(
    draft_id: str,
    expected_account: str,
    approval_reference: str,
    send_number: int = 1,
    live: bool = False,
) -> str:
    """Send one exact marked validation draft through the dedicated test-only gates.

    This is not a general email send tool. The existing draft must already match
    the configured recipient and contain ``KBA_TEST_EMAIL`` in both subject and
    body. Python independently enforces sender account, recipient, approval,
    maximum send count, provider read-back, and SENT verification.
    """

    from keystone_agents.gmail_triage.draft_actions import (
        send_approved_gmail_test_draft,
    )

    return _json(
        send_approved_gmail_test_draft(
            GmailTool(live=live),
            draft_id=draft_id,
            expected_account=expected_account,
            approval_reference=approval_reference,
            send_number=send_number,
        )
    )
