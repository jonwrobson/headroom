#!/usr/bin/env bash
#
# Launch the Headroom proxy in front of the IBM ICA (LiteLLM-backed) endpoint
# with multi-token spend-limit rotation enabled.
#
# The proxy holds a LIST of ANTHROPIC_AUTH_TOKEN values (one per line in the
# token file). On a `budget_exceeded` error from the upstream it rotates to the
# next token automatically; only when every token is exhausted does a request
# fail (HTTP 429 `all_tokens_exhausted`).
#
# Claude Code points at THIS proxy (http://localhost:PORT) — see
# scripts/setup-claude-ica.md for the settings.json block.
#
# Usage:
#   scripts/run-ica-proxy.sh
#
# Override defaults via env:
#   ICA_PROXY_PORT       proxy listen port              (default: 8787)
#   ICA_UPSTREAM_URL     upstream Anthropic-compatible  (default: IBM ICA)
#   ICA_TOKEN_FILE       token list, one per line       (default: ~/.headroom/ica_tokens.txt)
#   ICA_TOKEN_COOLDOWN   seconds to skip a spent token  (default: 3600)

set -euo pipefail

PORT="${ICA_PROXY_PORT:-8787}"
UPSTREAM_URL="${ICA_UPSTREAM_URL:-https://api.nextgen-beta.ica.ibm.com/ica}"
TOKEN_FILE="${ICA_TOKEN_FILE:-$HOME/.headroom/ica_tokens.txt}"
COOLDOWN="${ICA_TOKEN_COOLDOWN:-3600}"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "error: token file not found: $TOKEN_FILE" >&2
  echo "Create it with one ANTHROPIC_AUTH_TOKEN per line. See scripts/ica_tokens.txt.example" >&2
  exit 1
fi

n_tokens="$(grep -cvE '^\s*(#|$)' "$TOKEN_FILE" || true)"
echo "Headroom → IBM ICA proxy"
echo "  upstream : $UPSTREAM_URL"
echo "  listen   : http://localhost:$PORT"
echo "  tokens   : $n_tokens (from $TOKEN_FILE, cooldown ${COOLDOWN}s)"
echo "  telemetry: off"
echo

# HEADROOM_TELEMETRY=off keeps the run quiet/offline. The proxy forwards to the
# IBM URL and overrides the Authorization header from the token file per request.
exec env HEADROOM_TELEMETRY=off headroom proxy \
  --port "$PORT" \
  --anthropic-api-url "$UPSTREAM_URL" \
  --auth-token-file "$TOKEN_FILE" \
  --auth-token-cooldown "$COOLDOWN" \
  "$@"
