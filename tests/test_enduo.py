from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
import requests

from enduo_fitatu.config import load_config
from enduo_fitatu.enduo import Enduo, EnduoAuthError, EnduoError, EnduoToolError

Answer = Callable[[int], tuple[int, Any]]


def reply(status: int, body: Any, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = (body if isinstance(body, str) else json.dumps(body)).encode()
    response.headers.update({"content-type": "application/json", **(headers or {})})
    return response


class Server:
    """An MCP server in place of the HTTP session: it opens a session and answers tools/call with what the test gives."""

    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def post(self, url: str, data: str, **kwargs: Any) -> requests.Response:
        message = json.loads(data)
        if message["method"] == "initialize":
            return reply(200, {"jsonrpc": "2.0", "id": message["id"], "result": {}}, {"mcp-session-id": "session-1"})
        if message["method"] != "tools/call":
            return reply(202, "")
        self.calls.append(message["params"]["name"])
        status, body = self.answer(message["id"])
        return reply(status, body)


tokens = iter(f"token-{number}" for number in range(1000))


def enduo(answer: Answer, token: str | None = None) -> tuple[Enduo, Server]:
    client = Enduo(load_config({"ENDUO_URL": "https://api.enduo.test"}), token or next(tokens))
    server = Server(answer)
    client.http = server  # type: ignore[assignment]
    return client, server


def tool_error(text: str) -> Answer:
    return lambda request_id: (
        200,
        {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": text}]}},
    )


def rpc_error(code: int, message: str, data: Any = None, status: int = 200) -> Answer:
    return lambda request_id: (status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message, "data": data}})


def test_returns_what_a_tool_returned() -> None:
    client, _ = enduo(
        lambda request_id: (200, {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {"day": "2026-11-14"}}})
    )

    assert client.call_tool("nutrition_diary_set", {}) == {"day": "2026-11-14"}


@pytest.mark.parametrize(
    "answer",
    [
        tool_error("Invalid input. meals[0].kcal: This value should be between 0 and 10000."),
        rpc_error(-32602, "Invalid parameters for tool 'nutrition_diary_set': meals", {"validation_errors": [{}]}),
        rpc_error(-32602, "Invalid parameters", {"validation_errors": [{}]}, status=400),
        lambda request_id: (422, "Unprocessable"),
    ],
)
def test_input_enduo_refuses_as_invalid_is_a_refusal_of_this_day(answer: Answer) -> None:
    client, _ = enduo(answer)

    with pytest.raises(EnduoToolError):
        client.call_tool("nutrition_diary_set", {})


@pytest.mark.parametrize(
    "answer",
    [
        tool_error('Access denied. The token lacks the "write:food_log" scope.'),
        lambda request_id: (401, "no"),
        lambda request_id: (403, "no"),
    ],
)
def test_a_token_enduo_does_not_take_is_a_sign_in_problem_not_a_bad_day(answer: Answer) -> None:
    client, _ = enduo(answer)

    with pytest.raises(EnduoAuthError):
        client.call_tool("nutrition_diary_set", {})


@pytest.mark.parametrize(
    "answer",
    [
        lambda request_id: (400, "later"),
        lambda request_id: (429, "later"),
        lambda request_id: (500, "later"),
        lambda request_id: (503, "later"),
        lambda request_id: (200, "not json"),
        # "Invalid params" without validation errors is an unknown tool, a protocol error: no day is at fault.
        rpc_error(-32602, "Tool not found"),
        rpc_error(-32603, "Error while executing tool"),
    ],
)
def test_everything_that_is_not_about_the_day_is_a_failure_to_try_again(answer: Answer) -> None:
    client, _ = enduo(answer)

    with pytest.raises(EnduoError):
        client.call_tool("nutrition_diary_set", {})


def test_asks_once_per_token_which_account_it_belongs_to() -> None:
    def whoami(request_id: int) -> tuple[int, Any]:
        me = {"user": {"id": "account-a", "email": "me@example.com", "timezone": "Europe/Warsaw"}}
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": me}}

    first, server = enduo(whoami, "token-of-a")
    assert first.account() == "account-a"
    assert first.account() == "account-a"
    again, other = enduo(whoami, "token-of-a")
    assert again.account() == "account-a"
    assert (server.calls, other.calls) == (["whoami"], [])

    nobody, _ = enduo(lambda request_id: (200, {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {"scopes": []}}}))
    with pytest.raises(EnduoError, match="which account"):
        nobody.account()
