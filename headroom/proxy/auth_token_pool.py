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

Rotation state is **sticky with smart cooldown**: an exhausted token is skipped
until either the configured cooldown expires OR the next budget renewal time
(Monday 1 AM UTC for IBM ICA). This ensures tokens become available immediately
after budget renewal, preventing unnecessary downtime.

State is per-process. Under a multi-worker deployment each worker keeps its own
view, which is safe — at worst a freshly-exhausted token costs one extra probe
per worker before that worker learns it is dead.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("headroom.proxy")


@dataclass
class TokenInfo:
    """Token with optional ID for health reporting."""

    id: str
    token: str

#: Default cooldown applied to an exhausted token before it is retried.
DEFAULT_COOLDOWN_S = 3600

#: Default ``error.type`` values that mark a spend/budget exhaustion.
DEFAULT_SPEND_LIMIT_ERROR_TYPES = frozenset({"budget_exceeded"})

#: Default case-insensitive substring fallback matched against the error
#: message when the structured ``error.type`` is absent or unrecognized.
DEFAULT_SPEND_LIMIT_MATCH = "budget has been exceeded"


def load_tokens_from_file(path: str | Path) -> list[TokenInfo]:
    """Load auth tokens from a file.

    Supports two formats:
    - CSV format: ID,TOKEN (e.g., "prod-token-1,sk-ant-api03-xxx")
    - Plain format: TOKEN (ID auto-generated as token suffix)

    Blank lines and lines beginning with ``#`` are ignored. Order is preserved
    and duplicates are dropped (first occurrence wins), so the file doubles as
    the rotation priority order.
    """
    resolved = Path(path).expanduser()
    tokens: list[TokenInfo] = []
    seen: set[str] = set()
    for raw in resolved.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # Parse CSV or plain format
        if "," in line:
            parts = line.split(",", 1)
            token_id = parts[0].strip()
            token = parts[1].strip()
        else:
            token = line
            token_id = f"...{token[-4:]}" if len(token) >= 4 else token

        if token in seen:
            continue
        seen.add(token)
        tokens.append(TokenInfo(id=token_id, token=token))
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


def next_monday_1am_utc() -> float:
    """Calculate Unix timestamp of next Monday 1:00 AM UTC.

    This is when IBM ICA budgets renew. If it's currently Monday before 1 AM,
    returns today at 1 AM. Otherwise returns next Monday at 1 AM.

    Returns:
        Unix timestamp (seconds since epoch) of next budget renewal
    """
    now = datetime.now(timezone.utc)

    # Find next Monday
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday == 0 and now.hour >= 1:
        # It's Monday but after 1 AM, so next Monday
        days_until_monday = 7

    next_monday = now + timedelta(days=days_until_monday)

    # Set to 1:00 AM UTC
    renewal_time = next_monday.replace(hour=1, minute=0, second=0, microsecond=0)

    return renewal_time.timestamp()


