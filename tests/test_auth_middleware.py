"""Tests for the AuthMiddleware ASGI 401 door and identity propagation."""

from __future__ import annotations

import asyncio

from agentpay.services.auth import AuthMiddleware, current_agent_id

KEYS = {"sk-good": "support-bot"}


def drive(middleware, headers):
    """Run the middleware as an ASGI callable; return (status, seen_identity)."""
    seen = {}

    async def inner_app(scope, receive, send):
        seen["identity"] = current_agent_id.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    mw = AuthMiddleware(inner_app, KEYS)
    asyncio.run(mw(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, seen.get("identity"), sent


def test_missing_header_gets_401_and_never_reaches_app():
    status, identity, sent = drive(AuthMiddleware, {})
    assert status == 401
    assert identity is None  # inner app never ran
    # advertises the scheme
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert any(h[0] == b"www-authenticate" for h in start["headers"])


def test_wrong_key_gets_401():
    status, identity, _ = drive(AuthMiddleware, {"authorization": "Bearer sk-nope"})
    assert status == 401
    assert identity is None


def test_valid_key_reaches_app_with_identity_set():
    status, identity, _ = drive(AuthMiddleware, {"authorization": "Bearer sk-good"})
    assert status == 200
    assert identity == "support-bot"


def test_basic_scheme_is_rejected():
    status, _, _ = drive(AuthMiddleware, {"authorization": "Basic sk-good"})
    assert status == 401


def test_non_http_scope_passes_through():
    called = {}

    async def inner_app(scope, receive, send):
        called["ran"] = scope["type"]

    async def noop(*a):
        return {"type": "lifespan.startup"}

    mw = AuthMiddleware(inner_app, KEYS)
    asyncio.run(mw({"type": "lifespan"}, noop, noop))
    assert called["ran"] == "lifespan"  # not blocked by auth
