"""Proxy-level tests for auth-token spend-limit rotation.

Exercises ``HeadroomProxy._retry_request_with_token_rotation`` and its helpers
with a mocked ``_retry_request`` (mirroring the mock style in
``tests/test_proxy_streaming_ratelimit_headers.py``). No network or real proxy
init — just the rotation control flow.
"""

from __future__ import annotations

import httpx

from headroom.proxy.auth_token_pool import TokenPool
from headroom.proxy.models import ProxyConfig
from headroom.proxy.server import HeadroomProxy

BUDGET = {
    "error": {
        "type": "budget_exceeded",
        "message": "Budget has been exceeded! Team=x Current cost: 3212.27, Max budget: 3200.0",
    }
}
GENERIC_400 = {"error": {"type": "invalid_request_error", "message": "invalid model id"}}


def _make_proxy(tokens, cooldown=3600):
    proxy = object.__new__(HeadroomProxy)
    proxy.config = ProxyConfig(auth_token_cooldown_s=cooldown)
    proxy.auth_token_pool = TokenPool(tokens, cooldown_s=cooldown) if tokens is not None else None
    return proxy


async def test_rotates_to_next_token_on_budget_error():
    proxy = _make_proxy(["tok-a", "tok-b"])
    seen = []

    async def fake_retry(method, url, headers, body, **kw):
        seen.append(headers.get("authorization"))
        if headers.get("authorization") == "Bearer tok-a":
            return httpx.Response(400, json=BUDGET)
        return httpx.Response(200, json={"ok": True})

    proxy._retry_request = fake_retry
    resp = await proxy._retry_request_with_token_rotation(
        "POST", "http://x/v1/messages", {"authorization": "Bearer client"}, {"messages": []}
    )

    assert resp.status_code == 200
    assert seen == ["Bearer tok-a", "Bearer tok-b"]  # rotated exactly once
    # tok-a is now in cooldown; tok-b is the active token.
    assert proxy.auth_token_pool.current() == "tok-b"


async def test_all_tokens_exhausted_returns_429():
    proxy = _make_proxy(["tok-a", "tok-b"])

    async def fake_retry(method, url, headers, body, **kw):
        return httpx.Response(400, json=BUDGET)

    proxy._retry_request = fake_retry
    resp = await proxy._retry_request_with_token_rotation("POST", "u", {}, {})

    assert resp.status_code == 429
    payload = resp.json()
    assert payload["error"]["type"] == "all_tokens_exhausted"
    # The client-facing message surfaces the last upstream budget error.
    assert "Budget has been exceeded" in payload["error"]["message"]
    assert proxy.auth_token_pool.all_exhausted()


async def test_generic_400_passes_through_without_rotation():
    proxy = _make_proxy(["tok-a", "tok-b"])
    calls = 0

    async def fake_retry(method, url, headers, body, **kw):
        nonlocal calls
        calls += 1
        return httpx.Response(400, json=GENERIC_400)

    proxy._retry_request = fake_retry
    resp = await proxy._retry_request_with_token_rotation("POST", "u", {}, {})

    assert resp.status_code == 400
    assert calls == 1  # did not rotate on a non-budget error
    assert proxy.auth_token_pool.active_count() == 2  # no token marked exhausted


async def test_no_pool_is_transparent_passthrough():
    proxy = _make_proxy(None)
    sentinel = httpx.Response(200, json={"ok": 1})
    captured = {}

    async def fake_retry(method, url, headers, body, **kw):
        captured["headers"] = headers
        return sentinel

    proxy._retry_request = fake_retry
    resp = await proxy._retry_request_with_token_rotation(
        "POST", "u", {"authorization": "Bearer client"}, {}
    )

    assert resp is sentinel
    # Client header is forwarded untouched when no pool is configured.
    assert captured["headers"]["authorization"] == "Bearer client"


async def test_success_on_first_token_no_rotation():
    proxy = _make_proxy(["tok-a", "tok-b"])
    calls = 0

    async def fake_retry(method, url, headers, body, **kw):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    proxy._retry_request = fake_retry
    resp = await proxy._retry_request_with_token_rotation("POST", "u", {}, {})

    assert resp.status_code == 200
    assert calls == 1
    assert proxy.auth_token_pool.active_count() == 2


def test_apply_pool_token_overrides_auth_case_insensitive():
    proxy = object.__new__(HeadroomProxy)
    out = proxy._apply_pool_token(
        {
            "Authorization": "Bearer old",
            "X-Api-Key": "secret",
            "anthropic-version": "2023-06-01",
        },
        "newtok",
    )
    assert out["authorization"] == "Bearer newtok"
    assert not any(k.lower() == "x-api-key" for k in out)
    assert not any(k == "Authorization" for k in out)  # old auth dropped
    assert out["anthropic-version"] == "2023-06-01"  # unrelated headers preserved


def test_all_tokens_exhausted_response_without_last_response():
    proxy = object.__new__(HeadroomProxy)
    proxy.config = ProxyConfig(auth_token_cooldown_s=1800)
    resp = proxy._all_tokens_exhausted_response(None)
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "all_tokens_exhausted"
    assert "1800s" in resp.json()["error"]["message"]
