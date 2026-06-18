"""Tests for the auth-token pool and spend-limit detection.

Covers the rotation primitives in ``headroom/proxy/auth_token_pool.py``:
ordering, sticky-cooldown exhaustion, all-exhausted detection, file loading,
and the ``budget_exceeded`` signal the IBM ICA / LiteLLM endpoint returns.
"""

from __future__ import annotations

import time

import pytest

from headroom.proxy.auth_token_pool import (
    DEFAULT_SPEND_LIMIT_MATCH,
    TokenPool,
    is_spend_limit_error,
    load_tokens_from_file,
)

# The exact body observed live from the IBM ICA endpoint for an over-budget key.
ICA_BUDGET_ERROR = {
    "error": {
        "message": (
            "Budget has been exceeded! Team=6a0f1455e30c2d45c299f7e5 "
            "Current cost: 3212.277375725003, Max budget: 3200.0"
        ),
        "type": "budget_exceeded",
        "param": None,
        "code": "400",
    }
}


class TestSpendLimitDetection:
    def test_real_ica_budget_error_detected(self):
        assert is_spend_limit_error(400, ICA_BUDGET_ERROR) is True

    def test_message_fallback_when_type_missing(self):
        body = {"error": {"message": "Budget has been exceeded! Team=x", "code": "400"}}
        assert is_spend_limit_error(400, body) is True

    def test_generic_400_not_detected(self):
        # A normal client error must pass through, never burning a token.
        body = {"error": {"message": "model: invalid model id", "type": "invalid_request_error"}}
        assert is_spend_limit_error(400, body) is False

    def test_non_dict_body_not_detected(self):
        assert is_spend_limit_error(400, None) is False
        assert is_spend_limit_error(400, "not json") is False
        assert is_spend_limit_error(200, []) is False

    def test_custom_error_types(self):
        body = {"error": {"type": "quota_exhausted", "message": "no"}}
        assert is_spend_limit_error(429, body, error_types={"quota_exhausted"}) is True
        assert is_spend_limit_error(429, body) is False  # default set doesn't include it

    def test_match_disabled(self):
        body = {"error": {"message": "Budget has been exceeded!"}}
        assert is_spend_limit_error(400, body, error_types=set(), match=None) is False

    def test_default_match_constant(self):
        assert DEFAULT_SPEND_LIMIT_MATCH in ICA_BUDGET_ERROR["error"]["message"].lower()


class TestTokenPool:
    def test_order_and_dedup(self):
        pool = TokenPool(["a", "b", "a", "c", ""])
        assert len(pool) == 3
        assert pool.current() == "a"

    def test_rotation_skips_exhausted(self):
        pool = TokenPool(["a", "b", "c"], cooldown_s=3600)
        assert pool.current() == "a"
        pool.mark_exhausted("a")
        assert pool.current() == "b"
        pool.mark_exhausted("b")
        assert pool.current() == "c"
        assert pool.active_count() == 1

    def test_all_exhausted(self):
        pool = TokenPool(["a", "b"], cooldown_s=3600)
        assert pool.all_exhausted() is False
        pool.mark_exhausted("a")
        pool.mark_exhausted("b")
        assert pool.all_exhausted() is True
        assert pool.current() is None

    def test_cooldown_expiry_reactivates(self):
        pool = TokenPool(["a"], cooldown_s=0.05)
        pool.mark_exhausted("a")
        assert pool.current() is None
        time.sleep(0.06)
        # Cooldown elapsed — the token is offered again for a probe.
        assert pool.current() == "a"

    def test_reset_clears_cooldowns(self):
        pool = TokenPool(["a", "b"], cooldown_s=3600)
        pool.mark_exhausted("a")
        pool.reset()
        assert pool.current() == "a"
        assert pool.active_count() == 2

    def test_empty_pool(self):
        pool = TokenPool([])
        assert len(pool) == 0
        assert pool.current() is None
        assert pool.all_exhausted() is True


class TestLoadTokensFromFile:
    def test_loads_one_per_line_ignoring_comments(self, tmp_path):
        f = tmp_path / "tokens.txt"
        f.write_text(
            "# comment\n"
            "sk-one\n"
            "\n"
            "   sk-two  \n"
            "# another comment\n"
            "sk-one\n"  # duplicate dropped
            "sk-three\n"
        )
        assert load_tokens_from_file(f) == ["sk-one", "sk-two", "sk-three"]

    def test_from_file_classmethod(self, tmp_path):
        f = tmp_path / "tokens.txt"
        f.write_text("sk-a\nsk-b\n")
        pool = TokenPool.from_file(f, cooldown_s=10)
        assert len(pool) == 2
        assert pool.current() == "sk-a"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            load_tokens_from_file(tmp_path / "nope.txt")
