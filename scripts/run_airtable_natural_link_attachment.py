#!/usr/bin/env python3
"""Join natural HTTPS attachment selection to a reversible Airtable provider proof."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.config import load_settings
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.operational_context import AirtableContextResult
from keystone_agents.tools.internal_data_tools import (
    AIRTABLE_TEST_RECORD_MARKER,
    airtable_delete_test_record_impl,
    airtable_read_records_impl,
    airtable_write_record_impl,
)

EXPECTED_MODEL = "gpt-5.4-mini"
MAX_REQUESTS = 3
MAX_BUDGET_USD = 0.08
DEFAULT_RECEIPT_URL = (
    "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--receipt-url", default=DEFAULT_RECEIPT_URL)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/airtable-natural-link-attachment-live.json"),
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.model != EXPECTED_MODEL:
        raise SystemExit(f"Airtable attachment validation requires model={EXPECTED_MODEL}.")
    if args.max_openai_requests != MAX_REQUESTS:
        raise SystemExit("Airtable attachment validation requires max_openai_requests=3.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Airtable attachment validation requires a budget at or below $0.08.")
    if not args.receipt_url.startswith("https://"):
        raise SystemExit("Airtable attachment validation requires an HTTPS receipt URL.")


def _run_model(
    prompt: str,
    *,
    model: str,
    max_turns: int = MAX_REQUESTS,
) -> Any:
    return run_typed_sdk_agent(
        agent=build_airtable_context_agent(model=model, request_text=prompt),
        typed_input={"request": prompt},
        output_type=AirtableContextResult,
        live=True,
        workflow_name="Keystone Airtable natural HTTPS attachment validation",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        max_turns=max_turns,
    )


def _tool_names(raw_result: Any) -> list[str]:
    names: list[str] = []
    for item in getattr(raw_result, "new_items", []) or []:
        raw_item = getattr(item, "raw_item", None)
        name = (
            getattr(item, "name", None)
            or getattr(raw_item, "name", None)
            or getattr(getattr(item, "tool", None), "name", None)
        )
        if name and str(name) not in names:
            names.append(str(name))
    return names


def _attachment_count(record: dict[str, Any], field_name: str = "Attachments") -> int:
    fields = record.get("fields") if isinstance(record, dict) else {}
    attachments = fields.get(field_name, []) if isinstance(fields, dict) else []
    return len(attachments) if isinstance(attachments, list) else 0


def execute_validation(
    *,
    model: str,
    receipt_url: str,
    budget_usd: float,
    model_runner: Callable[..., Any] = _run_model,
) -> dict[str, Any]:
    suffix = uuid4().hex[:10]
    marker = f"{AIRTABLE_TEST_RECORD_MARKER} natural-link-{suffix}"
    approval = f"operator-command:anu-198-link:{suffix}"
    record_id = ""
    create: dict[str, Any] = {}
    delete: dict[str, Any] = {}
    failure = ""
    result: Any | None = None
    observed_count = 0
    try:
        create = airtable_write_record_impl(
            json.dumps(
                {
                    "Item": marker,
                    "Description": f"{marker} HTTPS attachment selection validation",
                    "Amount": "1.00",
                },
                sort_keys=True,
            ),
            table="Business Expenses",
            base_alias="finance_tax_tracker",
            approval_reference=f"{approval}:create",
            operation="create",
            validate_schema=True,
            live=True,
        )
        record_id = str(create.get("record_id") or "")
        if not record_id or not create.get("verification", {}).get("passed"):
            raise RuntimeError("Marked Airtable setup record did not verify.")
        filename = f"{marker.replace(' ', '-')}-receipt.pdf"
        prompt = (
            f"Attach the receipt at {receipt_url} to the exact existing Business Expenses "
            f"record {record_id} in the Attachments field of finance_tax_tracker. "
            f"Use filename {filename}. This is a credential-free HTTPS URL, not a local "
            f"file. Execute the approved live attachment now using approval reference "
            f"{approval}:attachment. Do not call a local-file upload tool, create another "
            "record, modify other fields, or perform any other write."
        )
        result = model_runner(prompt, model=model, max_turns=MAX_REQUESTS)
        readback = airtable_read_records_impl(
            "Business Expenses",
            base_alias="finance_tax_tracker",
            filter_formula=f"RECORD_ID()='{record_id}'",
            max_records=1,
            live=True,
        )
        records = readback.get("records", [])
        if isinstance(records, list) and len(records) == 1:
            observed_count = _attachment_count(records[0])
    except Exception as exc:
        failure = str(exc)
    finally:
        if record_id:
            try:
                delete = airtable_delete_test_record_impl(
                    record_id,
                    table="Business Expenses",
                    base_alias="finance_tax_tracker",
                    approval_reference=f"{approval}:delete",
                    live=True,
                )
            except Exception as exc:
                failure = f"{failure}; cleanup failed: {exc}".strip("; ")

    usage = dict(getattr(result, "usage", {}) or {})
    cost = dict(getattr(result, "cost", {}) or {})
    request_cache = dict(getattr(result, "request_cache", {}) or {})
    tool_names = _tool_names(getattr(result, "raw_result", None)) if result else []
    requests = int(usage.get("requests") or 0)
    estimated_usd = float(cost.get("estimated_usd") or 0.0)
    delete_verification = delete.get("verification", {})
    record_absent_after = bool(
        isinstance(delete_verification, dict)
        and delete_verification.get("record_absent_after")
    )
    passed = bool(
        not failure
        and 1 <= requests <= MAX_REQUESTS
        and estimated_usd <= budget_usd
        and int(request_cache.get("rate_limit_retries") or 0) == 0
        and "airtable_link_attachment" in tool_names
        and "airtable_upload_attachment" not in tool_names
        and observed_count == 1
        and delete.get("verification", {}).get("passed")
        and record_absent_after
    )
    return {
        "status": "pass" if passed else "partial",
        "failure": failure,
        "scenario": "airtable_natural_https_attachment_selection",
        "model": model,
        "request_ceiling": MAX_REQUESTS,
        "budget_usd": budget_usd,
        "usage": usage,
        "cost": cost,
        "rate_limit_retries": int(request_cache.get("rate_limit_retries") or 0),
        "tool_selection": {
            "tool_names": tool_names,
            "selected_link_tool": "airtable_link_attachment" in tool_names,
            "selected_local_upload_tool": "airtable_upload_attachment" in tool_names,
        },
        "provider": {
            "record_id_present": bool(record_id),
            "create_verified": bool(create.get("verification", {}).get("passed")),
            "attachment_count_after": observed_count,
            "delete_verified": bool(delete.get("verification", {}).get("passed")),
            "record_absent_after": record_absent_after,
        },
        "safety": {
            "synthetic_record": True,
            "https_url_only": True,
            "other_records_modified": False,
            "send_enabled": False,
        },
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    _validate_args(args)
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    payload = execute_validation(
        model=args.model,
        receipt_url=args.receipt_url,
        budget_usd=args.budget_usd,
    )
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
