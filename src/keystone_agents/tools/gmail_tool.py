"""Gmail API integration boundary.

Live Gmail support is limited to reading messages, applying labels, and creating drafts.
There is intentionally no send implementation.
"""

from __future__ import annotations

import base64
import html
import json
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
from urllib.parse import urlparse

import requests

from keystone_agents.guardrails import (
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.schemas.email_triage import (
    GMAIL_MANAGED_LABELS,
    GMAIL_PRIMARY_LABEL_SET,
    GmailAttachmentMetadata,
    GmailLinkRecord,
    GmailMessageEnvelope,
    normalize_managed_gmail_labels,
)
from keystone_agents.sdk import ToolGuardrailViolation, function_tool

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_GOOGLE_CREDENTIALS_FILE = "credentials.json"
DEFAULT_GOOGLE_TOKEN_FILE = "token.json"
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)
GMAIL_LIVE_OPERATIONS = ("read", "label", "create_draft")
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


def _decode_base64_url(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
    return decoded.decode("utf-8", errors="replace")


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


def _walk_payload_parts(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts", []) or []:
        if isinstance(child, dict):
            parts.extend(_walk_payload_parts(child))
    return parts


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


def _payload_text_links_and_attachments(
    payload: dict[str, Any] | None,
) -> tuple[str, list[GmailLinkRecord], list[GmailAttachmentMetadata], bool]:
    if not payload:
        return "", [], [], False

    plain_parts: list[str] = []
    html_parts: list[str] = []
    html_links: list[str] = []
    attachments: list[GmailAttachmentMetadata] = []

    for part in _walk_payload_parts(payload):
        attachment = _attachment_metadata(part)
        if attachment is not None:
            attachments.append(attachment)

        mime_type = str(part.get("mimeType") or "").lower()
        body = part.get("body", {}) if isinstance(part.get("body"), dict) else {}
        body_data = body.get("data")
        if not body_data:
            continue
        decoded = _decode_base64_url(str(body_data)).strip()
        if not decoded:
            continue
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)
            html_links.extend(_links_from_html(decoded))
        elif not part.get("parts") and mime_type.startswith("text/"):
            plain_parts.append(decoded)

    raw_text = "\n".join(plain_parts).strip()
    if not raw_text and html_parts:
        raw_text = "\n".join(_html_to_text(part) for part in html_parts).strip()
    normalized, quote_stripped = _normalize_body(raw_text)
    quote_stripped = quote_stripped or any(HTML_BLOCKQUOTE_RE.search(part) for part in html_parts)
    links = _extract_links(normalized, *html_links)
    return normalized, links, attachments, quote_stripped


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
            if THREAD_ACTION_RE.search(sentence):
                items.append(sentence)
    return _unique_nonempty(items, limit=5)


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


def _thread_level_limitations(envelopes: list[GmailMessageEnvelope]) -> list[str]:
    limitations = [
        "Read-only thread summary used sanitized Gmail message bodies from the selected thread."
    ]
    if any(envelope.attachment_metadata for envelope in envelopes):
        limitations.append("Attachments were not ingested; metadata only was screened.")
    if any(
        "Quoted prior replies were stripped before triage." in envelope.triage_limitations
        for envelope in envelopes
    ):
        limitations.append("Quoted prior replies were stripped before summary extraction.")
    return limitations


def _thread_overview(
    envelopes: list[GmailMessageEnvelope],
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
    latest = envelopes[-1]
    subject = latest.subject or next((item.subject for item in envelopes if item.subject), "")
    recent_points = _unique_nonempty(
        _thread_sentences(latest.normalized_body, latest.snippet)
        + _thread_sentences(envelopes[0].normalized_body, envelopes[0].snippet),
        limit=2,
    )
    fragments: list[str] = []
    if subject:
        fragments.append(f"Thread about {subject}.")
    if participants:
        fragments.append(f"Participants: {', '.join(participants[:3])}.")
    if recent_points:
        fragments.append(f"Recent context: {' '.join(recent_points)}")
    elif latest.thread_summary:
        fragments.append(latest.thread_summary)
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
    body, links, attachments, quote_stripped = _payload_text_links_and_attachments(payload)
    from_header = _header(headers, "From")
    sender_name, sender_email = parseaddr(from_header)
    snippet = str(data.get("snippet") or "")
    thread_summary = _thread_summary(snippet, body)
    limitations = [
        "Only the selected Gmail message was fetched; full thread history was not ingested."
    ]
    if attachments:
        limitations.append("Attachments were not ingested; metadata only was screened.")
    if quote_stripped:
        limitations.append("Quoted prior replies were stripped before triage.")
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
    body_text, html_links, html_quote_stripped = _text_for_normalization(body)
    normalized, quote_stripped = _normalize_body(body_text)
    quote_stripped = quote_stripped or html_quote_stripped
    links = _extract_links(normalized, *html_links)
    attachments = [
        GmailAttachmentMetadata.model_validate(item)
        for item in (message.get("attachment_metadata") or message.get("attachments") or [])
        if isinstance(item, Mapping)
    ]
    snippet = str(message.get("snippet") or "")
    supplied_summary = str(message.get("thread_summary") or "")
    supplied_context = str(message.get("thread_context") or "")
    thread_summary_text, _ = _normalize_body(_text_for_normalization(supplied_summary)[0])
    thread_context_text, _ = _normalize_body(_text_for_normalization(supplied_context)[0])
    thread_summary = thread_summary_text or _thread_summary(snippet, normalized)
    limitations = [str(item) for item in message.get("triage_limitations", []) if str(item).strip()]
    if quote_stripped:
        limitations.append("Quoted prior replies were stripped before triage.")
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
        extracted_links=links,
        attachment_metadata=attachments,
        thread_summary=thread_summary,
        thread_context=thread_context_text or thread_summary,
        thread_message_count=int(message.get("thread_message_count") or 1),
        suspicious_signals=_envelope_suspicious_signals(links, attachments),
        triage_limitations=limitations,
    )


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

    def _request(self, method: str, path: str, *, operation: str, **kwargs: Any) -> dict[str, Any]:
        if not self.live:
            raise GmailConfigurationError("Live Gmail mode is disabled.")

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
        envelopes = [gmail_message_envelope_from_api(message) for message in raw_messages]
        message_count = len(envelopes)
        summary, participants, action_items, deadlines, open_questions = _thread_overview(envelopes)
        thread_context = _thread_summary(
            " ".join(envelope.snippet for envelope in envelopes),
            " ".join(envelope.thread_summary for envelope in envelopes),
        )
        triage_limitations = _thread_level_limitations(envelopes)
        latest_received_at = envelopes[-1].received_at if envelopes else ""
        subject = (
            envelopes[-1].subject
            if envelopes and envelopes[-1].subject
            else next((item.subject for item in envelopes if item.subject), "")
        )
        messages = []
        for envelope in envelopes:
            normalized = envelope.model_copy(
                update={
                    "thread_message_count": message_count,
                    "thread_context": thread_context,
                    "triage_limitations": triage_limitations,
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
                    "envelope": normalized.model_dump(mode="json"),
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
        output = {
            "status": "draft_created",
            "message_id": message_id,
            "draft_id": str(data.get("id") or ""),
            "sent": False,
            "approval_required": True,
        }
        return enforce_tool_output_guardrails("gmail_create_draft_reply", output)

    def create_draft(self, to: str, subject: str, body: str) -> dict[str, Any]:
        if not self.live:
            return self.create_draft_reply(
                message_id="dry-run-message",
                body=body,
                to=to,
                subject=subject,
            )

        enforce_tool_input_guardrails(
            "gmail_create_draft",
            {"to": to, "subject": subject, "body": body},
        )
        to_address = to.strip()
        if not to_address:
            raise ValueError("Gmail draft recipient cannot be empty.")

        message = EmailMessage()
        message["To"] = to_address
        message["Subject"] = subject.strip()
        message.set_content(body.strip())

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        data = self._request(
            "POST",
            "drafts",
            operation="create draft",
            json={"message": {"raw": encoded_message}},
        )
        created_message = data.get("message", {})
        if not isinstance(created_message, Mapping):
            created_message = {}
        output = {
            "status": "draft_created",
            "message_id": str(created_message.get("id") or ""),
            "draft_id": str(data.get("id") or ""),
            "to": to_address,
            "subject": subject.strip(),
            "body_preview": body[:120],
            "sent": False,
            "approval_required": True,
        }
        return enforce_tool_output_guardrails("gmail_create_draft", output)

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        """Sending external email is intentionally unavailable."""

        raise NotImplementedError("External email sending is not implemented.")


def list_recent_messages(
    label: str | None = None,
    max_results: int = 1,
    query: str | None = None,
) -> list[dict[str, Any]]:
    return GmailTool(live=True).list_recent_messages(
        label=label,
        max_results=max_results,
        query=query,
    )


def search_message_summaries(
    label: str | None = None,
    max_results: int = 10,
    query: str | None = None,
) -> list[dict[str, Any]]:
    return GmailTool(live=True).search_message_summaries(
        label=label,
        max_results=max_results,
        query=query,
    )


def batch_get_messages(
    message_ids: list[str],
    *,
    skip_blocked: bool = False,
) -> list[dict[str, Any]]:
    return GmailTool(live=True).batch_get_messages(
        message_ids=message_ids,
        skip_blocked=skip_blocked,
    )


def list_threads_by_label_filter(label_filter: str, max_results: int = 5) -> list[dict[str, Any]]:
    return GmailTool(live=True).list_threads_by_label_filter(
        label_filter=label_filter,
        max_results=max_results,
    )


def get_message(message_id: str) -> dict[str, Any]:
    return GmailTool(live=True).get_message(message_id=message_id)


def get_thread(thread_id: str) -> dict[str, Any]:
    return GmailTool(live=True).get_thread(thread_id=thread_id)


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
def create_gmail_draft_reply(message_id: str, body: str) -> str:
    """Create a dry-run Gmail reply draft. This tool never sends email."""

    return _json(
        GmailTool(live=False).create_draft_reply(
            message_id=message_id,
            body=body,
        )
    )
