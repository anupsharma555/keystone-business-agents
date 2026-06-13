"""Local HTTP server for the Promptfoo eval dashboard and human scoring form."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from promptfoo.eval_dashboard import (
    DEFAULT_DASHBOARD_PATH,
    dashboard_case_export_csv,
    dashboard_case_export_rows,
    dashboard_payload,
    render_dashboard,
    render_review_form,
)
from promptfoo.eval_database import DEFAULT_EVAL_DB, set_promptfoo_analysis_exclusion
from promptfoo.eval_urls import eval_dashboard_url
from promptfoo.human_review import (
    SCORE_DIMENSIONS,
    HumanEvalReview,
    save_human_review,
)


def save_human_review_payload(
    payload: dict[str, Any],
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    require_recorded_response: bool = False,
) -> dict[str, Any]:
    """Validate and persist one dashboard human-review form payload."""

    case_id = str(payload.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("case_id is required")
    if require_recorded_response and not _case_has_recorded_response(
        case_id,
        database_path=database_path,
    ):
        raise ValueError("human review requires a recorded Promptfoo or Slack response")
    raw_scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    scores: dict[str, float] = {}
    for dimension in SCORE_DIMENSIONS:
        if dimension not in raw_scores:
            continue
        scores[dimension] = _coerce_score(raw_scores[dimension], dimension)
    if not scores:
        raise ValueError("at least one score is required")
    safety = str(payload.get("safety") or "pass").strip().lower()
    if safety not in {"pass", "fail"}:
        raise ValueError("safety must be pass or fail")
    review = HumanEvalReview(
        case_id=case_id,
        run_id=str(payload.get("run_id") or "").strip(),
        agent=str(payload.get("agent") or "").strip(),
        reviewer=str(payload.get("reviewer") or "anup").strip() or "anup",
        scores=scores,
        safety=safety,
        notes=str(payload.get("notes") or "").strip(),
        slack_channel_id=str(payload.get("slack_channel_id") or "C0BA17Y9C01").strip(),
        slack_channel_name=str(payload.get("slack_channel_name") or "evals").strip(),
        slack_thread_ts=str(payload.get("slack_thread_ts") or "").strip(),
        raw_text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
    )
    row_id = save_human_review(review, database_path=database_path)
    result = review.to_dict()
    result["id"] = row_id
    return result


def _case_has_recorded_response(
    case_id: str,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> bool:
    payload = dashboard_payload(database_path=database_path, limit=1000)
    normalized = str(case_id or "").strip()
    for case in payload.get("cases", []):
        if case.get("case_id") != normalized:
            continue
        return bool(
            str(case.get("response_text") or case.get("latest_slack_summary") or "").strip()
        )
    return False


def _coerce_score(value: Any, dimension: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{dimension} must be a numeric 0-5 score") from exc
    if score < 0 or score > 5:
        raise ValueError(f"{dimension} must be between 0 and 5")
    return score


def legacy_dashboard_redirect_target(path: str, query: str = "") -> str | None:
    """Return the canonical dashboard URL for the legacy static dashboard route."""

    if not path.endswith("/.keystone/promptfoo/dashboard.html"):
        return None
    target = eval_dashboard_url()
    if query:
        target = f"{target}?{query}"
    return target


def workflow_readiness_response(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return the local-only Slack eval workflow readiness contract."""

    db_path = Path(database_path)
    payload = dashboard_payload(database_path=db_path, limit=max(1, int(limit)))
    return {
        "status": "ok",
        "database_path": str(db_path),
        "workflow_readiness": payload.get("workflow_readiness") or {},
    }


