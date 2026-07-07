# Deploying Token Health Check Feature

This guide walks through deploying the new auth token health check feature to your Docker container.

## Changes Made

1. **`headroom/proxy/auth_token_pool.py`**
   - Added `TokenInfo` dataclass for named tokens
   - Updated `load_tokens_from_file()` to support CSV format (ID,TOKEN)
   - Added `check_token_health()` method to test individual tokens
   - Added `check_all_tokens()` method for parallel health checks
   - Maintained backward compatibility with plain token format

2. **`headroom/proxy/server.py`**
   - Enhanced `/health` endpoint to include token health checks
   - Added `auth_tokens` section to health response

3. **`docs/auth-token-health-check.md`**
   - Complete documentation for the new feature

4. **`tests/test_auth_token_health_check.py`**
   - Comprehensive test suite for token parsing and health checks

## Deployment Steps

### 1. Update Your Token File

Update `~/.headroom/ica_tokens.txt` to use the new CSV format:

```bash
cat > ~/.headroom/ica_tokens.txt <<'EOF'
production,sk-ant-api03-your-prod-token
backup,sk-ant-api03-your-backup-token
development,sk-ant-api03-your-dev-token
EOF

chmod 600 ~/.headroom/ica_tokens.txt
```

**Note**: The old plain format still works, but CSV format gives you named tokens in health checks.

### 2. Rebuild the Docker Image

```bash
# From the headroom project root
docker build --target runtime -t headroom-ica:local .
```

This will:
- Build the new image with updated code
- Tag it as `headroom-ica:local`
- Take ~2-5 minutes depending on your system

### 3. Stop and Remove Old Container

```bash
# Stop the running container
docker rm -f headroom-ica

# Optional: Check it's stopped
docker ps -a | grep headroom-ica
```

**Note**: This does NOT delete your data volume (`headroom-ica-data`), so your cost/token history is preserved.

### 4. Start New Container

```bash
# Use the existing script (it will use the new image)
scripts/run-ica-docker.sh
```

Or manually:

```bash
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

### 5. Verify Deployment

Check the container is running:

```bash
docker ps | grep headroom-ica
```

Check logs for startup:

```bash
docker logs headroom-ica
```

You should see:
```
Auth-token rotation enabled: 3 token(s) from /home/nonroot/.headroom/ica_tokens.txt (cooldown 3600s)
```

### 6. Test the Health Endpoint

> **Note:** `/health` includes the `auth_tokens` (per-key budget) and `config`
> blocks for all callers. The trust boundary is the network bind — the proxy is
> published on **loopback only** (`ICA_BIND_HOST=127.0.0.1`, and the LAN
> forwarder binds `127.0.0.1`), so only this host can reach the endpoint.

```bash
# Basic health check
curl -s http://localhost:8787/health | jq '.'

# Check token health specifically
curl -s http://localhost:8787/health | jq '.auth_tokens'
```

Expected response:
```json
{
  "total": 3,
  "healthy": 2,
  "tokens": [
    {
      "id": "production",
      "status": "active",
      "healthy": true
    },
    {
      "id": "backup",
      "status": "budget_exceeded",
      "message": "Budget has been exceeded! Current cost: 3212.27, Max budget: 3200.0",
      "healthy": false
    },
    {
      "id": "development",
      "status": "active",
      "healthy": true
    }
  ]
}
```

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker logs headroom-ica

# Common issues:
# - Port 8787 already in use
# - Token file not found
# - Invalid token file format
```

### No `auth_tokens` in Health Response

```bash
# Verify token file is mounted
docker exec headroom-ica cat /home/nonroot/.headroom/ica_tokens.txt

# Check proxy logs
docker logs headroom-ica | grep -i "auth-token"
```

### All Tokens Show `error` Status

```bash
# Test connectivity from container
docker exec headroom-ica curl -v https://api.nextgen-beta.ica.ibm.com/ica/v1/messages

# Check if API URL is correct
curl -s http://localhost:8787/health | jq '.config.anthropic_api_url'
```

### Token File Format Issues

```bash
# Verify format (should show ID,TOKEN or just TOKEN)
cat ~/.headroom/ica_tokens.txt

# Check for hidden characters
cat -A ~/.headroom/ica_tokens.txt

# Recreate with correct format
cat > ~/.headroom/ica_tokens.txt <<'EOF'
prod,sk-ant-api03-token1
backup,sk-ant-api03-token2
EOF
```

## Rollback

If you need to rollback to the previous version:

```bash
# Stop new container
docker rm -f headroom-ica

# Pull/use previous image
docker run -d --name headroom-ica --restart unless-stopped \
  -p 8787:8787 \
  -v "$HOME/.headroom/ica_tokens.txt:/home/nonroot/.headroom/ica_tokens.txt:ro" \
  -v "headroom-ica-data:/data" \
  -e HEADROOM_TELEMETRY=off \
  -e HEADROOM_WORKSPACE_DIR=/data \
  headroom-ica:previous \
  --host 0.0.0.0 --port 8787 \
  --anthropic-api-url https://api.nextgen-beta.ica.ibm.com/ica \
  --auth-token-file /home/nonroot/.headroom/ica_tokens.txt \
  --auth-token-cooldown 3600
```

**Note**: The new CSV token format is backward compatible, so old code will still work (it will just ignore the ID part).

## Monitoring

### Set Up Alerts

```bash
#!/bin/bash
# check-token-health.sh

HEALTHY=$(curl -s http://localhost:8787/health | jq '.auth_tokens.healthy')
TOTAL=$(curl -s http://localhost:8787/health | jq '.auth_tokens.total')

if [ "$HEALTHY" -lt 2 ]; then
  echo "ALERT: Only $HEALTHY/$TOTAL tokens are healthy!"
  # Send notification (email, Slack, PagerDuty, etc.)
fi
```

### Add to Cron

```bash
# Check every 5 minutes
*/5 * * * * /path/to/check-token-health.sh
```

## Next Steps

1. Monitor the `/health` endpoint for token status
2. Set up alerts for low healthy token counts
3. Plan token rotation before exhaustion
4. Review the full documentation: `docs/auth-token-health-check.md`

## Support

If you encounter issues:
1. Check container logs: `docker logs headroom-ica`
2. Verify token file format
3. Test API connectivity manually
4. Review the troubleshooting section above