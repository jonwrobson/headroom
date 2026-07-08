#!/usr/bin/env bash
#
# Run the Headroom proxy (with auth-token spend-limit rotation) in Docker,
# pointed at the IBM ICA endpoint, reading a file full of API keys.
#
# ── How to provide your API keys ────────────────────────────────────────────
# Put your ANTHROPIC_AUTH_TOKEN values in a plain text file, ONE PER LINE, in
# priority order. Blank lines and lines starting with '#' are ignored.
#
#     mkdir -p ~/.headroom
#     cat > ~/.headroom/ica_tokens.txt <<'KEYS'
#     sk-key-one
#     sk-key-two
#     sk-key-three
#     KEYS
#     chmod 600 ~/.headroom/ica_tokens.txt
#
# This script mounts that file READ-ONLY into the container. The proxy never
# copies the keys into the image — they live only in your file on the host.
# To change keys, edit the file and re-run this script (it recreates the
# container; the file is re-read on start).
# ─────────────────────────────────────────────────────────────────────────────
#
# Usage:
#   scripts/run-ica-docker.sh
#
# Override via env:
#   ICA_IMAGE          docker image                 (default: headroom-ica:local)
#   ICA_PROXY_PORT     host port to publish         (default: 8787)
#   ICA_UPSTREAM_URL   upstream Anthropic-compatible(default: IBM ICA)
#   ICA_TOKEN_FILE     host path to the keys file   (default: ~/.headroom/ica_tokens.txt)
#   ICA_TOKEN_COOLDOWN seconds to skip a spent key  (default: 3600)
#   ICA_PRICE_INPUT    flat USD / 1M input tokens   (default: 5)
#   ICA_PRICE_OUTPUT   flat USD / 1M output tokens  (default: 25)
#   ICA_DATA_VOLUME    docker volume for cost/token
#                      + savings history            (default: headroom-ica-data)
#   ICA_CODE_AWARE     1=AST code compression on    (default: 1; image ships it)
#
# Cost/token history is written to /data inside the container, kept on a named
# docker volume so it SURVIVES re-running this script (which recreates the
# container). Re-deploying does NOT reset the totals. To wipe history, remove
# the volume: `docker volume rm headroom-ica-data`.

set -euo pipefail

IMAGE="${ICA_IMAGE:-headroom-ica:local}"
PORT="${ICA_PROXY_PORT:-8787}"
# Host interface the published port binds to. Defaults to loopback so the
# container is reachable only from this machine; front it with a deliberate
# forwarder (e.g. lan-forward) if you want LAN access. Set ICA_BIND_HOST=0.0.0.0
# to publish on all interfaces (not recommended — it exposes the proxy + real
# tokens to the whole network).
BIND_HOST="${ICA_BIND_HOST:-127.0.0.1}"
UPSTREAM_URL="${ICA_UPSTREAM_URL:-https://api.nextgen-beta.ica.ibm.com/ica}"
TOKEN_FILE="${ICA_TOKEN_FILE:-$HOME/.headroom/ica_tokens.txt}"
COOLDOWN="${ICA_TOKEN_COOLDOWN:-3600}"
PRICE_INPUT="${ICA_PRICE_INPUT:-5}"
PRICE_OUTPUT="${ICA_PRICE_OUTPUT:-25}"
# Model routing: map the model ids Claude Code sends (incl. dated/legacy) to the
# fixed ICA catalog so every picker selection resolves. Any haiku -> haiku-4-5,
# any sonnet -> sonnet-5, opus 4.7/4.8 exact, any other opus -> opus-4-8.
# Override with ICA_MODEL_MAP (JSON object or path to a JSON file).
MODEL_MAP="${ICA_MODEL_MAP:-{\"haiku\":\"claude-haiku-4-5\",\"sonnet\":\"claude-sonnet-5\",\"opus-4-7\":\"claude-opus-4-7\",\"opus-4-8\":\"claude-opus-4-8\",\"opus\":\"claude-opus-4-8\"}}"
DATA_VOLUME="${ICA_DATA_VOLUME:-headroom-ica-data}"
CODE_AWARE="${ICA_CODE_AWARE:-1}"
CONTAINER_NAME="headroom-ica"

# Path the keys file is mounted to inside the container. The image runs as the
# 'nonroot' user whose home is /home/nonroot, and /home/nonroot/.headroom exists.
CONTAINER_TOKEN_PATH="/home/nonroot/.headroom/ica_tokens.txt"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "error: keys file not found: $TOKEN_FILE" >&2
  echo "Create it with one ANTHROPIC_AUTH_TOKEN per line (see the header of this script)." >&2
  exit 1
fi

n_tokens="$(grep -cvE '^\s*(#|$)' "$TOKEN_FILE" || true)"
echo "Headroom → IBM ICA proxy (Docker)"
echo "  image    : $IMAGE"
echo "  upstream : $UPSTREAM_URL"
echo "  listen   : http://${BIND_HOST}:$PORT (bind host: $BIND_HOST)"
echo "  keys     : $n_tokens (from $TOKEN_FILE, mounted read-only, cooldown ${COOLDOWN}s)"
echo "  pricing  : \$${PRICE_INPUT}/1M in, \$${PRICE_OUTPUT}/1M out (flat; visible at /stats)"
echo "  model-map: $MODEL_MAP"
echo "  history  : volume '$DATA_VOLUME' → /data (survives re-deploy)"
echo "  code-aware: $([ "$CODE_AWARE" = "1" ] && echo enabled || echo disabled) (AST compression)"
echo

# Replace any previous instance. The named data volume is NOT removed here, so
# cost/token history persists across re-runs of this script.
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

exec docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${BIND_HOST}:${PORT}:8787" \
  -v "${TOKEN_FILE}:${CONTAINER_TOKEN_PATH}:ro" \
  -v "${DATA_VOLUME}:/data" \
  -e HEADROOM_TELEMETRY=off \
  -e HEADROOM_WORKSPACE_DIR=/data \
  -e HEADROOM_PRICE_INPUT_PER_1M="$PRICE_INPUT" \
  -e HEADROOM_PRICE_OUTPUT_PER_1M="$PRICE_OUTPUT" \
  -e HEADROOM_MODEL_MAP="$MODEL_MAP" \
  -e HEADROOM_CODE_AWARE_ENABLED="$CODE_AWARE" \
  "$IMAGE" \
  --host 0.0.0.0 --port 8787 \
  --anthropic-api-url "$UPSTREAM_URL" \
  --auth-token-file "$CONTAINER_TOKEN_PATH" \
  --auth-token-cooldown "$COOLDOWN"
