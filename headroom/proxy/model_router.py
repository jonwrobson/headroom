"""Intelligent cost-aware model router for proxy requests.

When a user selects the "router" virtual model, the proxy analyzes each
request's complexity and routes it to the cheapest model capable of handling
it well, while biasing toward escalation (never silently downgrading). Uses
fast heuristics first; escalates ambiguous cases to Opus for final judgment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RouterDecision:
    """Decision from router: which model to use, thinking level, reasoning."""

    model_id: str  # e.g., "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"
    thinking_level: str | None  # "low", "high", "max", or None (no thinking)
    reasoning: str  # why this choice was made
    confidence: float  # 0.0-1.0, higher = more certain

    def thinking_param(self) -> dict[str, Any] | None:
        """Return the Anthropic thinking parameter for the request body, or None.

        NOTE: ICA (IBM LiteLLM backend) does not support extended thinking,
        so we return None regardless of thinking_level. This could be made
        conditional in the future (e.g., check proxy backend type).
        """
        # ICA doesn't support thinking; return None
        return None


def _estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough estimate of tokens in a message list (4 chars ≈ 1 token)."""
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if "text" in block:
                            total += len(str(block["text"])) // 4
                        elif "source" in block:
                            total += len(str(block.get("source", {}))) // 4
    return max(total, 1)


def classify_request_heuristic(body: dict[str, Any]) -> RouterDecision | None:
    """Fast heuristic classification without calling a model.

    Returns a RouterDecision with high confidence for simple/complex cases,
    or None to signal "ambiguous, escalate to Opus for final judgment."

    Bias: never guess low (simple) on ambiguity — if we're unsure,
    we escalate to Opus rather than risking silent downgrade.
    """
    messages = body.get("messages", [])
    if not messages:
        return RouterDecision(
            model_id="claude-haiku-4-5",
            thinking_level=None,
            reasoning="Empty messages — trivial request",
            confidence=0.95,
        )

    total_tokens = _estimate_message_tokens(messages)
    total_text = json.dumps(body).lower()

    # Indicators of complexity
    has_code = "```" in total_text or "<code>" in total_text
    has_tool_use = any(
        isinstance(m.get("content"), list) and any(
            b.get("type") == "tool_use" for b in m["content"] if isinstance(b, dict)
        )
        for m in messages
        if isinstance(m, dict)
    )
    has_tool_result = any(
        m.get("role") == "user" and isinstance(m.get("content"), list) and any(
            b.get("type") == "tool_result" for b in m["content"] if isinstance(b, dict)
        )
        for m in messages
        if isinstance(m, dict)
    )
    is_multi_turn = len(messages) > 3

    architecture_keywords = [
        "architect",
        "design",
        "refactor",
        "migrate",
        "rewrite",
        "structure",
        "pattern",
        "framework",
        "strategic",
    ]
    has_architecture_signal = any(
        kw in total_text for kw in architecture_keywords
    )

    debug_keywords = ["debug", "error", "traceback", "stack trace", "issue", "bug"]
    has_debug_signal = any(kw in total_text for kw in debug_keywords)

    # Check for architectural/complex indicators first (these are high-confidence)
    if has_architecture_signal or (is_multi_turn and has_code and has_tool_use) or total_tokens > 2000:
        # Complex: architectural keywords, multi-step code work, or very long
        return RouterDecision(
            model_id="claude-opus-4-8",
            thinking_level=None,  # ICA doesn't support thinking
            reasoning=f"Complex request ({total_tokens} tokens, code={has_code}, "
            f"tool_use={has_tool_use}, architecture={has_architecture_signal})",
            confidence=0.85,
        )

    # Simple classification for short, trivial requests
    if total_tokens < 200 and not (has_code or is_multi_turn):
        # Trivial: short, no code, single turn
        return RouterDecision(
            model_id="claude-haiku-4-5",
            thinking_level=None,
            reasoning=f"Trivial request ({total_tokens} tokens, no code)",
            confidence=0.90,
        )

    if has_code and (has_tool_use or has_tool_result or is_multi_turn):
        # Medium: code work with iteration, but not architectural scale
        return RouterDecision(
            model_id="claude-sonnet-5",
            thinking_level="low" if has_debug_signal else None,
            reasoning=f"Medium complexity ({total_tokens} tokens, code+iteration)",
            confidence=0.80,
        )

    if has_code or is_multi_turn:
        # Medium-ish: either code-heavy single turn, or non-code multi-turn
        return RouterDecision(
            model_id="claude-sonnet-5",
            thinking_level=None,
            reasoning=f"Medium complexity ({total_tokens} tokens, code={has_code}, "
            f"multi_turn={is_multi_turn})",
            confidence=0.75,
        )

    # Ambiguous mid-range: can't decide confidently
    # Return None so we escalate to Opus for final judgment
    return None