class TokenPool:
    """An ordered pool of upstream auth tokens with sticky-cooldown rotation."""

    def __init__(
        self, tokens: list[TokenInfo] | list[str], cooldown_s: float = DEFAULT_COOLDOWN_S
    ) -> None:
        # Support both TokenInfo and plain string tokens for backward compatibility
        self._tokens: list[TokenInfo] = []
        seen: set[str] = set()

        for tok in tokens:
            if isinstance(tok, TokenInfo):
                token_info = tok
            elif isinstance(tok, str) and tok:
                # Backward compatibility: plain string token
                token_info = TokenInfo(
                    id=f"...{tok[-4:]}" if len(tok) >= 4 else tok, token=tok
                )
            else:
                continue

            if token_info.token and token_info.token not in seen:
                seen.add(token_info.token)
                self._tokens.append(token_info)

        self._cooldown_s = cooldown_s
        # token string -> monotonic timestamp at which the cooldown expires
        self._exhausted: dict[str, float] = {}
        # token string -> cumulative tokens (input+output) served, for
        # least-consumed load balancing across the pool.
        self._usage: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path, cooldown_s: float = DEFAULT_COOLDOWN_S) -> TokenPool:
        return cls(load_tokens_from_file(path), cooldown_s=cooldown_s)

    def __len__(self) -> int:
        return len(self._tokens)

    def _active(self, token: str, now: float) -> bool:
        """Whether ``token`` is usable now; clears an expired cooldown.

        A token becomes active again when the cooldown has elapsed.
        The cooldown is set to the EARLIER of the configured cooldown duration
        or the time until budget renewal (Monday 1 AM UTC).
        """
        expiry = self._exhausted.get(token)
        if expiry is None:
            return True

        # Check if cooldown has expired (using monotonic time)
        if now >= expiry:
            del self._exhausted[token]
            logger.info(
                "auth-token pool: token …%s cooldown expired, now active",
                token[-4:] if len(token) >= 4 else token,
            )
            return True

        return False

    def current(self) -> str | None:
        """Least-consumed active token, or ``None`` if all are exhausted.

        Load is spread across the pool: among tokens not currently in cooldown,
        the one with the lowest cumulative usage (see :meth:`record_usage`) is
        chosen. File order breaks ties, so at startup — when all usage is 0 —
        this still begins at the first token, and the choice is deterministic.
        """
        now = time.monotonic()
        with self._lock:
            active = [t for t in self._tokens if self._active(t.token, now)]
            if not active:
                return None
            # min() is stable: equal-usage tokens keep file order.
            return min(active, key=lambda t: self._usage.get(t.token, 0)).token

    def record_usage(self, token: str, tokens: int) -> None:
        """Credit ``token`` with ``tokens`` used (input+output), for balancing.

        No-op for a token not in the pool (defensive) or a non-positive count.
        """
        if tokens <= 0:
            return
        with self._lock:
            if not any(t.token == token for t in self._tokens):
                return
            self._usage[token] = self._usage.get(token, 0) + tokens

    def usage_snapshot(self) -> dict[str, int]:
        """Map of token id -> cumulative usage, for /health and tests."""
        with self._lock:
            return {t.id: self._usage.get(t.token, 0) for t in self._tokens}

    def mark_exhausted(self, token: str) -> None:
        """Mark ``token`` exhausted; it is skipped until cooldown or budget renewal.

        The token will become active again at the EARLIER of:
        1. cooldown_s seconds from now
        2. Next Monday 1:00 AM UTC (budget renewal time)
        """
        with self._lock:
            cooldown_expiry = time.monotonic() + self._cooldown_s
            renewal_timestamp = next_monday_1am_utc()
            renewal_seconds = renewal_timestamp - time.time()

            # Use the earlier of cooldown or renewal time
            if renewal_seconds < self._cooldown_s:
                self._exhausted[token] = time.monotonic() + renewal_seconds
                logger.warning(
                    "auth-token pool: token …%s marked spend-exhausted; "
                    "will retry at budget renewal (Monday 1 AM UTC, %.1f hours) "
                    "(%d/%d tokens active)",
                    token[-4:] if len(token) >= 4 else token,
                    renewal_seconds / 3600,
                    self._active_count_locked(time.monotonic()),
                    len(self._tokens),
                )
            else:
                self._exhausted[token] = cooldown_expiry
                logger.warning(
                    "auth-token pool: token …%s marked spend-exhausted; "
                    "cooling down %.0fs (%.1f hours) "
                    "(%d/%d tokens active)",
                    token[-4:] if len(token) >= 4 else token,
                    self._cooldown_s,
                    self._cooldown_s / 3600,
                    self._active_count_locked(time.monotonic()),
                    len(self._tokens),
                )

    def all_exhausted(self) -> bool:
        return self.current() is None

    def _active_count_locked(self, now: float) -> int:
        return sum(1 for t in self._tokens if self._exhausted.get(t.token, 0.0) <= now)

    def active_count(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(
                1
                for t in self._tokens
                if (exp := self._exhausted.get(t.token)) is None or now >= exp
            )

    async def check_token_health(
        self, token_info: TokenInfo, api_url: str
    ) -> dict[str, Any]:
        """Test a single token with minimal API call.

        Args:
            token_info: Token information to test
            api_url: Base API URL (e.g., https://api.nextgen-beta.ica.ibm.com/ica)

        Returns:
            Dict with keys: id, status, healthy, and optionally message
        """
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{api_url}/v1/messages",
                    headers={
                        "authorization": f"Bearer {token_info.token}",
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-opus-4-8",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "test"}],
                    },
                )

                # Success or budget error = token works
                if response.status_code in (200, 400):
                    body = response.json()
                    if response.status_code == 400:
                        error = body.get("error", {})
                        error_type = error.get("type", "unknown")
                        error_message = error.get("message", "Unknown error")
                        
                        if error_type == "budget_exceeded":
                            return {
                                "id": token_info.id,
                                "status": "budget_exceeded",
                                "error_type": error_type,
                                "message": error_message,
                                "healthy": False,
                            }
                        else:
                            # Other 400 errors (e.g., invalid_request_error, permission_error)
                            return {
                                "id": token_info.id,
                                "status": "error",
                                "error_type": error_type,
                                "message": error_message,
                                "healthy": False,
                            }
                    return {"id": token_info.id, "status": "active", "healthy": True}
                elif response.status_code == 401:
                    try:
                        body = response.json()
                        error = body.get("error", {})
                        error_message = error.get("message", "Invalid or expired token")
                    except Exception:
                        error_message = "Invalid or expired token"
                    
                    return {
                        "id": token_info.id,
                        "status": "unauthorized",
                        "error_type": "authentication_error",
                        "message": error_message,
                        "healthy": False,
                    }
                else:
                    # Try to get error details from response body
                    try:
                        body = response.json()
                        error = body.get("error", {})
                        error_type = error.get("type", "unknown")
                        error_message = error.get("message", f"HTTP {response.status_code}")
                    except Exception:
                        error_type = "http_error"
                        error_message = f"HTTP {response.status_code}"
                    
                    return {
                        "id": token_info.id,
                        "status": "error",
                        "error_type": error_type,
                        "message": error_message,
                        "http_status": response.status_code,
                        "healthy": False,
                    }
        except Exception as e:
            error_message = str(e) if str(e) else "Unknown error during health check"
            return {
                "id": token_info.id,
                "status": "error",
                "error_type": "exception",
                "message": error_message,
                "healthy": False,
            }

    async def check_all_tokens(self, api_url: str) -> list[dict[str, Any]]:
        """Check health of all tokens in parallel and mark exhausted ones.

        Args:
            api_url: Base API URL for health checks

        Returns:
            List of health check results, one per token
        """
        import asyncio

        tasks = [self.check_token_health(token_info, api_url) for token_info in self._tokens]
        results = await asyncio.gather(*tasks)
        
        # Automatically mark budget-exceeded tokens as exhausted
        for result, token_info in zip(results, self._tokens):
            if result.get("status") == "budget_exceeded":
                self.mark_exhausted(token_info.token)
                logger.info(
                    "auth-token pool: token %s marked exhausted due to budget_exceeded in health check",
                    token_info.id,
                )
        
        return results

    def reset(self) -> None:
        """Clear all cooldowns (test/debug helper)."""
        with self._lock:
            self._exhausted.clear()
