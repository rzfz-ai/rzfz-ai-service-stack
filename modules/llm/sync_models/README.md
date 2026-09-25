# Model Sync Service

Automatically synchronizes GPUStack model availability with OpenWebUI's model database.

## Purpose

This service runs as a background container that:
1. Polls GPUStack's API for available models (every 60 seconds)
2. Updates OpenWebUI's PostgreSQL database with model metadata
3. Ensures model names and capabilities are in sync between services

## Configuration

Set these environment variables in `.env`:

| Variable | Description | Required |
|----------|-------------|----------|
| `GPUSTACK_URL` | GPUStack API endpoint (set automatically by compose) | Yes |
| `GPUSTACK_API_KEY` | API key from GPUStack UI | Yes |
| `DB_HOST` | PostgreSQL hostname (set automatically by compose) | Yes |
| `DB_NAME` | OpenWebUI database name (set automatically by compose) | Yes |
| `DB_USER` | PostgreSQL username (set automatically by compose) | Yes |
| `DB_PASS` | PostgreSQL password (set automatically by compose) | Yes |

## Building

```bash
docker build -f Dockerfile.sync_models -t razzfazz:sync-models .
```

## Usage

The service is automatically started with the `llm-*` profiles. It runs continuously and syncs models every 60 seconds.

## Setting the GPUStack API Key

The `GPUSTACK_API_KEY` must be generated in the GPUStack UI **after the first start** of GPUStack.

### Via Setup Tool (recommended)

```bash
# Set the key
rzfz setup --set-gpustack-api-key gpustack_xxxx_xxxxxxxxxxxxxxxx

# Check current status
rzfz setup --gpustack-api-key-status
```

Or use the Configuration Portal at `https://settings.<your-domain>/` → Settings → GPUStack (the setup.<domain> UI was removed in 2026.07, #22).

### Via GPUStack UI

1. Start GPUStack and log in at `https://llm.<your-domain>/`
2. Go to **API Keys**
3. Create a new key with read permissions
4. Copy the key and set it via the setup tool (see above)

### After Setting the Key

The model-sync container must be **recreated** (not just restarted) to pick up the new `.env` value:

```bash
docker compose up -d --force-recreate model-sync
```

> **⚠️ Note:** `docker compose restart model-sync` does **not** reload `.env` changes. The container must be destroyed and recreated to apply updated environment variables.
