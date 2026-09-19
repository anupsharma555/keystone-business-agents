"""Freeze accepted workflow JSON independently of ephemeral input files."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from keystone_agents.runtime.durable_execution import ExecutionConflict
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.storage.sqlite_store import stable_json


def _checked_json_copy(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(dict(payload), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ExecutionConflict(
            "Workflow context must contain only JSON data, not runtime clients."
        ) from exc
    if json.loads(stable_json(copied)) != copied:
        raise ExecutionConflict("Workflow context requires redaction before persistence.")
    return copied


def freeze_workflow_context(request: WorkflowRunRequest) -> WorkflowRunRequest:
    """Capture a file once; JSON references do not authorize attached media bytes."""
    payload = request.model_dump(mode="python")
    if request.context_file_path and request.context_file_snapshot is None:
        file_payload = json.loads(Path(request.context_file_path).read_text(encoding="utf-8"))
        if not isinstance(file_payload, dict):
            raise ValueError("context_file_path must contain a JSON object.")
        payload["context_file_snapshot"] = file_payload
    return WorkflowRunRequest.model_validate(_checked_json_copy(payload))


def restore_workflow_context(
    request: WorkflowRunRequest, saved_request: Mapping[str, Any]
) -> WorkflowRunRequest:
    """Reuse only the same accepted request identified by a saved execution/origin."""
    saved = WorkflowRunRequest.from_checkpoint(saved_request)
    supplied = _checked_json_copy(request.model_dump(mode="python"))
    accepted = _checked_json_copy(saved.model_dump(mode="python"))
    supplied_snapshot = supplied.pop("context_file_snapshot")
    accepted_snapshot = accepted.pop("context_file_snapshot")
    if supplied != accepted or (
        supplied_snapshot is not None and supplied_snapshot != accepted_snapshot
    ):
        raise ExecutionConflict(
            "Execution identity refers to a different accepted workflow request."
        )
    if saved.context_file_path and accepted_snapshot is None:
        raise ExecutionConflict(
            "Saved execution has no accepted context snapshot; review before retry."
        )
    return freeze_workflow_context(saved)
