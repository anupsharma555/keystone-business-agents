"""Build bounded Responses API file/image inputs from explicit local paths."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_LOCAL_INPUT_FILE_BYTES = 50 * 1024 * 1024
SUPPORTED_FILE_EXTENSIONS = frozenset({".pdf"})
SUPPORTED_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
SUPPORTED_EXTENSIONS = SUPPORTED_FILE_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS

LOCAL_PATH_RE = re.compile(
    r"(?P<path>(?:~|/Users/|/private/|/tmp/)[^\s\"'<>]+?\.(?:pdf|png|jpe?g|webp|gif))",
    flags=re.I,
)
BLOCKED_PATH_PARTS = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".config",
    "credentials",
    "credential",
    "secrets",
    "secret",
    "oauth",
    "token",
}


@dataclass(frozen=True)
class LocalFileAttachment:
    """One explicit local file prepared for model input."""

    path: Path
    filename: str
    mime_type: str
    kind: str
    size_bytes: int
    input_part: dict[str, Any]
    checksum_sha256: str = ""


@dataclass(frozen=True)
class LocalFileBytes:
    """Validated local file bytes for approved provider upload tools."""

    path: Path
    filename: str
    mime_type: str
    size_bytes: int
    data: bytes


@dataclass(frozen=True)
class LocalFileInputBundle:
    """Responses input items plus audit-safe diagnostics for local attachments."""

    attachments: tuple[LocalFileAttachment, ...]
    diagnostics: tuple[str, ...]

    @property
    def has_inputs(self) -> bool:
        return bool(self.attachments)

    def prompt_note(self) -> str:
        if not self.attachments:
            return ""
        lines = [
            "Local file inputs were attached from explicit operator-provided paths.",
            (
                "Use the attached file/image content as bounded evidence; "
                "do not infer from filename alone."
            ),
        ]
        for attachment in self.attachments:
            lines.append(
                f"- {attachment.filename}: {attachment.kind}, {attachment.mime_type}, "
                f"{attachment.size_bytes} bytes."
            )
        if self.diagnostics:
            lines.append("Attachment diagnostics: " + "; ".join(self.diagnostics))
        return "\n".join(lines)

    def response_input(self, prompt: str) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        note = self.prompt_note()
        if note:
            content.append({"type": "input_text", "text": note})
        content.extend(attachment.input_part for attachment in self.attachments)
        return [{"role": "user", "content": content}]


def local_file_input_bundle_from_text(text: object) -> LocalFileInputBundle:
    """Return OpenAI Responses input parts for explicit safe local file paths."""

    if not isinstance(text, str):
        return LocalFileInputBundle((), ())
    return _local_file_input_bundle_from_paths(_extract_local_paths(text))


def local_file_input_bundle_from_operator_input(
    value: object,
    *,
    attachment_paths: Sequence[str | Path] = (),
) -> LocalFileInputBundle:
    """Read only operator text or attachment paths admitted by the caller's ingress.

    Structured provider evidence and rendered prompts are not attachment authority.
    A typed input may preserve the operator's wording in its top-level raw_request;
    authenticated ingress can also pass attachment_paths independently of that text.
    """

    if isinstance(value, str):
        request = value
    elif isinstance(value, Mapping):
        request = value.get("raw_request", "")
    else:
        request = getattr(value, "raw_request", "")
    paths = _extract_local_paths(request) if isinstance(request, str) else []
    paths.extend(Path(path) for path in attachment_paths)
    return _local_file_input_bundle_from_paths(paths)


def _local_file_input_bundle_from_paths(paths: Sequence[Path]) -> LocalFileInputBundle:
    attachments: list[LocalFileAttachment] = []
    diagnostics: list[str] = []
    seen: set[Path] = set()
    for raw_path in paths:
        if raw_path in seen:
            continue
        seen.add(raw_path)
        try:
            attachment = _attachment_for_path(raw_path)
        except (OSError, ValueError) as exc:
            diagnostics.append(f"{Path(raw_path).name}: {type(exc).__name__}: {exc}")
            continue
        attachments.append(attachment)
    return LocalFileInputBundle(tuple(attachments), tuple(diagnostics))


def read_supported_local_file(path: str | Path) -> LocalFileBytes:
    """Read a safe explicit local PDF/image path for a bounded live upload."""

    requested = Path(path).expanduser()
    _reject_unsafe_path(requested)
    resolved = requested.resolve(strict=True)
    _reject_unsafe_path(resolved)
    suffix = resolved.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS or requested.suffix.lower() != suffix:
        raise ValueError(f"unsupported extension {suffix}")
    data = _read_local_file_bounded(resolved)
    size = len(data)
    _validate_file_signature(data, suffix)
    return LocalFileBytes(
        path=resolved,
        filename=resolved.name,
        mime_type=_mime_type_for_path(resolved),
        size_bytes=size,
        data=data,
    )


def _read_local_file_bounded(path: Path) -> bytes:
    """Pin canonical parent directories and the file without following new symlinks."""

    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("safe local-file reads require no-follow directory access")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    with os.fdopen(file_fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a readable regular file")
        if before.st_size <= 0:
            raise ValueError("empty file")
        if before.st_size > MAX_LOCAL_INPUT_FILE_BYTES:
            raise ValueError("file exceeds 50 MB direct input limit")
        data = handle.read(MAX_LOCAL_INPUT_FILE_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(data) > MAX_LOCAL_INPUT_FILE_BYTES:
        raise ValueError("file exceeds 50 MB direct input limit")
    if (
        len(data) != before.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError("local file changed while being read")
    return data


def _validate_file_signature(data: bytes, suffix: str) -> None:
    signatures = {
        ".pdf": (b"%PDF-",),
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".gif": (b"GIF87a", b"GIF89a"),
    }
    matches = (
        data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        if suffix == ".webp"
        else data.startswith(signatures[suffix])
    )
    if not matches:
        raise ValueError("file signature does not match its supported extension")


def _extract_local_paths(text: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for match in LOCAL_PATH_RE.finditer(text):
        raw = match.group("path").rstrip(".,;:)")
        path = Path(raw).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _attachment_for_path(path: Path) -> LocalFileAttachment:
    local_file = read_supported_local_file(path)
    resolved = local_file.path
    suffix = resolved.suffix.lower()
    mime_type = local_file.mime_type
    data_url = f"data:{mime_type};base64,{base64.b64encode(local_file.data).decode('ascii')}"
    if suffix in SUPPORTED_FILE_EXTENSIONS:
        kind = "input_file"
        part = {
            "type": "input_file",
            "filename": resolved.name,
            "file_data": data_url,
        }
    else:
        kind = "input_image"
        part = {
            "type": "input_image",
            "image_url": data_url,
            "detail": "high",
        }
    return LocalFileAttachment(
        path=resolved,
        filename=resolved.name,
        mime_type=mime_type,
        kind=kind,
        size_bytes=local_file.size_bytes,
        input_part=part,
        checksum_sha256=hashlib.sha256(local_file.data).hexdigest(),
    )


def _reject_unsafe_path(path: Path) -> None:
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & BLOCKED_PATH_PARTS:
        raise ValueError("path is blocked by local-file safety policy")
    name = path.name.lower()
    if name.startswith(".") or any(marker in name for marker in (".env", "private-key")):
        raise ValueError("file is blocked by local-file safety policy")


def _mime_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"
