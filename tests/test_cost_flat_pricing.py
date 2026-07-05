"""Tests for flat-rate pricing override, output-token cost, and persistence.

The flat-rate override (``price_input_per_1m`` / ``price_output_per_1m``) lets
credit-point endpoints (e.g. IBM ICA), whose model names aren't in LiteLLM's
pricing DB, still produce a cost figure. It also makes the test independent of
LiteLLM's database contents.
"""

from __future__ import annotations

from headroom.proxy.cost import CostTracker

# Arbitrary model name that is NOT in LiteLLM's DB — proves the override does
# not depend on a database lookup.
MODEL = "ica/some-credit-point-model"


def _flat_tracker() -> CostTracker:
    return CostTracker(price_input_per_1m=5.0, price_output_per_1m=25.0)


def test_flat_rate_input_and_output_cost():
    ct = _flat_tracker()
    ct.record_tokens(
        MODEL,
        tokens_saved=0,
        tokens_sent=1_000_000,
        uncached_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    stats = ct.stats()

    # 1M input @ $5 + 1M output @ $25.
    assert stats["total_output_tokens"] == 1_000_000
    assert stats["total_input_cost_usd"] == 5.0
    assert stats["total_output_cost_usd"] == 25.0
    assert stats["total_cost_usd"] == 30.0
    # cost_with_headroom_usd now includes output (the headline "what you paid").
    assert stats["cost_with_headroom_usd"] == 30.0


def test_flat_rate_cache_tokens_priced_at_input_rate():
    ct = _flat_tracker()
    ct.record_tokens(
        MODEL,
        tokens_saved=0,
        tokens_sent=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        uncached_tokens=0,
        output_tokens=0,
    )
    stats = ct.stats()
    # 2M input-side tokens (cache read + write) @ $5/1M = $10, no output.
    assert stats["total_input_cost_usd"] == 10.0
    assert stats["total_output_cost_usd"] == 0.0


def test_output_tokens_tracked_per_model():
    ct = _flat_tracker()
    ct.record_tokens(MODEL, 0, 100, uncached_tokens=100, output_tokens=42)
    ct.record_tokens(MODEL, 0, 100, uncached_tokens=100, output_tokens=8)
    stats = ct.stats()
    assert stats["total_output_tokens"] == 50
    assert stats["per_model"][MODEL]["output_tokens"] == 50


def test_no_override_falls_back_to_litellm_path():
    # Without the override, flat pricing must not kick in (price helpers return
    # None or LiteLLM values, never the flat rate).
    ct = CostTracker()
    assert ct._price_input_per_token is None
    assert ct._price_output_per_token is None


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "proxy_cost.json"
    ct = _flat_tracker()
    ct.record_tokens(
        MODEL,
        tokens_saved=10,
        tokens_sent=1000,
        uncached_tokens=1000,
        output_tokens=250,
    )
    before = ct.stats()
    ct.save(path)
    assert path.exists()

    restored = _flat_tracker()
    assert restored.load(path) is True
    after = restored.stats()

    assert after["total_output_tokens"] == before["total_output_tokens"]
    assert after["total_cost_usd"] == before["total_cost_usd"]
    assert after["per_model"][MODEL]["output_tokens"] == 250
    # Budget-enforcement deque also restored.
    assert len(restored._costs) == len(ct._costs)


def test_load_missing_file_is_noop(tmp_path):
    ct = _flat_tracker()
    assert ct.load(tmp_path / "does_not_exist.json") is False


def test_load_corrupt_file_is_tolerated(tmp_path):
    path = tmp_path / "proxy_cost.json"
    path.write_text("{ not valid json", encoding="utf-8")
    ct = _flat_tracker()
    assert ct.load(path) is False
    # Tracker stays usable/empty.
    assert ct.stats()["total_output_tokens"] == 0
