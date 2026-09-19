"""Bounded source identity for read-only Zotero continuations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ZoteroMetadataScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str
    library_type: Literal["user", "group"] = "user"
    collection_key: str = ""
    item_key: str = ""
    query: str = ""
    tag: str = ""
    limit: int = Field(default=25, ge=1, le=100)
    sort: str = ""
    direction: str = ""
    top_level_only: bool = False
    item_type: str = ""
    require_abstract: bool = False
    selection_rank: int = Field(default=1, ge=1, le=10)
    selection_count: int = Field(default=1, ge=1, le=10)
    include_abstract_text: bool = True


class ZoteroMetadataContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ZoteroMetadataScope
    next_start: int = Field(ge=1, lt=1000)
    pages_read: int = Field(ge=1, lt=10)
    eligible_items_seen: int = Field(ge=0, le=1000)
    library_version: int = Field(ge=0)


class ZoteroChildrenContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str
    library_type: Literal["user", "group"]
    parent_item_key: str
    limit: int = Field(ge=1, le=100)
    next_start: int = Field(ge=1, lt=1000)
    pages_read: int = Field(ge=1, lt=10)
    library_version: int = Field(ge=0)


class ZoteroNoteContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str
    library_type: Literal["user", "group"]
    parent_item_key: str
    note_item_key: str
    version: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    next_char: int = Field(ge=1, le=120000)
    windows_read: int = Field(ge=1, lt=10)


class ZoteroPdfContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str
    library_type: Literal["user", "group"]
    parent_item_key: str
    attachment_item_key: str
    version: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    next_page: int = Field(ge=0)
    next_char: int = Field(ge=0)
    max_pages: int = Field(ge=1, le=100)
    max_chars: int = Field(ge=1000, le=200000)
    windows_read: int = Field(ge=1, lt=10)
