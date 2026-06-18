"""Streaming-path tests for auth-token spend-limit rotation.

Mirrors the mock setup in ``tests/test_proxy_streaming_ratelimit_headers.py`` to
drive ``HeadroomProxy._stream_response`` through the token-rotation loop without
a real proxy or network.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from headroom.proxy.auth_token_pool import TokenPool
from headroom.proxy.server import HeadroomProxy

BUDGET = {
    "error": {
        "type": "budget_exceeded",
        "message": "Budget has been exceeded! Team=x Current cost: 3212.27, Max budget: 3200.0",
    }
}


@pytest.fixture(autouse=True)
def _reset_codex_rate_limit_singleton():
    from headroom.subscription.codex_rate_limits import get_codex_rate_limit_state

    state = get_codex_rate_limit_state()
    saved = state._latest
    state._latest = None
    try:
        yield
    finally:
        state._latest = saved


def _make_streaming_proxy(tokens, cooldown=3600):
    proxy = object.__new__(HeadroomProxy)
    proxy.http_client = MagicMock(spec=httpx.AsyncClient)
    proxy.metrics = MagicMock()
    proxy.metrics.record_request = AsyncMock(return_value=None)
    proxy.metrics.record_failed = AsyncMock(return_value=None)
    proxy.cost_tracker = MagicMock()
    proxy.cost_tracker.estimate_cost.return_value = 0.001
    proxy.cost_tracker.record_request.return_value = None
    proxy.stats = {
        "requests_total": 0,
        "requests_optimized": 0,
        "tokens": {"original": 0, "optimized": 0, "saved": 0},
        "cost": {"total_usd": 0, "savings_usd": 0},
        "errors": 0,
        "active_requests": 0,
        "requests_per_model": {},
    }
    proxy.memory_manager = None
    config = MagicMock()
    config.memory_enabled = False
    config.ccr_inject_tool = False
    config.retry_max_attempts = 3
    config.retry_base_delay_ms = 0
    config.retry_max_delay_ms = 0
    # Real values for the rotation/detection logic (MagicMock would mis-evaluate `in`).
    config.spend_limit_error_types = {"budget_exceeded"}
    config.spend_limit_match = "budget has been exceeded"
    config.auth_token_cooldown_s = cooldown
    proxy.config = config
    proxy._parse_sse_usage_from_buffer = MagicMock(return_value=None)
    proxy.memory_handler = None
    proxy.auth_token_pool = TokenPool(tokens, cooldown_s=cooldown)
    return proxy


def _budget_response():
    r = AsyncMock()
    r.status_code = 400
    r.headers = httpx.Headers({"content-type": "application/json"})
    r.aread = AsyncMock(return_value=json.dumps(BUDGET).encode())
    r.aclose = AsyncMock()
    return r


def _success_stream_response():
    r = AsyncMock()
    r.status_code = 200
    r.headers = httpx.Headers({"content-type": "text/event-stream"})

    async def aiter_bytes():
        yield (
            b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1"}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )

    r.aiter_bytes = aiter_bytes
    r.aclose = AsyncMock()
    return r


def _call_kwargs():
    return {
        "url": "https://api.nextgen-beta.ica.ibm.com/ica/v1/messages",
        "headers": {"authorization": "Bearer client", "anthropic-version": "2023-06-01"},
        "body": {
            "model": "claude-sonnet-4-5",
            "max_tokens": 16,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "request_id": "test-stream",
        "original_tokens": 10,
        "optimized_tokens": 10,
        "tokens_saved": 0,
        "transforms_applied": [],
        "tags": {},
        "optimization_latency": 0.0,
    }


@pytest.mark.asyncio
async def test_streaming_rotates_past_exhausted_token():
    proxy = _make_streaming_proxy(["tok-a", "tok-b"])
    sent_auth = []

    def build_request(method, url, content, headers):
        sent_auth.append(headers.get("authorization"))
        return MagicMock()

    proxy.http_client.build_request = MagicMock(side_effect=build_request)
    proxy.http_client.send = AsyncMock(
        side_effect=[_budget_response(), _success_stream_response()]
    )

    result = await proxy._stream_response(**_call_kwargs())

    # First token hit the budget, proxy rotated to the second and streamed.
    assert result.status_code == 200
    assert sent_auth == ["Bearer tok-a", "Bearer tok-b"]
    assert proxy.auth_token_pool.current() == "tok-b"


@pytest.mark.asyncio
async def test_streaming_all_tokens_exhausted_returns_429():
    proxy = _make_streaming_proxy(["tok-a"])
    proxy.http_client.build_request = MagicMock(return_value=MagicMock())
    proxy.http_client.send = AsyncMock(side_effect=[_budget_response()])

    result = await proxy._stream_response(**_call_kwargs())

    assert result.status_code == 429
    payload = json.loads(bytes(result.body))
    assert payload["error"]["type"] == "all_tokens_exhausted"
    assert proxy.auth_token_pool.all_exhausted()
