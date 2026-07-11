"""Create, verify, and remove marked PNG/PDF copies of one exact slide."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from keystone_agents.config import with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    presentation_delete_test_artifact_local_impl,
    presentation_extract_slide_copy_local_impl,
)

DEFAULT_OUTPUT = Path("artifacts/test-pack/presentation-derived-copy-lifecycle.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relative_path")
    parser.add_argument("--slide-number", type=int, default=2)
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-local-write", action="store_true")
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    suffix = uuid4().hex[:10]
    approval = args.approval_reference.strip() or f"anu-213-slide-{suffix}"
    live = bool(args.live_local_write)
    created: dict[str, dict[str, object]] = {}
    deleted: dict[str, dict[str, object]] = {}
    cleanup_errors: list[str] = []
    failure = ""
    try:
        for output_format in ("png", "pdf"):
            created[output_format] = presentation_extract_slide_copy_local_impl(
                args.relative_path,
                args.slide_number,
                output_format=output_format,
                output_name=f"KBA_TEST_SLIDE-{suffix}-slide-{args.slide_number}.{output_format}",
                approval_reference=f"{approval}:create-{output_format}",
                live=live,
            )
            if live and not _verification_passed(created[output_format]):
                raise RuntimeError(f"Derived {output_format} copy did not verify.")
        if not live:
            payload = _payload("preview", created, deleted, cleanup_errors)
            _write_receipt(args.output, payload)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live:
            for output_format, result in created.items():
                artifact_path = str(result.get("artifact_path") or "")
                if not artifact_path:
                    continue
                try:
                    deleted[output_format] = presentation_delete_test_artifact_local_impl(
                        artifact_path,
                        approval_reference=f"{approval}:delete-{output_format}",
                        live=True,
                    )
                except Exception as exc:
                    cleanup_errors.append(
                        f"{output_format} cleanup: {type(exc).__name__}: {exc}"
                    )

    passed = bool(
        not failure
        and not cleanup_errors
        and set(created) == {"png", "pdf"}
        and set(deleted) == {"png", "pdf"}
        and all(_verification_passed(result) for result in created.values())
        and all(_verification_passed(result) for result in deleted.values())
    )
    payload = _payload(
        "passed" if passed else "failed",
        created,
        deleted,
        cleanup_errors,
        failure=failure,
    )
    _write_receipt(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed else 1


def _verification_passed(result: dict[str, object]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _payload(
    status: str,
    created: dict[str, dict[str, object]],
    deleted: dict[str, dict[str, object]],
    cleanup_errors: list[str],
    *,
    failure: str = "",
) -> dict[str, object]:
    return {
        "status": status,
        "openai_requests": 0,
        "created": created,
        "deleted": deleted,
        "failure": failure,
        "cleanup_errors": cleanup_errors,
        "parent_modified": False,
        "send_or_post": False,
    }


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
