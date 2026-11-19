"""Enduo: signing in with a code (device flow, RFC 8628) and calling MCP tools (Streamable HTTP, JSON-RPC 2.0).
The container asks only for "write:food_log", so it never takes the place of the coach container (which holds
"agent:runtime")."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

import requests

from .config import ENDUO_SCOPES, Config
from .store import Store
from .text import line

PROTOCOL_VERSION = "2025-06-18"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: JSON-RPC "invalid params": with ``validation_errors``, the arguments of a tool call did not fit its schema.
INVALID_PARAMS = -32602

LoginResult = Literal["signed_in", "denied", "expired"]


class EnduoAuthError(Exception):
    """The token is missing, revoked or lacks the scope: only a new sign-in helps."""


class EnduoToolError(Exception):
    """Enduo refused what was sent as invalid: sending the same again will not help, and the next day may be fine."""


class EnduoError(Exception):
    """Network, 429, 5xx, a protocol hiccup: nothing about one day; try again later."""


def _said(text: Any) -> str:
    """Enduo's own words in a log line or an error: one line, bounded."""
    return line(text, 300) or ""


def _refused_arguments(error: Any) -> bool:
    """The arguments of the call did not fit the schema of the tool: about this day, not about the connection."""
    data = error.get("data") if isinstance(error, dict) else None
    return isinstance(error, dict) and error.get("code") == INVALID_PARAMS and isinstance(data, dict) and "validation_errors" in data


