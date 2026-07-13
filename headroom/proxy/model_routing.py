"""Model-name routing for the proxy.

Resolves an incoming request ``model`` id to an upstream model id using a
configurable map. This exists because the ICA (LiteLLM/OpenAI-compatible)
backend is reached via the direct Anthropic passthrough, which forwards the
``model`` verbatim — so model ids the client sends that are not exactly in the
upstream catalog (dated ids like ``claude-haiku-4-5-20251001`` or legacy ids
like ``claude-3-5-sonnet-20241022``) never resolve and land in the ``unknown``
cost bucket. A map lets each incoming id route to a real upstream model.

Resolution order in :func:`resolve_model_id`:
  1. exact key match wins;
  2. otherwise the *longest* map key that is a case-insensitive substring of the
     model id (so ``claude-opus-4-7-20250101`` matches key ``opus-4-7`` over
     ``opus``, and ``claude-3-5-sonnet-...`` matches ``sonnet``);
  3. otherwise the model is returned unchanged (passthrough).

An empty/absent map is always a no-op passthrough.

This module also detects the special "router" virtual model id, which signals
the proxy to analyze each request and intelligently route it to the best
(cheapest) model capable of handling it well (see :mod:`headroom.proxy.model_router`).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

__all__ = ["parse_model_map", "resolve_model_id", "is_router_model"]

# Virtual model aliases for intelligent routing.
# When the client sends any of these (e.g., `/model router`), the proxy
# analyzes the request and routes it dynamically.
_ROUTER_ALIASES = frozenset({"router", "claude-router-auto", "auto"})


def parse_model_map(raw: str | None) -> dict[str, str]:
    """Parse a model map from a JSON object string or a path to a JSON file.

    Mirrors the ``HEADROOM_MODEL_LIMITS`` convention (see
    ``headroom/providers/anthropic.py``): the value may be an inline JSON object
    or a filesystem path to one. Returns an empty dict on empty input or on any
    parse error (fail-open — routing is a no-op rather than breaking startup).
    """
    if not raw or not raw.strip():
        return {}
    try:
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8") as f:
                loaded = json.load(f)
        else:
            loaded = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load HEADROOM_MODEL_MAP: %s", e)
        return {}

    if not isinstance(loaded, dict):
        logger.warning("HEADROOM_MODEL_MAP must be a JSON object, got %s", type(loaded).__name__)
        return {}

    # Coerce to str->str; drop any non-string/blank entries.
    model_map: dict[str, str] = {}
    for key, value in loaded.items():
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
            model_map[key] = value
    return model_map


def is_router_model(model: str | None) -> bool:
    """Check if a model id is the special "router" virtual model.

    When this returns True, the proxy should analyze the request and
    dynamically route it to the best model, rather than using a static mapping.
    """
    if not model or not isinstance(model, str):
        return False
    return model.strip().lower() in _ROUTER_ALIASES


def resolve_model_id(model: str, model_map: dict[str, str] | None) -> str:
    """Resolve ``model`` against ``model_map`` (see module docstring for order).

    Returns ``model`` unchanged when the map is empty, the model is not a
    string, or nothing matches.

    Note: does not handle the special "router" virtual model — that is
    detected by :func:`is_router_model` and routed separately.
    """
    if not model_map or not isinstance(model, str) or not model:
        return model

    # 1. Exact match.
    if model in model_map:
        return model_map[model]

    # 2. Longest case-insensitive substring key.
    lowered = model.lower()
    best_key: str | None = None
    for key in model_map:
        if key.lower() in lowered and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is not None:
        return model_map[best_key]

    # 3. Passthrough.
    return model
