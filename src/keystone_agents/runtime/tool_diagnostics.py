"""Keep bounded tool-contract verdicts without retaining returned source bodies."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def _name(value: Any) -> str:
    return (
        value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", value) else ""
    )


def _names(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return list(dict.fromkeys(name for item in value[:24] if (name := _name(item))))


def _counts(value: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in keys
        if key in value and (value[key] is None or type(value[key]) is int and value[key] >= 0)
    }


def compact_tool_diagnostics(cache: Mapping[str, Any]) -> dict[str, Any]:
    """Project host-generated verdict fields; never copy arbitrary cache content."""
    result: dict[str, Any] = {}
    post = cache.get("tool_execution_postcondition")
    if (
        isinstance(post, Mapping)
        and post.get("schema") == "keystone.tool_execution_postcondition.v1"
    ):
        if (
            isinstance(post.get("mode"), str)
            and post["mode"] in {"required", "optional", "forbidden"}
            and type(post.get("satisfied")) is bool
        ):
            result["tool_execution_postcondition"] = {
                "schema": post["schema"],
                "mode": post["mode"],
                "stage": _name(post.get("stage")),
                "satisfied": post["satisfied"],
                **{
                    key: _names(post.get(key))
                    for key in (
                        "attempted_tool_names",
                        "completed_tool_names",
                        "receipt_tool_names",
                        "missing_groups",
                        "prohibited_tool_names",
                    )
                },
            }
    visibility = cache.get("source_window_visibility")
    if isinstance(visibility, list):
        retained = []
        for item in visibility[:4]:
            if (
                not isinstance(item, Mapping)
                or item.get("schema") != "keystone.source_window_visibility.v1"
            ):
                continue
            observations = item.get("observations")
            retained.append(
                {
                    "schema": item["schema"],
                    "observation_kind": "is_enabled_callback_evaluations_not_provider_schema",
                    "tool_names": _names(item.get("tool_names")),
                    **_counts(item, ("evaluation_count",)),
                    "truncated": item.get("truncated") is True,
                    "observations": [
                        {
                            **_counts(row, ("limit", "remaining", "consumed")),
                            **(
                                {"enabled": row["enabled"]}
                                if type(row.get("enabled")) is bool
                                else {}
                            ),
                        }
                        for row in observations[:12]
                        if isinstance(row, Mapping)
                    ]
                    if isinstance(observations, list)
                    else [],
                }
            )
        if retained:
            result["source_window_visibility"] = retained
    return result