def build_handler(
    *,
    database_path: Path,
    dashboard_path: Path,
    limit: int,
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to dashboard paths."""

    class EvalDashboardHandler(BaseHTTPRequestHandler):
        server_version = "KeystoneEvalDashboard/1.0"

        def do_GET(self) -> None:  # noqa: N802 - http.server API.
            parsed = urlparse(self.path)
            path = parsed.path
            target = legacy_dashboard_redirect_target(path, parsed.query)
            if target:
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", target)
                self.end_headers()
                return
            if path in {"/", "/dashboard", "/dashboard.html"}:
                self._serve_dashboard()
                return
            if path in {"/review", "/review.html"}:
                self._serve_review(parsed.query)
                return
            if path == "/api/status":
                self._send_json({"status": "ok", "database_path": str(database_path)})
                return
            if path == "/api/workflow-readiness":
                self._serve_workflow_readiness()
                return
            if path == "/api/eval-cases":
                self._serve_eval_cases()
                return
            if path == "/api/eval-cases.csv":
                self._serve_eval_cases_csv()
                return
            if path == "/favicon.ico":
                self._send_empty(HTTPStatus.NO_CONTENT)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_HEAD(self) -> None:  # noqa: N802 - http.server API.
            parsed = urlparse(self.path)
            path = parsed.path
            target = legacy_dashboard_redirect_target(path, parsed.query)
            if target:
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", target)
                self.end_headers()
                return
            if path in {"/", "/dashboard", "/dashboard.html"}:
                self._serve_dashboard(head_only=True)
                return
            if path in {"/review", "/review.html"}:
                self._serve_review(parsed.query, head_only=True)
                return
            if path == "/api/status":
                self._send_json(
                    {"status": "ok", "database_path": str(database_path)},
                    head_only=True,
                )
                return
            if path == "/api/workflow-readiness":
                self._serve_workflow_readiness(head_only=True)
                return
            if path == "/api/eval-cases":
                self._serve_eval_cases(head_only=True)
                return
            if path == "/api/eval-cases.csv":
                self._serve_eval_cases_csv(head_only=True)
                return
            if path == "/favicon.ico":
                self._send_empty(HTTPStatus.NO_CONTENT)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802 - http.server API.
            path = urlparse(self.path).path
            if path == "/api/human-review":
                self._save_human_review()
                return
            if path == "/api/analysis-exclusion":
                self._save_analysis_exclusion()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def _save_human_review(self) -> None:
            try:
                payload = self._read_json_body()
                result = save_human_review_payload(
                    payload,
                    database_path=database_path,
                    require_recorded_response=True,
                )
                render_dashboard(
                    database_path=database_path,
                    output_path=dashboard_path,
                    limit=limit,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"status": "error", "error": str(exc)}, status=400)
                return
            self._send_json({"status": "saved", **result})

        def _save_analysis_exclusion(self) -> None:
            try:
                payload = self._read_json_body()
                result = set_promptfoo_analysis_exclusion(
                    eval_id=str(payload.get("eval_id") or ""),
                    case_id=str(payload.get("case_id") or ""),
                    excluded=bool(payload.get("excluded")),
                    reason=str(payload.get("reason") or ""),
                    database_path=database_path,
                )
                render_dashboard(
                    database_path=database_path,
                    output_path=dashboard_path,
                    limit=limit,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"status": "error", "error": str(exc)}, status=400)
                return
            self._send_json({"status": "saved", **result})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _serve_dashboard(self, *, head_only: bool = False) -> None:
            try:
                path = render_dashboard(
                    database_path=database_path,
                    output_path=dashboard_path,
                    limit=limit,
                )
                body = path.read_bytes()
            except OSError as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if head_only:
                return
            self.wfile.write(body)

        def _serve_review(self, query: str, *, head_only: bool = False) -> None:
            case_id = str((parse_qs(query).get("case") or [""])[0] or "").strip()
            if not case_id:
                self.send_error(HTTPStatus.BAD_REQUEST, "case query parameter is required")
                return
            try:
                body = render_review_form(
                    case_id=case_id,
                    database_path=database_path,
                    limit=limit,
                ).encode("utf-8")
            except OSError as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if head_only:
                return
            self.wfile.write(body)

        def _serve_eval_cases(self, *, head_only: bool = False) -> None:
            try:
                payload = dashboard_payload(database_path=database_path, limit=limit)
                rows = dashboard_case_export_rows(payload)
            except OSError as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(
                {
                    "status": "ok",
                    "database_path": str(database_path),
                    "rows": rows,
                },
                head_only=head_only,
            )

        def _serve_workflow_readiness(self, *, head_only: bool = False) -> None:
            try:
                payload = workflow_readiness_response(
                    database_path=database_path,
                    limit=limit,
                )
            except OSError as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(payload, head_only=head_only)

        def _serve_eval_cases_csv(self, *, head_only: bool = False) -> None:
            try:
                payload = dashboard_payload(database_path=database_path, limit=limit)
                body = dashboard_case_export_csv(payload).encode("utf-8")
            except OSError as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="keystone-eval-cases.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if head_only:
                return
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                raise ValueError("JSON body is required")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _send_json(
            self,
            payload: dict[str, Any],
            *,
            status: int = 200,
            head_only: bool = False,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if head_only:
                return
            self.wfile.write(body)

        def _send_empty(self, status: HTTPStatus) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return EvalDashboardHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local Promptfoo eval dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--database-path", default=str(DEFAULT_EVAL_DB))
    parser.add_argument("--dashboard-path", default=str(DEFAULT_DASHBOARD_PATH))
    parser.add_argument("--limit", type=int, default=500)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database_path = Path(args.database_path)
    dashboard_path = Path(args.dashboard_path)
    handler = build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=max(1, int(args.limit)),
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving Keystone eval dashboard at http://{args.host}:{args.port}/dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
