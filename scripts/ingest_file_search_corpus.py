"""Upload approved FileSearch corpus files to an OpenAI vector store."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from keystone_agents.file_search_corpus import (
    DEFAULT_CORPUS_MANIFEST,
    CorpusUploadPlan,
    build_corpus_upload_plan,
    corpus_upload_plan_summary,
)
from keystone_agents.model_provider import (
    KEYSTONE_OPENAI_API_KEY_ENV,
    KEYSTONE_OPENAI_BASE_URL_ENV,
    openai_api_key_from_env,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_CORPUS_MANIFEST),
        help="Corpus manifest path. Defaults to docs/corpus/seed_manifest.json.",
    )
    parser.add_argument(
        "--corpus",
        default="keystone-business-agents-reference",
        help="Manifest corpus name to upload.",
    )
    parser.add_argument(
        "--vector-store-id",
        default=None,
        help="Existing OpenAI vector store id. Required for live upload unless creating one.",
    )
    parser.add_argument(
        "--create-vector-store",
        action="store_true",
        help="Create a new vector store before uploading files. Requires --live.",
    )
    parser.add_argument(
        "--vector-store-name",
        default="keystone-business-agents-reference",
        help="Name for --create-vector-store.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually upload files. Without this flag, only prints the dry-run plan.",
    )
    parser.add_argument(
        "--allow-internal-corpus-upload",
        action="store_true",
        help=(
            "Permit live upload of non-public corpus files. Intended only for an "
            "operator-controlled environment after reviewing the manifest."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main() -> None:
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    plan = build_corpus_upload_plan(
        project_root=PROJECT_ROOT,
        manifest_path=Path(args.manifest),
        corpus_name=args.corpus,
    )

    if not args.live:
        _print_output({"dry_run": True, **corpus_upload_plan_summary(plan)}, json_output=args.json)
        return

    if _contains_non_public_files(plan) and not args.allow_internal_corpus_upload:
        raise SystemExit(
            "Live upload is restricted to public_reference corpus files by default. "
            "Use --corpus keystone-business-agents-public-vendor-reference for smoke "
            "tests, or rerun with --allow-internal-corpus-upload only after reviewing "
            "the manifest in an operator-controlled environment."
        )
    if args.vector_store_id and args.create_vector_store:
        raise SystemExit("Use either --vector-store-id or --create-vector-store, not both.")
    if not args.vector_store_id and not args.create_vector_store:
        raise SystemExit("Live upload requires --vector-store-id or --create-vector-store.")
    api_key = openai_api_key_from_env()
    if not api_key:
        raise SystemExit(
            f"{KEYSTONE_OPENAI_API_KEY_ENV} is required for live FileSearch corpus ingestion."
        )

    client = _build_openai_client(api_key=api_key)
    vector_store_id = args.vector_store_id
    if args.create_vector_store:
        vector_store = client.vector_stores.create(
            name=args.vector_store_name,
            metadata={
                "keystone_corpus": args.corpus,
                "generated_from": str(DEFAULT_CORPUS_MANIFEST),
            },
        )
        vector_store_id = vector_store.id

    uploaded = _upload_plan(client=client, vector_store_id=str(vector_store_id), plan=plan)
    _print_output(
        {
            "dry_run": False,
            "vector_store_id": vector_store_id,
            "uploaded_count": len(uploaded),
            "uploaded": uploaded,
        },
        json_output=args.json,
    )


def _upload_plan(
    *,
    client: OpenAI,
    vector_store_id: str,
    plan: CorpusUploadPlan,
) -> list[dict[str, Any]]:
    uploaded = []
    for item in plan.files:
        with item.path.open("rb") as handle:
            vector_store_file = client.vector_stores.files.upload_and_poll(
                vector_store_id=vector_store_id,
                file=(item.relative_path, handle),
                attributes=item.attributes,
            )
        uploaded.append(
            {
                "path": item.relative_path,
                "vector_store_file_id": vector_store_file.id,
                "status": getattr(vector_store_file, "status", None),
            }
        )
    return uploaded


def _contains_non_public_files(plan: CorpusUploadPlan) -> bool:
    return any(item.sensitivity != "public_reference" for item in plan.files)


def _build_openai_client(*, api_key: str) -> OpenAI:
    base_url = os.environ.get(KEYSTONE_OPENAI_BASE_URL_ENV)
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url and base_url.strip():
        kwargs["base_url"] = base_url.strip()
    return OpenAI(**kwargs)


def _print_output(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload["dry_run"]:
        print(
            "Dry run: "
            f"{payload['file_count']} files, {payload['total_bytes']} bytes, "
            f"corpora={', '.join(payload['corpora'])}"
        )
        print("Run again with --live and --vector-store-id or --create-vector-store to upload.")
        return
    print(
        f"Uploaded {payload['uploaded_count']} files to vector store {payload['vector_store_id']}."
    )


if __name__ == "__main__":
    main()