def _qr(text: str) -> str:
    if shutil.which("qrencode") is None:
        return ""
    result = subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "1", text], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def _post_form(config: Config, path: str, fields: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    response = requests.post(
        f"{config.enduo_url}{path}",
        data=dict(fields),
        headers={"User-Agent": config.user_agent},
        allow_redirects=False,
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def device_login(config: Config, store: Store, show: Callable[[str], None] = print) -> LoginResult:
    """Shows a link and a code, waits until the athlete approves it in Enduo, and stores the token."""
    status, started = _post_form(config, "/oauth/device/code", {"client_name": config.name, "scope": ENDUO_SCOPES})
    if status != 200:
        raise EnduoError(
            f"Enduo refused to start the sign-in ({status} {started.get('error', '')}: {started.get('error_description', '')})"
        )

    link = str(started.get("verification_uri_complete"))
    rule = "─" * 62
    show(
        "\n".join(
            [
                "",
                rule,
                " Sign this container in to Enduo:",
                "",
                f"   open   {link}",
                f"   or go  {started.get('verification_uri')}  and enter  {started.get('user_code')}",
                "",
                _qr(link),
                f" The code is valid for {round(int(started.get('expires_in', 600)) / 60)} minutes.",
                rule,
                "",
            ]
        )
    )

    interval = int(started.get("interval", 5) or 5)
    while True:
        time.sleep(interval)
        status, polled = _post_form(
            config, "/oauth/device/token", {"grant_type": DEVICE_GRANT, "device_code": str(started.get("device_code"))}
        )
        if status == 200 and isinstance(polled.get("access_token"), str):
            store.save_enduo(
                {
                    "access_token": polled["access_token"],
                    "token_id": polled.get("token_id"),
                    "scope": polled.get("scope"),
                    "enduo_url": config.enduo_url,
                    "signed_in_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
            return "signed_in"
        error = polled.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            return "denied"
        if error == "expired_token":
            return "expired"
        raise EnduoError(f"Enduo answered {status} {error or ''}: {polled.get('error_description', '')}")


def enduo_token(store: Store, env: Mapping[str, str] | None = None) -> str | None:
    """ENDUO_TOKEN wins over the stored sign-in."""
    env = os.environ if env is None else env
    from_env = (env.get("ENDUO_TOKEN") or "").strip()
    if from_env:
        return from_env
    credentials = store.enduo()
    return str(credentials["access_token"]) if credentials else None


class Enduo:
    """One MCP session, opened on the first call and opened again when the server forgets it."""

    #: Account ids already asked for, by server and token: a token never changes its account.
    _accounts: ClassVar[dict[str, str]] = {}

    def __init__(self, config: Config, token: str) -> None:
        self.config = config
        self.token = token
        self.session_id: str | None = None
        self.next_id = 1
        self.http = requests.Session()

    def _send(self, message: dict[str, Any]) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent,
        }
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
            headers["Mcp-Protocol-Version"] = PROTOCOL_VERSION
        try:
            return self.http.post(self.config.mcp_url, data=json.dumps(message), headers=headers, allow_redirects=False, timeout=120)
        except requests.RequestException as error:
            raise EnduoError(f"Enduo is not reachable: {error}") from error

    def _answer(self, response: requests.Response, request_id: int, call: bool = False) -> Any:
        """The answer to a request. ``call`` says it was a tool call, the only request that carries a day: only then
        can an answer mean "this day is invalid" (EnduoToolError) instead of "try again later"."""
        if response.status_code in (401, 403):
            raise EnduoAuthError(f"Enduo refused the token ({response.status_code}): run `enduo-fitatu setup --enduo`.")

        messages: list[Any] = []
        text = response.text
        try:
            if "text/event-stream" in response.headers.get("content-type", ""):
                for event in text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(row[5:].lstrip() for row in event.split("\n") if row.startswith("data:"))
                    if data:
                        messages.append(json.loads(data))
            elif text.strip():
                parsed = json.loads(text)
                messages.extend(parsed if isinstance(parsed, list) else [parsed])
        except ValueError as error:
            if response.ok:
                raise EnduoError(f"Enduo MCP sent an answer that cannot be read ({response.status_code})") from error

        answer = next((message for message in messages if isinstance(message, dict) and message.get("id") == request_id), None)
        failure = answer.get("error") if answer is not None else None
        said = _said(failure.get("message")) if isinstance(failure, dict) else ""
        if call and _refused_arguments(failure):
            raise EnduoToolError(said or "the arguments do not fit the tool")
        if call and response.status_code == 422:
            raise EnduoToolError(said or "Enduo answered 422")
        if not response.ok:
            raise EnduoError(f"Enduo MCP answered {response.status_code}: {_said(text)}")
        if answer is None:
            raise EnduoError(f"Enduo MCP sent no answer to request {request_id}")
        if failure is not None:
            raise EnduoError(f"Enduo MCP error {failure.get('code') if isinstance(failure, dict) else None}: {said}")
        return answer.get("result")

    def _open(self) -> None:
        self.session_id = None
        request_id = self.next_id
        self.next_id += 1
        response = self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "enduo-fitatu-agent", "version": self.config.version},
                },
            }
        )
        self._answer(response, request_id)
        self.session_id = response.headers.get("mcp-session-id")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Calls a tool and returns what it returned (the structured content, or the decoded JSON text)."""
        if self.session_id is None:
            self._open()

        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
        response = self._send(message)
        if response.status_code == 404 and self.session_id is not None:
            # The server forgot the session: open a new one and try once more.
            self._open()
            response = self._send(message)
        result = self._answer(response, request_id, call=True)
        result = result if isinstance(result, dict) else {}

        text = next((part.get("text", "") for part in result.get("content") or [] if isinstance(part, dict) and "text" in part), "")
        if result.get("isError"):
            if isinstance(text, str) and text.startswith("Access denied"):
                # The token lacks the scope (or the account its athlete): no day is at fault, and none may be given up on.
                raise EnduoAuthError(f"Enduo refused the token for {name} ({_said(text)}): run `enduo-fitatu setup --enduo`.")
            raise EnduoToolError(_said(text) or f"{name} failed")
        if "structuredContent" in result:
            return result["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            return text

    def whoami(self) -> dict[str, Any]:
        """Who the token belongs to (the tool any token may call): the user with its id, e-mail and time zone, the
        scopes, the athlete."""
        me = self.call_tool("whoami")
        return me if isinstance(me, dict) else {}

    def account(self) -> str:
        """Id of the Enduo account the token belongs to, to tie the sync state to it; asked once per token."""
        key = hashlib.sha256(f"{self.config.mcp_url}\n{self.token}".encode()).hexdigest()
        known = self._accounts.get(key)
        if known is not None:
            return known
        user = self.whoami().get("user")
        account = user.get("id") if isinstance(user, dict) else None
        if not isinstance(account, str) or not account:
            raise EnduoError("Enduo did not say which account the token belongs to.")
        self._accounts[key] = account
        return account