async def classify_request_with_opus(
    proxy: Any, body: dict[str, Any]
) -> RouterDecision:
    """Ask Opus to classify the request when heuristic is ambiguous.

    Sends a small, cheap non-streaming call to Opus with a structured
    classification task. Fails safe: on any error, returns Opus (expensive
    but correct) rather than guessing low.
    """
    messages = body.get("messages", [])
    system_prompt = (
        "You are a request router that picks the right Claude model for a task. "
        "Analyze the user's request and pick the best model tier:\n"
        "- 'haiku': trivial, straightforward, no reasoning needed\n"
        "- 'sonnet': moderate, some reasoning or coding\n"
        "- 'opus': complex, multi-step, architecture, advanced reasoning\n\n"
        "Also pick a thinking level: 'low', 'high', 'max', or None.\n"
        "Respond ONLY with valid JSON: {\"model\": \"haiku|sonnet|opus\", "
        "\"thinking_level\": \"low\"|\"high\"|\"max\"|null, \"reasoning\": \"...\"}"
    )

    # Summarize the request so we don't send massive tokens to the classifier
    summarized_messages = []
    for msg in messages[-3:]:  # Last 3 messages max
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str):
                summarized_messages.append(
                    {
                        "role": msg.get("role", "user"),
                        "content": content[:500],  # Truncate to 500 chars
                    }
                )
            else:
                summarized_messages.append(msg)

    classify_body = {
        "model": "claude-opus-4-8",
        "max_tokens": 200,  # Tiny, just a JSON response
        "system": system_prompt,
        "messages": summarized_messages or [{"role": "user", "content": "..."}],
    }

    try:
        # Use proxy's http_client directly, minimal one-off call
        if not hasattr(proxy, "http_client") or proxy.http_client is None:
            logger.warning("No http_client on proxy, falling back to Opus")
            return RouterDecision(
                model_id="claude-opus-4-8",
                thinking_level="low",
                reasoning="Router escalation (http_client unavailable)",
                confidence=1.0,
            )

        # Build headers with auth token if pool available
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if hasattr(proxy, "auth_token_pool") and proxy.auth_token_pool is not None:
            token = proxy.auth_token_pool.current()
            if token:
                headers["authorization"] = f"Bearer {token}"

        api_url = getattr(
            proxy, "ANTHROPIC_API_URL", "https://api.nextgen-beta.ica.ibm.com/ica"
        )
        url = f"{api_url}/v1/messages"

        response = await asyncio.wait_for(
            proxy.http_client.post(
                url,
                json=classify_body,
                headers=headers,
                timeout=5.0,
            ),
            timeout=6.0,
        )

        if response.status_code != 200:
            logger.warning(
                f"Opus classification failed (status {response.status_code}), "
                f"falling back to Opus"
            )
            return RouterDecision(
                model_id="claude-opus-4-8",
                thinking_level="low",
                reasoning=f"Router escalation (status {response.status_code})",
                confidence=1.0,
            )

        data = response.json()
        content = data.get("content", [])
        if not isinstance(content, list) or not content:
            raise ValueError("Unexpected response format (no content)")

        # Extract text from response
        text = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break

        if not text:
            raise ValueError("No text in response")

        # Parse JSON from response
        result = json.loads(text)
        model_choice = result.get("model", "").lower()
        thinking_level = result.get("thinking_level")
        reasoning = result.get("reasoning", "Opus classification")

        # Validate and normalize
        if model_choice not in ("haiku", "sonnet", "opus"):
            raise ValueError(f"Invalid model choice: {model_choice}")

        # Map tier name to full model ID
        tier_to_id = {
            "haiku": "claude-haiku-4-5",
            "sonnet": "claude-sonnet-5",
            "opus": "claude-opus-4-8",
        }
        model_id = tier_to_id[model_choice]

        return RouterDecision(
            model_id=model_id,
            thinking_level=thinking_level,
            reasoning=reasoning,
            confidence=0.95,
        )

    except Exception as e:
        logger.warning(
            f"Opus classification call failed ({type(e).__name__}: {e}), "
            f"falling back to Opus"
        )
        return RouterDecision(
            model_id="claude-opus-4-8",
            thinking_level="low",
            reasoning=f"Router escalation (error: {type(e).__name__})",
            confidence=1.0,
        )


async def route_request(proxy: Any, body: dict[str, Any]) -> RouterDecision:
    """Route a request: try heuristic, escalate to Opus if unsure.

    This is the main entry point. Returns a RouterDecision with the chosen
    model and thinking level.
    """
    # Try fast heuristic first
    decision = classify_request_heuristic(body)
    if decision is not None:
        logger.debug(
            f"Router heuristic: {decision.model_id} "
            f"(confidence={decision.confidence}, {decision.reasoning})"
        )
        return decision

    # Heuristic was ambiguous, escalate to Opus
    logger.debug("Router heuristic ambiguous, escalating to Opus for classification")
    decision = await classify_request_with_opus(proxy, body)
    logger.debug(
        f"Router Opus classification: {decision.model_id} "
        f"(thinking={decision.thinking_level}, {decision.reasoning})"
    )
    return decision
