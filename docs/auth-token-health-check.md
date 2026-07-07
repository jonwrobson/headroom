# Auth Token Health Check

The Headroom proxy can now check the health status of all configured authentication tokens via the `/health` endpoint.

## Overview

When using multiple API tokens (e.g., for IBM ICA with budget rotation), the `/health` endpoint will automatically test each token and report its status. This helps you:

- Monitor which tokens are active and working
- Identify tokens that have exceeded their budget
- Detect expired or invalid tokens
- Plan token rotation before exhaustion

## Token File Format

The token file supports two formats:

### CSV Format (Recommended)
```
# Format: ID,TOKEN
production,sk-ant-api03-prod-token-here
backup,sk-ant-api03-backup-token-here
development,sk-ant-api03-dev-token-here
```

### Plain Format (Backward Compatible)
```
sk-ant-api03-token-one
sk-ant-api03-token-two
sk-ant-api03-token-three
```

With plain format, tokens are auto-labeled with their last 4 characters (e.g., `...here`).

## Configuration

1. **Create or update your token file**:
   ```bash
   cat > ~/.headroom/ica_tokens.txt <<EOF
   prod-token-1,sk-ant-api03-xxxxx
   backup-token-2,sk-ant-api03-yyyyy
   dev-token-3,sk-ant-api03-zzzzz
   EOF
   chmod 600 ~/.headroom/ica_tokens.txt
   ```

2. **Start the proxy with token rotation**:
   ```bash
   # Local
   scripts/run-ica-proxy.sh
   
   # Docker
   scripts/run-ica-docker.sh
   ```

## Health Check Response

The `/health` endpoint now includes an `auth_tokens` section:

```json
{
  "service": "headroom-proxy",
  "status": "healthy",
  "ready": true,
  "version": "0.5.25",
  "timestamp": "2026-06-19T10:00:00Z",
  "uptime_seconds": 3600,
  "auth_tokens": {
    "total": 3,
    "healthy": 2,
    "tokens": [
      {
        "id": "prod-token-1",
        "status": "active",
        "healthy": true
      },
      {
        "id": "backup-token-2",
        "status": "budget_exceeded",
        "error_type": "budget_exceeded",
        "message": "Budget has been exceeded! Current cost: 3212.27, Max budget: 3200.0",
        "healthy": false
      },
      {
        "id": "dev-token-3",
        "status": "active",
        "healthy": true
      }
    ]
  },
  "checks": { ... },
  "runtime": { ... }
}
```

## Token Status Values

| Status | Description | Healthy |
|--------|-------------|---------|
| `active` | Token is working and has budget remaining | ✅ Yes |
| `budget_exceeded` | Token has exceeded its budget limit | ❌ No |
| `unauthorized` | Token is invalid or expired | ❌ No |
| `error` | Permission issues, network errors, or other API problems | ❌ No |

## Error Types

When a token has an error, the `error_type` field provides additional context:

| Error Type | Description |
|------------|-------------|
| `budget_exceeded` | Budget limit reached for current billing period |
| `authentication_error` | Invalid, expired, or malformed token |
| `permission_error` | Token lacks permission for the requested model/operation |
| `invalid_request_error` | Malformed request or unsupported parameters |
| `http_error` | HTTP-level error (connection, timeout, etc.) |
| `unknown` | Error type could not be determined |

## Usage Examples

> **Note:** the `auth_tokens` block is returned to all `/health` callers. The
> trust boundary is the network bind — publish the proxy on loopback only
> (`ICA_BIND_HOST=127.0.0.1`) so only the local host can reach the endpoint.

### Check Token Health
```bash
curl -s http://localhost:8787/health | jq '.auth_tokens'
```

### Monitor Healthy Token Count
```bash
curl -s http://localhost:8787/health | jq '.auth_tokens.healthy'
```

### List All Token Statuses
```bash
curl -s http://localhost:8787/health | jq '.auth_tokens.tokens[] | {id, status, healthy}'
```

### Alert on Low Healthy Tokens
```bash
#!/bin/bash
HEALTHY=$(curl -s http://localhost:8787/health | jq '.auth_tokens.healthy')
TOTAL=$(curl -s http://localhost:8787/health | jq '.auth_tokens.total')

if [ "$HEALTHY" -lt 2 ]; then
  echo "WARNING: Only $HEALTHY/$TOTAL tokens are healthy!"
  # Send alert...
fi
```

## How It Works

1. **Parallel Testing**: All tokens are tested simultaneously for speed
2. **Minimal API Usage**: Each check sends a single 1-token test message using `claude-opus-4-8`
3. **Non-Blocking**: Health checks don't affect request processing
4. **Budget Detection**: Identifies tokens that have hit their spend limit
5. **Automatic Exhaustion**: Budget-exceeded tokens are automatically marked as exhausted
6. **Smart Rotation**: Proxy automatically skips failed tokens and tries the next one
7. **Error Handling**: Network failures don't crash the health endpoint

## Token Rotation Behavior

When a request fails with a token-specific error, the proxy automatically:

1. **Detects Error Type**: Identifies budget, authentication, or permission errors
2. **Marks Token**: Marks the failed token as exhausted
3. **Rotates**: Immediately tries the next available token
4. **Continues**: Only returns an error when ALL tokens have failed

**Errors that trigger rotation:**
- `budget_exceeded` - Token has hit spend limit (waits until Monday 1 AM UTC or cooldown)
- `authentication_error` - Invalid or expired token
- `permission_error` - Token lacks permission for requested model/operation
- `invalid_api_key` - Token is malformed or revoked
- HTTP 401 - Authentication failure

**Errors that DON'T trigger rotation:**
- Rate limiting (429) - Temporary, will succeed on retry
- Server errors (5xx) - Upstream issues, not token-specific
- Invalid requests (400) - Client error, not token-specific

## Performance

- **Latency**: ~1-2 seconds for 3 tokens (parallel execution)
- **API Cost**: 1 token per check (negligible)
- **Frequency**: Only when `/health` is called (not automatic polling)

## Monitoring Integration

### Prometheus
```yaml
# Alert when healthy tokens drop below threshold
- alert: LowHealthyTokens
  expr: headroom_auth_tokens_healthy < 2
  for: 5m
  annotations:
    summary: "Low number of healthy auth tokens"
```

### Kubernetes Liveness Probe
```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8787
  initialDelaySeconds: 30
  periodSeconds: 60
```

### Docker Health Check
```dockerfile
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8787/health || exit 1
```

## Troubleshooting

### No `auth_tokens` in Response
- Token file not configured or empty
- Check `HEADROOM_ANTHROPIC_AUTH_TOKEN_FILE` environment variable
- Verify file exists and is readable

### All Tokens Show `error` Status
- Check network connectivity to API endpoint
- Verify API URL is correct
- Check firewall/proxy settings

### Tokens Show `unauthorized`
- Tokens may be expired or invalid
- Verify tokens are correctly formatted
- Check with your API provider

## Security Notes

- Token IDs are visible in health checks (not the full tokens)
- Use descriptive but non-sensitive IDs (e.g., "prod-1" not "john-personal")
- Protect the `/health` endpoint if exposing publicly
- Token file should have restricted permissions (600)