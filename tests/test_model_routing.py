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
