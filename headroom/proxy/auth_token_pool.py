"""Auth-token pool with spend-limit rotation.

The Headroom proxy can hold a LIST of upstream auth tokens (e.g. multiple
``ANTHROPIC_AUTH_TOKEN`` values, each tied to its own upstream spend budget).
When the upstream rejects a request because the current token's budget is
exhausted, the proxy rotates to the next token and retries the same request
transparently. Only when *every* token is exhausted does the client see an
error.

The canonical exhaustion signal (observed live against the IBM ICA /
LiteLLM-backed endpoint) is an HTTP 400 with body::

    {"error": {"message": "Budget has been exceeded! Team=... Current cost:
     3212.27, Max budget: 3200.0", "type": "budget_exceeded", ...}}

Detection keys on ``error.type`` (default ``budget_exceeded``) or a substring
match on the message, so ordinary 400s (bad model, malformed body) pass
straight through and never burn the key pool.

Rotation state is **sticky with cooldown**: an exhausted token is skipped for
``cooldown_s`` seconds, so each request starts from the first still-good token
instead of re-probing dead keys on every call. After the cooldown elapses the
token is tried again (one probing round-trip); if it is still over budget it is
simply re-marked.

State is per-process. Under a multi-worker deployment each worker keeps its own
view, which is safe — at worst a freshly-exhausted token costs one extra probe
per worker before that worker learns it is dead.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("headroom.proxy")

#: Default cooldown applied to an exhausted token before it is retried.
DEFAULT_COOLDOWN_S = 3600

#: Default ``error.type`` values that mark a spend/budget exhaustion.
DEFAULT_SPEND_LIMIT_ERROR_TYPES = frozenset({"budget_exceeded"})

#: Default case-insensitive substring fallback matched against the error
#: message when the structured ``error.type`` is absent or unrecognized.
DEFAULT_SPEND_LIMIT_MATCH = "budget has been exceeded"


def load_tokens_from_file(path: str | Path) -> list[str]:
    """Load auth tokens from a file, one per line.

    Blank lines and lines beginning with ``#`` are ignored. Order is preserved
    and duplicates are dropped (first occurrence wins), so the file doubles as
    the rotation priority order.
    """
    resolved = Path(path).expanduser()
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in resolved.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        tokens.append(line)
    return tokens


def is_spend_limit_error(
    status_code: int,
    body_json: Any,
    *,
    error_types: frozenset[str] | set[str] | None = None,
    match: str | None = DEFAULT_SPEND_LIMIT_MATCH,
) -> bool:
    """Return ``True`` if an upstream response signals a spend/budget limit.

    Detection is intentionally narrow so that ordinary client errors (invalid
    model, malformed body — also HTTP 400) are NOT treated as exhaustion and
    are forwarded to the client unchanged.

    Args:
        status_code: Upstream HTTP status (currently unused for the decision —
            the signal is carried in the body — but accepted so callers can
            pass it and future policies can gate on it).
        body_json: Parsed JSON body (dict) or anything else; non-dict inputs
            yield ``False``.
        error_types: ``error.type`` values that count as exhaustion.
        match: Case-insensitive substring matched against ``error.message`` as
            a fallback when the type is missing/unknown. ``None`` disables it.
    """
    types = error_types if error_types is not None else DEFAULT_SPEND_LIMIT_ERROR_TYPES
    try:
        if not isinstance(body_json, dict):
            return False
        err = body_json.get("error")
        if not isinstance(err, dict):
            return False
        err_type = err.get("type")
        if isinstance(err_type, str) and err_type in types:
            return True
        if match:
            message = err.get("message")
            if isinstance(message, str) and match.lower() in message.lower():
                return True
    except Exception:  # pragma: no cover - defensive; never block on detection
        return False
    return False


class TokenPool:
    """An ordered pool of upstream auth tokens with sticky-cooldown rotation."""

    def __init__(self, tokens: list[str], cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        self._tokens: list[str] = []
        for tok in tokens:
            if tok and tok not in seen:
                seen.add(tok)
                self._tokens.append(tok)
        self._cooldown_s = cooldown_s
        # token -> monotonic timestamp at which the cooldown expires
        self._exhausted: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path, cooldown_s: float = DEFAULT_COOLDOWN_S) -> TokenPool:
        return cls(load_tokens_from_file(path), cooldown_s=cooldown_s)

    def __len__(self) -> int:
        return len(self._tokens)

    def _active(self, token: str, now: float) -> bool:
        """Whether ``token`` is usable now; clears an expired cooldown."""
        expiry = self._exhausted.get(token)
        if expiry is None:
            return True
        if now >= expiry:
            # Cooldown elapsed — give the token another chance.
            del self._exhausted[token]
            return True
        return False

    def current(self) -> str | None:
        """First token not currently in cooldown, or ``None`` if all exhausted."""
        now = time.monotonic()
        with self._lock:
            for token in self._tokens:
                if self._active(token, now):
                    return token
            return None

    def mark_exhausted(self, token: str) -> None:
        """Mark ``token`` exhausted; it is skipped until the cooldown elapses."""
        with self._lock:
            self._exhausted[token] = time.monotonic() + self._cooldown_s
            logger.warning(
                "auth-token pool: token …%s marked spend-exhausted; "
                "cooling down %.0fs (%d/%d tokens active)",
                token[-4:] if len(token) >= 4 else token,
                self._cooldown_s,
                self._active_count_locked(time.monotonic()),
                len(self._tokens),
            )

    def all_exhausted(self) -> bool:
        return self.current() is None

    def _active_count_locked(self, now: float) -> int:
        return sum(1 for t in self._tokens if self._exhausted.get(t, 0.0) <= now)

    def active_count(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(
                1
                for t in self._tokens
                if (exp := self._exhausted.get(t)) is None or now >= exp
            )

    def reset(self) -> None:
        """Clear all cooldowns (test/debug helper)."""
        with self._lock:
            self._exhausted.clear()
