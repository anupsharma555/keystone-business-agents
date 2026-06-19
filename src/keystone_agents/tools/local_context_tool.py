"""Allowlisted local folder context tools for Keystone agents."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystone_agents.context_env import context_env_path
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool

DEFAULT_LOCAL_ZOTERO_IMPORT_REPO = Path(
    os.getenv("KEYSTONE_ZOTERO_IMPORT_REPO_DEFAULT", "../zotero-import")
)
DEFAULT_LOCAL_CONTEXT_SOURCE_ENV: dict[str, str] = {
    "keystone_neuroinformatics": "KEYSTONE_NEUROINFORMATICS_DIR",
    "zotero_active": "KEYSTONE_ZOTERO_ACTIVE_DIR",
    "zotero_app_support": "KEYSTONE_ZOTERO_APP_SUPPORT_DIR",
    "zotero_reference_archive": "KEYSTONE_ZOTERO_REFERENCE_ARCHIVE_DIR",
    "zotero_import_cache": "KEYSTONE_ZOTERO_IMPORT_CACHE",
}
TEXT_EXTENSIONS = frozenset(
    {
        ".bib",
        ".csv",
        ".html",
        ".json",
        ".md",
        ".ris",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
SKIPPED_DIR_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        "locate",
        "pipes",
        "styles",
        "translators",
    }
)
MAX_READ_CHARS = 12_000
MAX_SEARCH_RESULTS = 25
MAX_SEARCH_FILE_BYTES = 8_000_000
MAX_WALK_FILES = 2_000
SOURCE_ID_RE = re.compile(r"^[a-z0-9_:-]+$")


@dataclass(frozen=True)
class LocalContextSource:
    """One allowlisted local folder available to agent tools."""

    source_id: str
    path: Path

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def readable(self) -> bool:
        return os.access(self.path, os.R_OK)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "path": str(self.path),
            "exists": self.exists,
            "readable": self.readable,
        }


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def _valid_source_id(source_id: str) -> bool:
    return bool(SOURCE_ID_RE.fullmatch(source_id))


def _configured_source_paths() -> dict[str, Path]:
    sources = {
        source_id: Path(raw_path).expanduser()
        for source_id, env_var in DEFAULT_LOCAL_CONTEXT_SOURCE_ENV.items()
        if (raw_path := os.getenv(env_var, "").strip())
    }
    default_zotero_cache = _default_zotero_import_cache_source()
    if default_zotero_cache is not None:
        sources.setdefault("zotero_import_cache", default_zotero_cache)
    raw_json = os.getenv("KEYSTONE_LOCAL_CONTEXT_SOURCES_JSON", "").strip()
    if raw_json:
        configured = json.loads(raw_json)
        if not isinstance(configured, dict):
            raise ValueError("KEYSTONE_LOCAL_CONTEXT_SOURCES_JSON must be a JSON object.")
        sources.update(
            {
                str(source_id): Path(str(path)).expanduser()
                for source_id, path in configured.items()
                if _valid_source_id(str(source_id))
            }
        )

    raw_pairs = os.getenv("KEYSTONE_LOCAL_CONTEXT_SOURCES", "").strip()
    if raw_pairs:
        for raw_pair in raw_pairs.split(";"):
            if not raw_pair.strip() or "=" not in raw_pair:
                continue
            source_id, raw_path = raw_pair.split("=", 1)
            source_id = source_id.strip()
            if _valid_source_id(source_id):
                sources[source_id] = Path(raw_path.strip()).expanduser()
    return sources


def _default_zotero_import_cache_source() -> Path | None:
    configured_cache = context_env_path("KEYSTONE_ZOTERO_IMPORT_CACHE", "")
    if str(configured_cache) != "." and configured_cache.exists():
        return configured_cache
    collection_cache = context_env_path("ZOTERO_COLLECTION_CACHE", "")
    if collection_cache.name == "zotero_collections.json" and collection_cache.parent.exists():
        return collection_cache.parent
    import_repo = context_env_path(
        "KEYSTONE_ZOTERO_IMPORT_REPO",
        str(DEFAULT_LOCAL_ZOTERO_IMPORT_REPO),
    )
    repo_cache = import_repo / ".cache"
    if repo_cache.exists():
        return repo_cache
    default_cache = DEFAULT_LOCAL_ZOTERO_IMPORT_REPO / ".cache"
    if default_cache.exists():
        return default_cache
    return None


def local_context_sources() -> dict[str, LocalContextSource]:
    """Return configured local context sources keyed by source id."""

    return {
        source_id: LocalContextSource(source_id=source_id, path=path)
        for source_id, path in sorted(_configured_source_paths().items())
    }


def _source_or_raise(source_id: str) -> LocalContextSource:
    sources = local_context_sources()
    source = sources.get(source_id)
    if source is None:
        raise ValueError(f"Unknown local context source: {source_id}")
    if not source.exists or not source.readable:
        raise FileNotFoundError(f"Local context source is unavailable: {source_id}")
    return source


def _resolve_context_file(source_id: str, relative_path: str) -> Path:
    if not relative_path.strip():
        raise ValueError("relative_path is required.")
    raw_relative = Path(relative_path)
    if raw_relative.is_absolute():
        raise ValueError("relative_path must be relative to the selected source.")

    source = _source_or_raise(source_id)
    root = source.path.resolve()
    candidate = (root / raw_relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("relative_path escapes the selected local context source.")
    if not candidate.is_file():
        raise FileNotFoundError(f"Local context file not found: {source_id}:{relative_path}")
    return candidate


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def _relative_to_source(source: LocalContextSource, path: Path) -> str:
    return str(path.resolve().relative_to(source.path.resolve()))


def _walk_text_files(source: LocalContextSource) -> Iterable[Path]:
    yielded = 0
    for root, dirs, files in os.walk(source.path):
        dirs[:] = [
            dirname
            for dirname in dirs
            if dirname not in SKIPPED_DIR_NAMES and not dirname.startswith(".")
        ]
        for filename in files:
            if filename.startswith("."):
                continue
            path = Path(root) / filename
            if not _is_text_file(path):
                continue
            yielded += 1
            if yielded > MAX_WALK_FILES:
                return
            yield path


def _source_ids_or_default(source_ids: list[str] | None) -> list[str]:
    if source_ids:
        return [source_id for source_id in source_ids if source_id in local_context_sources()]
    return list(local_context_sources())


@function_tool(**keystone_tool_guardrail_kwargs())
def list_local_context_sources() -> str:
    """List allowlisted local context folders available to Keystone agents."""

    return _json_payload(
        {
            "mode": "local_context",
            "send_enabled": False,
            "sources": [source.to_dict() for source in local_context_sources().values()],
            "notes": [
                "Use these sources only for research context and draft support.",
                "Do not treat local context as approval for outbound external use.",
            ],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_local_context(
    query: str,
    source_ids: list[str] | None = None,
    max_results: int = 10,
) -> str:
    """Search allowlisted local text files and return capped snippets with paths."""

    normalized_query = " ".join(query.strip().lower().split())
    if not normalized_query:
        raise ValueError("query is required.")
    terms = normalized_query.split()
    limit = max(1, min(max_results, MAX_SEARCH_RESULTS))
    matches: list[dict[str, Any]] = []

    for source_id in _source_ids_or_default(source_ids):
        source = _source_or_raise(source_id)
        for path in _walk_text_files(source):
            if len(matches) >= limit:
                break
            try:
                if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lowered = text.lower()
            if not all(term in lowered for term in terms):
                continue
            index = min(lowered.find(term) for term in terms if lowered.find(term) >= 0)
            start = max(0, index - 220)
            end = min(len(text), index + 520)
            snippet = " ".join(text[start:end].split())
            matches.append(
                {
                    "source_id": source_id,
                    "relative_path": _relative_to_source(source, path),
                    "snippet": snippet,
                }
            )
        if len(matches) >= limit:
            break

    return _json_payload(
        {
            "mode": "local_context_search",
            "query": query,
            "source_ids": _source_ids_or_default(source_ids),
            "send_enabled": False,
            "raw_file_bodies_included": False,
            "matches": matches,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def read_local_context_file(
    source_id: str,
    relative_path: str,
    max_chars: int = 4_000,
) -> str:
    """Read a capped text file from an allowlisted local context source."""

    path = _resolve_context_file(source_id, relative_path)
    if not _is_text_file(path):
        raise ValueError("Only prompt-safe text file extensions can be read.")
    limit = max(1, min(max_chars, MAX_READ_CHARS))
    text = path.read_text(encoding="utf-8", errors="ignore")
    truncated = len(text) > limit
    return _json_payload(
        {
            "mode": "local_context_file",
            "source_id": source_id,
            "relative_path": relative_path,
            "send_enabled": False,
            "truncated": truncated,
            "content": text[:limit],
        }
    )
