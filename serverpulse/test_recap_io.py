"""The Claude API call, against a throwaway local HTTP server (no internet, no real key)."""
import asyncio

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from serverpulse import recap, recap_io  # noqa: E402


async def serve(monkeypatch, handler):
    app = web.Application()
    app.router.add_post("/v1/messages", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(recap_io, "RECAP_API_URL", str(server.make_url("/v1/messages")))
    return server


async def test_the_request_carries_the_key_version_and_body_and_returns_the_json(monkeypatch):
    seen = {}

    async def handler(request):
        seen["headers"], seen["body"] = dict(request.headers), await request.json()
        return web.json_response({"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 3, "output_tokens": 2}, "stop_reason": "end_turn"})

    server = await serve(monkeypatch, handler)
    try:
        body = recap.build_request("claude-sonnet-5-5", "SYS", "USER", 100)
        payload = await recap_io.call_claude("sk-test", body)
    finally:
        await server.close()
    assert seen["headers"]["x-api-key"] == "sk-test" and seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["headers"]["Content-Type"].startswith("application/json")
    assert seen["body"] == body
    assert recap.parse_response(payload).text == "hi"


@pytest.mark.parametrize("status, expected", [
    (401, "key was rejected"), (403, "isn't allowed"), (404, "doesn't know that model"), (429, "rate-limiting or out of credit"),
    (500, "busy right now"), (529, "busy right now"), (400, "refused the request (400): credit balance is too low"),
])
async def test_api_errors_become_plain_messages_without_leaking_the_key(monkeypatch, status, expected):
    async def handler(request):
        return web.json_response({"type": "error", "error": {"type": "x", "message": "credit balance is too low"}}, status=status)

    server = await serve(monkeypatch, handler)
    try:
        with pytest.raises(recap.RecapError) as err:
            await recap_io.call_claude("sk-secret-123", recap.build_request("m", "s", "u", 10))
    finally:
        await server.close()
    assert expected in str(err.value) and "sk-secret-123" not in str(err.value)


async def test_a_non_json_error_page_is_still_handled(monkeypatch):
    async def handler(request):
        return web.Response(text="<html>bad gateway</html>", status=502)

    server = await serve(monkeypatch, handler)
    try:
        with pytest.raises(recap.RecapError, match="busy right now"):
            await recap_io.call_claude("k", recap.build_request("m", "s", "u", 10))
    finally:
        await server.close()


async def test_a_slow_answer_times_out_politely(monkeypatch):
    async def handler(request):
        await asyncio.sleep(2)
        return web.json_response({})

    server = await serve(monkeypatch, handler)
    try:
        with pytest.raises(recap.RecapError, match="too long"):
            await recap_io.call_claude("k", recap.build_request("m", "s", "u", 10), timeout=0.2)
    finally:
        await server.close()


async def test_an_unreachable_api_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(recap_io, "RECAP_API_URL", "http://127.0.0.1:9/v1/messages")  # nothing listens on port 9
    with pytest.raises(recap.RecapError, match="Couldn't reach"):
        await recap_io.call_claude("k", recap.build_request("m", "s", "u", 10), timeout=5)
