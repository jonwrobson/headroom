"""Tests for model routing (headroom/proxy/model_routing.py) and its wiring."""

from __future__ import annotations

import json

import pytest

from headroom.proxy.model_routing import parse_model_map, resolve_model_id

# The tier map the ICA Docker helper ships by default.
ICA_MAP = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus-4-7": "claude-opus-4-7",
    "opus-4-8": "claude-opus-4-8",
    "opus": "claude-opus-4-8",
}


class TestParseModelMap:
    def test_empty_and_none(self):
        assert parse_model_map(None) == {}
        assert parse_model_map("") == {}
        assert parse_model_map("   ") == {}

    def test_json_string(self):
        assert parse_model_map('{"a": "b", "c": "d"}') == {"a": "b", "c": "d"}

    def test_json_file(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text(json.dumps(ICA_MAP), encoding="utf-8")
        assert parse_model_map(str(p)) == ICA_MAP

    def test_invalid_json_fails_open(self):
        assert parse_model_map("{not json") == {}

    def test_non_object_json_fails_open(self):
        assert parse_model_map('["a", "b"]') == {}

    def test_drops_non_string_and_blank_entries(self):
        raw = json.dumps({"ok": "val", "num": 3, "blank": "", "  ": "x"})
        assert parse_model_map(raw) == {"ok": "val"}


class TestResolveModelId:
    def test_exact_match_wins(self):
        assert resolve_model_id("claude-opus-4-7", ICA_MAP) == "claude-opus-4-7"
        assert resolve_model_id("claude-opus-4-8", ICA_MAP) == "claude-opus-4-8"

    def test_dated_id_strips_via_substring(self):
        assert resolve_model_id("claude-haiku-4-5-20251001", ICA_MAP) == "claude-haiku-4-5"
        assert resolve_model_id("claude-opus-4-8-20250101", ICA_MAP) == "claude-opus-4-8"

    def test_legacy_sonnet_maps_to_sonnet_5(self):
        assert resolve_model_id("claude-3-5-sonnet-20241022", ICA_MAP) == "claude-sonnet-5"
        assert resolve_model_id("claude-sonnet-4-5", ICA_MAP) == "claude-sonnet-5"

    def test_longest_key_wins_over_generic_opus(self):
        # "opus-4-7" (len 8) must beat "opus" (len 4).
        assert resolve_model_id("claude-opus-4-7-20250101", ICA_MAP) == "claude-opus-4-7"

    def test_generic_opus_falls_back_to_default(self):
        assert resolve_model_id("claude-opus-4-6", ICA_MAP) == "claude-opus-4-8"
        assert resolve_model_id("claude-3-opus-20240229", ICA_MAP) == "claude-opus-4-8"

    def test_case_insensitive_substring(self):
        assert resolve_model_id("Claude-HAIKU-4-5", ICA_MAP) == "claude-haiku-4-5"

    def test_non_matching_passthrough(self):
        assert resolve_model_id("gpt-4o", ICA_MAP) == "gpt-4o"
        assert resolve_model_id("gemini-3.5-flash", ICA_MAP) == "gemini-3.5-flash"

    def test_empty_map_is_noop(self):
        assert resolve_model_id("claude-opus-4-8", {}) == "claude-opus-4-8"
        assert resolve_model_id("claude-opus-4-8", None) == "claude-opus-4-8"

    def test_non_string_model_returned_unchanged(self):
        assert resolve_model_id(None, ICA_MAP) is None  # type: ignore[arg-type]
        assert resolve_model_id("", ICA_MAP) == ""


class TestProxyConfigWiring:
    """HEADROOM_MODEL_MAP env → ProxyConfig.model_map via the env builder."""

    def test_env_json_string_populates_config(self, monkeypatch):
        pytest.importorskip("fastapi")
        from headroom.proxy import server

        builder = getattr(server, "_proxy_config_from_env", None)
        if builder is None:
            pytest.skip("no _proxy_config_from_env builder in this version")
        monkeypatch.setenv("HEADROOM_MODEL_MAP", json.dumps(ICA_MAP))
        config = builder()
        assert config.model_map == ICA_MAP

    def test_env_file_path_populates_config(self, monkeypatch, tmp_path):
        pytest.importorskip("fastapi")
        from headroom.proxy import server

        builder = getattr(server, "_proxy_config_from_env", None)
        if builder is None:
            pytest.skip("no _proxy_config_from_env builder in this version")
        p = tmp_path / "map.json"
        p.write_text(json.dumps({"haiku": "claude-haiku-4-5"}), encoding="utf-8")
        monkeypatch.setenv("HEADROOM_MODEL_MAP", str(p))
        config = builder()
        assert config.model_map == {"haiku": "claude-haiku-4-5"}

    def test_unset_env_is_empty_map(self, monkeypatch):
        pytest.importorskip("fastapi")
        from headroom.proxy import server

        builder = getattr(server, "_proxy_config_from_env", None)
        if builder is None:
            pytest.skip("no _proxy_config_from_env builder in this version")
        monkeypatch.delenv("HEADROOM_MODEL_MAP", raising=False)
        config = builder()
        assert config.model_map == {}


class TestModelsAdvertising:
    """GET /v1/models advertises the model_map targets in Anthropic format."""

    def _client(self, model_map):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from headroom.proxy.server import ProxyConfig, create_app

        config = ProxyConfig(
            cache_enabled=False,
            rate_limit_enabled=False,
            log_requests=False,
            model_map=model_map,
        )
        return TestClient(create_app(config))

    def test_lists_distinct_map_targets_in_anthropic_format(self):
        with self._client(ICA_MAP) as client:
            resp = client.get("/v1/models")
            assert resp.status_code == 200
            body = resp.json()
            ids = [m["id"] for m in body["data"]]
            # Distinct upstream targets (dedup of map values), sorted.
            assert ids == [
                "claude-haiku-4-5",
                "claude-opus-4-7",
                "claude-opus-4-8",
                "claude-sonnet-5",
            ]
            first = body["data"][0]
            assert first["type"] == "model"
            assert "display_name" in first and "created_at" in first
            assert body["has_more"] is False

    def test_get_model_returns_advertised_entry(self):
        with self._client(ICA_MAP) as client:
            resp = client.get("/v1/models/claude-opus-4-7")
            assert resp.status_code == 200
            body = resp.json()
            assert body["id"] == "claude-opus-4-7"
            assert body["type"] == "model"


class TestRouterDetection:
    """is_router_model detects the special virtual router aliases."""

    def test_detects_router_aliases(self):
        from headroom.proxy.model_routing import is_router_model

        assert is_router_model("router") is True
        assert is_router_model("Router") is True  # Case-insensitive
        assert is_router_model("ROUTER") is True
        assert is_router_model("claude-router-auto") is True
        assert is_router_model("Claude-Router-Auto") is True
        assert is_router_model("auto") is True  # Also a router alias
        assert is_router_model("claude-opus-4-8") is False
        assert is_router_model("gpt-4") is False
        assert is_router_model(None) is False
        assert is_router_model("") is False


class TestRouterHeuristic:
    """Heuristic classification without calling a model."""

    def test_trivial_request(self):
        from headroom.proxy.model_router import classify_request_heuristic

        body = {
            "messages": [{"role": "user", "content": "hello"}],
        }
        decision = classify_request_heuristic(body)
        assert decision is not None
        assert decision.model_id == "claude-haiku-4-5"
        assert decision.thinking_level is None
        assert decision.confidence > 0.8

    def test_complex_architectural_request(self):
        from headroom.proxy.model_router import classify_request_heuristic

        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "design an architecture for a microservices platform with database sharding "
                    * 5,  # Long + architecture keyword
                }
            ],
        }
        decision = classify_request_heuristic(body)
        assert decision is not None
        assert decision.model_id == "claude-opus-4-8"
        assert decision.confidence > 0.8

    def test_code_with_iteration_uses_sonnet(self):
        from headroom.proxy.model_router import classify_request_heuristic

        body = {
            "messages": [
                {"role": "user", "content": "```python\ncode here\n```"},
                {"role": "assistant", "content": "response"},
                {"role": "user", "content": "fix it"},
            ],
        }
        decision = classify_request_heuristic(body)
        assert decision is not None
        assert decision.model_id == "claude-sonnet-5"

    def test_classifier_respects_never_downgrade(self):
        from headroom.proxy.model_router import classify_request_heuristic

        # Any classification (even heuristic) should never downgrade to Haiku
        # if there's any complexity signal
        body = {
            "messages": [
                {"role": "user", "content": "some medium-length prompt\n```python\nx = 1\n```"},
            ],
        }
        decision = classify_request_heuristic(body)
        # Must be Sonnet or higher (never Haiku) because of code signal
        if decision is not None:
            assert decision.model_id in ("claude-sonnet-5", "claude-opus-4-8")


class TestRouterThinkingParam:
    """Router decision's thinking_param() method."""

    def test_thinking_param_none_when_disabled(self):
        from headroom.proxy.model_router import RouterDecision

        decision = RouterDecision(
            model_id="claude-haiku-4-5",
            thinking_level=None,
            reasoning="trivial",
            confidence=0.95,
        )
        assert decision.thinking_param() is None

    def test_thinking_param_disabled_for_ica(self):
        from headroom.proxy.model_router import RouterDecision

        # ICA doesn't support extended thinking, so all requests return None
        decision = RouterDecision(
            model_id="claude-opus-4-8",
            thinking_level="high",
            reasoning="complex",
            confidence=0.85,
        )
        param = decision.thinking_param()
        # ICA backend doesn't support thinking
        assert param is None
