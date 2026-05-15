"""FileSearch corpus manifest loading and upload planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

DEFAULT_CORPUS_MANIFEST = Path("docs/corpus/seed_manifest.json")


@dataclass(frozen=True)
class CorpusFile:
    """One approved local file prepared for vector store upload."""

    path: Path
    relative_path: str
    corpus: str
    approved_for: tuple[str, ...]
    sensitivity: str
    source_url: str | None = None
    license: str | None = None

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

    @property
    def attributes(self) -> dict[str, str]:
        attributes = {
            "source_path": self.relative_path,
            "corpus": self.corpus,
            "approved_for": ",".join(self.approved_for),
            "sensitivity": self.sensitivity,
            "generated_from": str(DEFAULT_CORPUS_MANIFEST),
        }
        if self.source_url:
            attributes["source_url"] = self.source_url
        if self.license:
            attributes["license"] = self.license
        return attributes


@dataclass(frozen=True)
class CorpusUploadPlan:
    """Validated set of files to upload to a hosted vector store."""

    manifest_path: Path
    files: tuple[CorpusFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


def load_corpus_manifest(manifest_path: Path = DEFAULT_CORPUS_MANIFEST) -> dict[str, Any]:
    """Load the FileSearch corpus manifest."""

    return json.loads(manifest_path.read_text())


def build_corpus_upload_plan(
    *,
    project_root: Path,
    manifest_path: Path = DEFAULT_CORPUS_MANIFEST,
    corpus_name: str | None = None,
) -> CorpusUploadPlan:
    """Build and validate an upload plan from the corpus manifest."""

    resolved_manifest = manifest_path
    if not resolved_manifest.is_absolute():
        resolved_manifest = project_root / resolved_manifest
    manifest = load_corpus_manifest(resolved_manifest)
    blocked_patterns = tuple(manifest.get("blocked_patterns", ()))
    source_metadata = _source_metadata_by_path(manifest)

    files: list[CorpusFile] = []
    seen: set[str] = set()
    for corpus in manifest.get("corpora", []):
        name = str(corpus.get("name", ""))
        if corpus_name and name != corpus_name:
            continue
        for rel_path in corpus.get("local_files", []):
            rel_path = str(rel_path)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            if _matches_blocked_pattern(rel_path, blocked_patterns):
                raise ValueError(f"Blocked FileSearch corpus path: {rel_path}")
            path = project_root / rel_path
            if not path.is_file():
                raise FileNotFoundError(f"Missing FileSearch corpus path: {rel_path}")
            metadata = source_metadata.get(rel_path, {})
            files.append(
                CorpusFile(
                    path=path,
                    relative_path=rel_path,
                    corpus=name,
                    approved_for=tuple(corpus.get("approved_for", ())),
                    sensitivity=str(corpus.get("sensitivity", "")),
                    source_url=metadata.get("source_url"),
                    license=metadata.get("license"),
                )
            )

    if corpus_name and not files:
        raise ValueError(f"No files found for FileSearch corpus: {corpus_name}")
    return CorpusUploadPlan(manifest_path=resolved_manifest, files=tuple(files))


def corpus_upload_plan_summary(plan: CorpusUploadPlan) -> dict[str, Any]:
    """Return a JSON-safe summary for dry-run output and tests."""

    return {
        "manifest_path": str(plan.manifest_path),
        "file_count": len(plan.files),
        "total_bytes": plan.total_bytes,
        "corpora": sorted({item.corpus for item in plan.files}),
        "files": [
            {
                "path": item.relative_path,
                "size_bytes": item.size_bytes,
                "attributes": item.attributes,
            }
            for item in plan.files
        ],
    }


def _matches_blocked_pattern(path: str, blocked_patterns: tuple[str, ...]) -> bool:
    path_name = Path(path).name
    return any(
        fnmatch(path, pattern) or fnmatch(path_name, pattern)
        for pattern in blocked_patterns
    )


def _source_metadata_by_path(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for corpus in manifest.get("corpora", []):
        for source in corpus.get("external_sources_summarized", []):
            local_summary = source.get("local_summary")
            if local_summary:
                metadata[str(local_summary)] = {
                    "source_url": str(source.get("source_url", "")),
                }
        for source in corpus.get("external_sources_vendored", []):
            for rel_path in source.get("local_files", []):
                metadata[str(rel_path)] = {
                    "source_url": str(source.get("source_url", "")),
                    "license": str(source.get("license", "")),
                }
    return metadata
