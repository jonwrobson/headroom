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

set -euo pipefail

IMAGE="${ICA_IMAGE:-headroom-ica:local}"
PORT="${ICA_PROXY_PORT:-8787}"
UPSTREAM_URL="${ICA_UPSTREAM_URL:-https://api.nextgen-beta.ica.ibm.com/ica}"
TOKEN_FILE="${ICA_TOKEN_FILE:-$HOME/.headroom/ica_tokens.txt}"
COOLDOWN="${ICA_TOKEN_COOLDOWN:-3600}"
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
echo "  listen   : http://localhost:$PORT"
echo "  keys     : $n_tokens (from $TOKEN_FILE, mounted read-only, cooldown ${COOLDOWN}s)"
echo

# Replace any previous instance.
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

exec docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${PORT}:8787" \
  -v "${TOKEN_FILE}:${CONTAINER_TOKEN_PATH}:ro" \
  -e HEADROOM_TELEMETRY=off \
  "$IMAGE" \
  --host 0.0.0.0 --port 8787 \
  --anthropic-api-url "$UPSTREAM_URL" \
  --auth-token-file "$CONTAINER_TOKEN_PATH" \
  --auth-token-cooldown "$COOLDOWN"
