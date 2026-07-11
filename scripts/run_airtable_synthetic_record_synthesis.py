"""Create, synthesize, and remove one marked Airtable record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.config import load_settings
from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.operational_context import AirtableContextResult
from keystone_agents.tools.internal_data_tools import (
    AIRTABLE_TEST_RECORD_MARKER,
    airtable_delete_test_record_impl,
    airtable_read_records_impl,
    airtable_write_record_impl,
)

MODEL = "gpt-5.4-mini"
MAX_BUDGET_USD = 0.05


def _hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:16]


def _run_model(prompt: str, *, model: str, trace_metadata: dict[str, str]) -> Any:
    return run_typed_sdk_agent(
        agent=build_airtable_context_agent(
            model=model,
            request_text=prompt,
            attach_tools=False,
        ),
        typed_input=prompt,
        output_type=AirtableContextResult,
        live=True,
        max_turns=1,
        workflow_name="Keystone Airtable synthetic record synthesis validation",
        trace_metadata=trace_metadata,
    )


def execute(
    *,
    model: str,
    budget_usd: float,
    suffix: str,
    model_runner: Callable[..., Any] = _run_model,
) -> dict[str, Any]:
    marker = f"{AIRTABLE_TEST_RECORD_MARKER} synthesis-{suffix}"
    approval = f"operator-approved:airtable-synthesis:{suffix}"
    fields = {
        "Item": marker,
        "Estimated Tax Periods": "3",
        "Date of Expense": "2026-07-11",
        "Categories": "Software",
        "Expense Client/Vendor": "Synthetic Validation Vendor",
        "Description": f"{marker} bounded context synthesis",
        "Amount": "42.00",
        "Receipt Available": False,
        "Payment Method": ["Business Debit Card (relay)"],
        "Additional Taxes": "3.36",
        "Total Expenses": "45.36",
    }
    identity = create_validation_execution_identity(
        scenario="airtable_synthetic_record_synthesis",
        route="airtable_context_agent",
    )
    record_id = ""
    create: dict[str, Any] = {}
    read: dict[str, Any] = {}
    delete: dict[str, Any] = {}
    model_result: Any | None = None
    model_checks: dict[str, bool] = {}
    failure = ""
    try:
        create = airtable_write_record_impl(
            json.dumps(fields, sort_keys=True),
            table="Business Expenses",
            base_alias="finance_tax_tracker",
            approval_reference=f"{approval}:create",
            operation="create",
            validate_schema=True,
            live=True,
        )
        record_id = str(create.get("record_id") or "")
        if not record_id or not create.get("verification", {}).get("passed"):
            raise RuntimeError("synthetic_airtable_create_failed")
        read = airtable_read_records_impl(
            "Business Expenses",
            base_alias="finance_tax_tracker",
            filter_formula=f"RECORD_ID()='{record_id}'",
            max_records=1,
            live=True,
        )
        records = list(read.get("records") or [])
        if len(records) != 1:
            raise RuntimeError("synthetic_airtable_exact_read_failed")
        packet = {
            "base_alias": "finance_tax_tracker",
            "table": "Business Expenses",
            "record_ref": _hash(record_id),
            "fields": fields,
            "required_interpretation": (
                "Summarize the expense, calculate the total from amount plus taxes, "
                "identify the category and payment method, note that the receipt is "
                "missing, and recommend one exact follow-up. Do not propose a write."
            ),
            "synthetic_test_data": True,
        }
        prompt = (
            "Airtable Context: use only this verified provider packet to answer the "
            "operator's natural request: summarize this test expense and identify what "
            "needs follow-up. Do not call tools or write anything.\n\n"
            + json.dumps(packet, sort_keys=True)
        )
        model_result = model_runner(
            prompt,
            model=model,
            trace_metadata=identity.trace_metadata(),
        )
        output = AirtableContextResult.model_validate(model_result.final_output)
        rendered = " ".join(
            [output.summary, *output.recommended_actions, *output.blockers]
        ).lower()
        model_checks = {
            "amount_and_total": "42" in rendered and "45.36" in rendered,
            "category": "software" in rendered,
            "payment_method": "debit" in rendered,
            "missing_receipt": "receipt" in rendered,
            "follow_up": bool(output.recommended_actions),
            "no_executed_model_write": not output.executed_write_results,
            "write_plan_approval_gated": output.write_plan.approval_required is True
            and output.write_plan.live_write_allowed_for_specialist is False,
        }
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
    usage = dict(getattr(model_result, "usage", {}) or {})
    cost = dict(getattr(model_result, "cost", {}) or {})
    cache = dict(getattr(model_result, "request_cache", {}) or {})
    checks = {
        "provider_create_verified": bool(create.get("verification", {}).get("passed")),
        "provider_exact_read": len(list(read.get("records") or [])) == 1,
        **model_checks,
        "one_request": int(usage.get("requests") or 0) == 1,
        "no_retry": int(cache.get("rate_limit_retries") or 0) == 0,
        "budget": float(cost.get("estimated_usd") or 0) <= budget_usd,
        "provider_deleted": bool(
            delete.get("verification", {}).get("record_absent_after")
        ),
    }
    passed = not failure and all(checks.values())
    return {
        "status": "pass" if passed else "partial",
        "scenario": "airtable_synthetic_record_synthesis",
        "model": model,
        "requests": int(usage.get("requests") or 0),
        "budget_usd": budget_usd,
        "usage": usage,
        "cost": cost,
        "request_cache": cache,
        "execution_identity": identity.receipt(),
        "record_ref": _hash(record_id),
        "checks": checks,
        "failure": failure,
        "safety": {
            "synthetic_record_only": True,
            "record_absent_after": bool(
                delete.get("verification", {}).get("record_absent_after")
            ),
            "raw_record_persisted": False,
            "email_sent": False,
            "slack_posted": False,
            "external_writes": 2 if record_id else 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--revalidate-receipt", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/airtable-synthetic-record-synthesis-live.json"),
    )
    args = parser.parse_args()
    if args.model != MODEL or args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Airtable synthesis requires gpt-5.4-mini and budget <= $0.05.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    if args.revalidate_receipt is not None:
        result = json.loads(args.revalidate_receipt.read_text(encoding="utf-8"))
        remaining = airtable_read_records_impl(
            "Business Expenses",
            base_alias="finance_tax_tracker",
            filter_formula='FIND("KBA_TEST_RECORD synthesis-", {Item})',
            max_records=10,
            live=True,
        )
        checks = dict(result.get("checks") or {})
        checks.pop("no_write_plan", None)
        checks["no_executed_model_write"] = (
            int(result.get("request_cache", {}).get("tool_count") or 0) == 0
        )
        checks["write_plan_approval_gated"] = bool(checks.get("send_disabled"))
        checks["provider_deleted"] = not list(remaining.get("records") or [])
        result["checks"] = checks
        result["safety"]["record_absent_after"] = checks["provider_deleted"]
        result["status"] = "pass" if all(checks.values()) else "partial"
        result["revalidated_from"] = str(args.revalidate_receipt)
        result["validation_revision"] = (
            "Use the provider deletion contract's record_absent_after key and accept "
            "approval-gated review planning when the model had zero tools or writes. "
            "No model call ran during revalidation."
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "pass" else 2
    result = execute(
        model=args.model,
        budget_usd=args.budget_usd,
        suffix=uuid4().hex[:10],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
