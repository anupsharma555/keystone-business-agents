"""Typed target-to-artifact scope resolution without positional fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generic, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True)
class TargetScopeResolution(Generic[_T]):
    """One deterministic mapping between requested and available targets."""

    selected_item: _T | None = None
    requested_targets: tuple[str, ...] = ()
    available_targets: tuple[str, ...] = ()
    missing_targets: tuple[str, ...] = ()
    extra_targets: tuple[str, ...] = ()
    duplicate_targets: tuple[str, ...] = ()
    explicit_count: int = 0
    ambiguous: bool = False

    @property
    def requires_batch(self) -> bool:
        return self.explicit_count > 1 or len(self.requested_targets) > 1

    @property
    def selected_ref(self) -> _T | None:
        """Compatibility name used by existing artifact-specific callers."""

        return self.selected_item

    @property
    def selected_targets(self) -> tuple[str, ...]:
        """Compatibility name for the available target identities."""

        return self.available_targets


def normalize_target_key(value: str) -> str:
    """Return a conservative identity key for a typed entity name."""

    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def resolve_target_scope(
    *,
    requested_targets: list[str],
    available_targets: list[tuple[str, _T]],
    explicit_count: int = 0,
) -> TargetScopeResolution[_T]:
    """Resolve one exact target or report the bounded ambiguity."""

    requested_by_key = {
        normalize_target_key(target): " ".join(str(target or "").split())
        for target in requested_targets
        if normalize_target_key(target)
    }
    available_by_key: dict[str, list[tuple[str, _T]]] = {}
    for name, item in available_targets:
        cleaned = " ".join(str(name or "").split())
        key = normalize_target_key(cleaned)
        if key:
            available_by_key.setdefault(key, []).append((cleaned, item))
    requested_keys = set(requested_by_key)
    available_keys = set(available_by_key)
    matching_keys = requested_keys & available_keys
    duplicate_keys = {
        key for key, matching_items in available_by_key.items() if len(matching_items) > 1
    }
    selected_item: _T | None = None
    ambiguous = bool(duplicate_keys)
    if (
        len(requested_keys) == 1
        and len(matching_keys) == 1
        and not (matching_keys & duplicate_keys)
    ):
        selected_item = available_by_key[next(iter(matching_keys))][0][1]
    elif (
        not requested_keys
        and len(available_keys) == 1
        and not duplicate_keys
    ):
        selected_item = available_by_key[next(iter(available_keys))][0][1]
    elif not requested_keys and len(available_keys) > 1:
        ambiguous = True
    return TargetScopeResolution(
        selected_item=selected_item,
        requested_targets=tuple(requested_by_key[key] for key in sorted(requested_keys)),
        available_targets=tuple(
            available_by_key[key][0][0] for key in sorted(available_keys)
        ),
        missing_targets=tuple(
            requested_by_key[key] for key in sorted(requested_keys - available_keys)
        ),
        extra_targets=tuple(
            available_by_key[key][0][0]
            for key in sorted(available_keys - requested_keys)
        ),
        duplicate_targets=tuple(
            available_by_key[key][0][0] for key in sorted(duplicate_keys)
        ),
        explicit_count=max(0, int(explicit_count or 0)),
        ambiguous=ambiguous,
    )


__all__ = ["TargetScopeResolution", "normalize_target_key", "resolve_target_scope"]
