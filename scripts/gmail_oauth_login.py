"""Create a local Gmail OAuth token file for live draft-only Gmail access."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from keystone_agents.tools.gmail_tool import (
    DEFAULT_GOOGLE_CREDENTIALS_FILE,
    DEFAULT_GOOGLE_TOKEN_FILE,
    GMAIL_SCOPES,
    GOOGLE_TOKEN_URL,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class OAuthCallbackState:
    """Mutable state shared with the one-request local callback server."""

    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self.code: str | None = None
        self.error: str | None = None


def _load_desktop_client(credentials_file: Path) -> dict[str, Any]:
    if not credentials_file.is_file():
        raise FileNotFoundError(f"Gmail OAuth credentials file not found: {credentials_file}")
    data = json.loads(credentials_file.read_text(encoding="utf-8"))
    client = data.get("installed") or data.get("web") or {}
    if not isinstance(client, dict):
        client = {}
    if not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("Gmail OAuth credentials file is missing client_id or client_secret.")
    return client


def _build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str,
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTH_URL}?{query}"


def _callback_handler(callback_state: OAuthCallbackState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name.
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            state = params.get("state", [""])[0]
            code = params.get("code", [""])[0]
            error = params.get("error", [""])[0]

            if state != callback_state.expected_state:
                callback_state.error = "OAuth state mismatch."
                self._send_page("Authorization failed. State mismatch. Return to terminal.")
                return
            if error:
                callback_state.error = error
                self._send_page(f"Authorization failed: {error}. Return to terminal.")
                return
            if not code:
                callback_state.error = "OAuth callback did not include an authorization code."
                self._send_page("Authorization failed. Missing code. Return to terminal.")
                return

            callback_state.code = code
            self._send_page("Authorization complete. You can close this tab.")

        def log_message(self, _format: str, *_args: object) -> None:
            return None

        def _send_page(self, message: str) -> None:
            body = (
                "<!doctype html><html><body>"
                f"<p>{message}</p>"
                "<p>Keystone will not send email automatically.</p>"
                "</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _exchange_code_for_token(
    *,
    client: dict[str, Any],
    code: str,
    redirect_uri: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    token_uri = str(client.get("token_uri") or GOOGLE_TOKEN_URL)
    response = requests.post(
        token_uri,
        data={
            "code": code,
            "client_id": str(client["client_id"]),
            "client_secret": str(client["client_secret"]),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Token exchange failed with HTTP {response.status_code}.")
    token = response.json()
    if not isinstance(token, dict) or not token.get("access_token"):
        raise RuntimeError("Token exchange returned no access token.")
    if not token.get("refresh_token"):
        raise RuntimeError(
            "Token exchange returned no refresh token. Revoke the app grant, then rerun this "
            "script with prompt=consent."
        )
    return {
        **token,
        "client_id": str(client["client_id"]),
        "client_secret": str(client["client_secret"]),
        "token_uri": token_uri,
        "scopes": list(GMAIL_SCOPES),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Gmail OAuth token.json locally.")
    parser.add_argument(
        "--credentials-file",
        default=os.getenv("GOOGLE_CREDENTIALS_FILE", DEFAULT_GOOGLE_CREDENTIALS_FILE),
        help="Path to Desktop OAuth client JSON.",
    )
    parser.add_argument(
        "--token-file",
        default=os.getenv("GOOGLE_TOKEN_FILE", DEFAULT_GOOGLE_TOKEN_FILE),
        help="Path where token JSON should be written.",
    )
    parser.add_argument("--host", default="localhost", help="Loopback callback host.")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Loopback callback port. 0 chooses one.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="How long to wait for browser authorization.",
    )
    parser.add_argument("--no-browser", action="store_true", help="Print URL without opening it.")
    parser.add_argument("--print-url-only", action="store_true", help="Only print the auth URL.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing token file.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    credentials_file = Path(args.credentials_file)
    token_file = Path(args.token_file)
    if token_file.exists() and not args.force and not args.print_url_only:
        raise SystemExit(f"{token_file} already exists. Use --force to replace it.")

    client = _load_desktop_client(credentials_file)
    state = secrets.token_urlsafe(24)
    callback_state = OAuthCallbackState(expected_state=state)

    if args.print_url_only:
        redirect_uri = f"http://{args.host}:{args.port or 8080}/"
        print(
            _build_auth_url(
                client_id=str(client["client_id"]),
                redirect_uri=redirect_uri,
                scopes=GMAIL_SCOPES,
                state=state,
            )
        )
        return 0

    with HTTPServer((args.host, args.port), _callback_handler(callback_state)) as server:
        host, port = server.server_address
        redirect_host = args.host if args.host != "127.0.0.1" else host
        redirect_uri = f"http://{redirect_host}:{port}/"
        auth_url = _build_auth_url(
            client_id=str(client["client_id"]),
            redirect_uri=redirect_uri,
            scopes=GMAIL_SCOPES,
            state=state,
        )
        print(auth_url)
        if not args.no_browser:
            webbrowser.open(auth_url)

        server.timeout = args.timeout_seconds
        server.handle_request()

    if callback_state.error:
        raise SystemExit(callback_state.error)
    if not callback_state.code:
        raise SystemExit("Timed out waiting for Gmail OAuth callback.")

    token_data = _exchange_code_for_token(
        client=client,
        code=callback_state.code,
        redirect_uri=redirect_uri,
        timeout_seconds=30.0,
    )
    token_file.write_text(json.dumps(token_data, indent=2, sort_keys=True), encoding="utf-8")
    token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"Wrote {token_file}. Do not commit or print this file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
