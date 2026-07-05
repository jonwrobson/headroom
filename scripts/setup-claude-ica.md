# Claude Code → Headroom → IBM ICA (with auth-token rotation)

This fork adds an **auth-token pool** to the Headroom proxy: it holds a list of
`ANTHROPIC_AUTH_TOKEN` values (each with its own upstream spend budget) and, when
one returns a `budget_exceeded` error, transparently retries the same request
with the next token. Only when **every** token is exhausted does a request fail
(HTTP 429 `all_tokens_exhausted`).

```
Claude Code (VSCode) ──▶ Headroom proxy (localhost:8787) ──▶ https://api.nextgen-beta.ica.ibm.com/ica
  ANTHROPIC_BASE_URL=localhost        overrides Authorization      /v1/messages
                                      from the token pool,
                                      rotates on budget_exceeded
```

## 1. Create the token file

```sh
mkdir -p ~/.headroom
cp scripts/ica_tokens.txt.example ~/.headroom/ica_tokens.txt
chmod 600 ~/.headroom/ica_tokens.txt
# edit ~/.headroom/ica_tokens.txt — one token per line, in priority order
```

The real token file lives under `~/.headroom/` and is **never committed**
(`scripts/ica_tokens.txt` is git-ignored in this repo as a safety net).

## 2. Start the proxy

### Option A — Docker (recommended)

Build the image once from this fork (it runs the Python proxy, where the
rotation lives):

```sh
docker build --target runtime -t headroom-ica:local .
```

Then run it. The keys file is mounted **read-only** into the container — the
tokens never enter the image, only your host file:

```sh
scripts/run-ica-docker.sh
```

which is equivalent to:

```sh
docker run -d --name headroom-ica --restart unless-stopped \
  -p 8787:8787 \
  -v "$HOME/.headroom/ica_tokens.txt:/home/nonroot/.headroom/ica_tokens.txt:ro" \
  -v "headroom-ica-data:/data" \
  -e HEADROOM_TELEMETRY=off \
  -e HEADROOM_WORKSPACE_DIR=/data \
  -e HEADROOM_PRICE_INPUT_PER_1M=5 \
  -e HEADROOM_PRICE_OUTPUT_PER_1M=25 \
  -e HEADROOM_CODE_AWARE_ENABLED=1 \
  headroom-ica:local \
  --host 0.0.0.0 --port 8787 \
  --anthropic-api-url https://api.nextgen-beta.ica.ibm.com/ica \
  --auth-token-file /home/nonroot/.headroom/ica_tokens.txt \
  --auth-token-cooldown 3600
```

To change keys: edit `~/.headroom/ica_tokens.txt` and re-run
`scripts/run-ica-docker.sh` (it recreates the container so the file is re-read).
Logs: `docker logs -f headroom-ica`. Stop: `docker rm -f headroom-ica`.

The `headroom-ica-data` named volume holds cost/token history (and savings
history) at `/data`, so **re-running the script / re-deploying does NOT reset
the totals** — only `docker volume rm headroom-ica-data` clears them.

### Option B — local process (no Docker)

```sh
scripts/run-ica-proxy.sh
```

which runs (telemetry off):

```sh
headroom proxy \
  --anthropic-api-url https://api.nextgen-beta.ica.ibm.com/ica \
  --auth-token-file ~/.headroom/ica_tokens.txt \
  --auth-token-cooldown 3600 \
  --price-input 5 \
  --price-output 25 \
  --code-aware
```

`--code-aware` enables AST-based code compression; install the extra first with
`pip install headroom-ai[code]` (the Docker image already ships it). Without the
extra the proxy logs a warning and runs without it.

Cost/token persistence is **opt-in**: set `HEADROOM_COST_PATH` (the launcher
defaults it to `~/.headroom/proxy_cost.json`) to keep totals across restarts.
Without it the proxy tracks cost in memory only and resets on restart.

## 3. Point Claude Code (VSCode extension) at the proxy

`headroom wrap claude` only wraps the **terminal** CLI — it does not reach the
VSCode extension. Configure the extension via `~/.claude/settings.json`, whose
`env` block Claude Code honors in the IDE:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8787",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "ANTHROPIC_AUTH_TOKEN": "managed-by-headroom"
  }
}
```

Notes:

- `ANTHROPIC_BASE_URL` → the local proxy. The proxy forwards to the IBM ICA URL.
- `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` is a Claude Code client flag (kept on
  the client side; not forwarded upstream).
- `ANTHROPIC_AUTH_TOKEN` is a **placeholder** — the proxy overrides the
  Authorization header from its token pool, so the client value is ignored. It
  just needs to be set so Claude Code sends the request.

Reload the VSCode window after editing settings, then run `/status` in the
extension to confirm it is talking to the local proxy.

## Configuration reference

| Setting | CLI flag | Env var | Default |
|---|---|---|---|
| Upstream URL | `--anthropic-api-url` | `ANTHROPIC_TARGET_API_URL` | api.anthropic.com |
| Token file | `--auth-token-file` | `HEADROOM_ANTHROPIC_AUTH_TOKEN_FILE` | unset (passthrough) |
| Cooldown (s) | `--auth-token-cooldown` | `HEADROOM_AUTH_TOKEN_COOLDOWN_S` | 3600 |
| Input price / 1M | `--price-input` | `HEADROOM_PRICE_INPUT_PER_1M` | unset (LiteLLM lookup) |
| Output price / 1M | `--price-output` | `HEADROOM_PRICE_OUTPUT_PER_1M` | unset (LiteLLM lookup) |
| Code-aware | `--code-aware` | `HEADROOM_CODE_AWARE_ENABLED=1` | disabled (needs `[code]` extra) |
| State dir | — | `HEADROOM_WORKSPACE_DIR` | `~/.headroom` |

## Cost tracking & history

ICA bills in **credit points**, and its model names aren't in LiteLLM's pricing
database — so without an override the proxy can't attach a cost to requests. Set
`--price-input 5 --price-output 25` (or the env vars) to price every request at a
flat **$5/1M input, $25/1M output**; all input-side tokens (uncached + cache) use
the input rate. Output tokens are tracked separately.

See the running totals (input/output tokens and cost) any time:

```sh
curl -s localhost:8787/stats | jq '.cost'
# total_input_tokens, total_output_tokens,
# total_input_cost_usd, total_output_cost_usd, total_cost_usd
```

**Persistence / re-deploy:** persistence is **opt-in** — it turns on only when
`HEADROOM_WORKSPACE_DIR` or `HEADROOM_COST_PATH` is set (otherwise totals are
in-memory and reset on restart). When enabled, totals are snapshotted to
`$HEADROOM_WORKSPACE_DIR/proxy_cost.json` (every 60s and on shutdown) and reloaded
on startup. The Docker helper sets `HEADROOM_WORKSPACE_DIR=/data` and mounts the
`headroom-ica-data:/data` named volume, so **history survives container
re-deploys**; remove the volume to reset. Without the volume an in-container
`/data` is ephemeral and totals reset when the container is recreated. Note: cost
state is per-process — run a single proxy worker for accurate totals.

Detection of an exhausted token keys on the upstream `error.type ==
"budget_exceeded"` (or message containing "budget has been exceeded"). Every
other error — including ordinary 400s like an invalid model — is passed straight
through to the client and never burns a token. The match is overridable via
`ProxyConfig.spend_limit_error_types` / `spend_limit_match`.
