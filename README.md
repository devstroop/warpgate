# WarpGate

SOCKS5 + HTTP/HTTPS proxy over Cloudflare WARP, powered by [3proxy](https://github.com/3proxy/3proxy).

All traffic is routed through Cloudflare WARP for privacy and IP diversity.

## Quick Start

### Proxy container only

```bash
docker compose up -d warpgate
```

The proxy listens on:
- `1080` — SOCKS5 proxy
- `3128` — HTTP/HTTPS proxy

### Full stack (proxy pool + manager)

```bash
docker compose up -d --scale warpgate=3
```

This starts 3 warpgate proxy nodes and the manager control-plane on `:9090`.

## Usage

### curl

```bash
# SOCKS5
curl --socks5 127.0.0.1:1080 https://ipinfo.io/ip

# HTTP
curl -x http://127.0.0.1:3128 https://ipinfo.io/ip
```

### Verify WARP

```bash
curl --socks5 127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace
# Look for: warp=on
```

## Manager

A single Python control-plane that manages the warpgate proxy pool via the Docker API.
It creates, scales, health-checks, and rotates warpgate containers — proxy traffic flows
directly to the containers (SOCKS5/HTTP), not through the manager.

### Architecture

```
warpgate-manager (Python, docker-py)
  │  API :9090
  │  Docker socket → manages warpgate containers
  │
  └── warpgate-1 ── SOCKS5 :1080 ──→ upstream
  └── warpgate-2 ── SOCKS5 :1080 ──→ upstream
  └── warpgate-3 ── SOCKS5 :1080 ──→ upstream
```

Clients (e.g. ai-gateway) connect to warpgate containers directly via SOCKS5.
The manager is a **management-only** control plane.

### API Reference (v1)

> **Breaking change** from the 1.x API: routes now live under `/v1`, mutating
> operations return `202 + Location` (poll the returned task), and the proxy
> shape changed. The old routes (`/health`, `/status`, `/proxies`, `/scale`)
> are removed.

**Base URL:** `http://manager:9090/v1`

**Auth:** all routes except `GET /v1/health`,
`GET /v1/openapi.yaml`, `GET /v1/docs` and `OPTIONS` require
`Authorization: Bearer <MANAGER_API_KEY>` when a key is configured (so
load-balancer probes always work). Failed auth is rate-limited per client IP.
An unset **or empty** `MANAGER_API_KEY` disables auth.

**Async operations:** `POST /v1/proxies`, `PATCH /v1/pool`, rotate, restart
and delete return `202 Accepted` with the task body and a `Location:
/v1/tasks/{id}` header. Poll `GET /v1/tasks/{id}` until `status` is
`succeeded` or `failed` (running tasks set `Retry-After: 1`). Tasks live in
memory and are lost on manager restart.

**Polling/backoff:** `proxy.create`/`proxy.restart` can take up to ~90s (WARP
connect + health check). Poll with exponential backoff (e.g. 1s → 2s → 4s,
capped at ~5s) rather than hammering every second.

**Idempotency:** every mutating operation accepts an optional
`Idempotency-Key` header. Repeating a request with the same key returns the
already-created task instead of starting a new operation — a client that
retries after a timeout/network blip never duplicates a proxy. Reusing a key
with a *different* operation is rejected with `409 IDEMPOTENCY_CONFLICT`.

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| `GET` | `/v1/health` | — | Liveness `{"status","pool_size","healthy","degraded"}` → always `200` while the manager is up |
| `GET` | `/v1/pool` | `?include=proxies` (default on; `none` to omit) | Pool summary + proxy list |
| `PATCH` | `/v1/pool` | `{"target": N}` | `202` + task (replaces `POST /scale`) |
| `GET` | `/v1/proxies` | `?healthy=true\|false` | `[Proxy, ...]` |
| `POST` | `/v1/proxies` | `{"name": "optional"}` | `202` + task (replaces `POST /proxies`; optional explicit name) |
| `GET` | `/v1/proxies/{id}` | — | single `Proxy` or `404` |
| `DELETE` | `/v1/proxies/{id}` | — | `202` + task |
| `POST` | `/v1/proxies/{id}/rotate` | — | `202` + task |
| `POST` | `/v1/proxies/{id}/restart` | — | `202` + task (recreates the container) |
| `POST` | `/v1/proxies/rotate` | `{"scope":"unhealthy"\|"all"}` (default `unhealthy`) | `202` + task (bulk) |
| `GET` | `/v1/tasks/{id}` | — | task object; `404` if unknown/expired |
| `GET` | `/v1/openapi.yaml` | — | OpenAPI 3.1 spec |
| `GET` | `/v1/docs` | — | Swagger UI |

Proxy shape (consistent everywhere; `in_flight` removed, ISO8601 timestamps):

```json
{
  "id": "warpgate-a1b2c3d4",
  "container_id": "9f2c...",
  "endpoints": {
    "socks5": "socks5://warpgate-a1b2c3d4:1080",
    "http": "http://warpgate-a1b2c3d4:3128"
  },
  "status": {
    "healthy": true,
    "socks5": true,
    "http": true,
    "warp": {"connected": true, "detail": "Connected"}
  },
  "created_at": "2026-08-14T00:00:00Z",
  "uptime_s": 120.5
}
```

Errors use a consistent envelope; every response carries `X-Request-ID`:

```json
{"error": {"code": "PROXY_NOT_FOUND", "message": "proxy x not found", "request_id": "..."}}
```

| Code | HTTP |
|---|---|
| `UNAUTHORIZED` | 401 |
| `VALIDATION_ERROR` | 422 |
| `RATE_LIMITED` | 429 |
| `PROXY_NOT_FOUND` / `TASK_NOT_FOUND` | 404 |
| `POOL_AT_CAPACITY` / `PROXY_EXISTS` / `IDEMPOTENCY_CONFLICT` | 409 |
| `DOCKER_UNAVAILABLE` | 503 |
| `NOT_FOUND` / `METHOD_NOT_ALLOWED` / `INTERNAL` | 404 / 405 / 500 |
| `CREATE_FAILED` / `RECREATE_FAILED` | task `error_code` (no HTTP status) |

Failed tasks expose the same machine-readable code as `error_code` (plus a
human-readable `error`). Task `result` shapes by type: `proxy.create` → a
`Proxy`; `proxy.remove` → `{"status","id"}`; `proxy.rotate` →
`{"rotated", "proxy"}`; `proxy.restart` → `{"restarted", "proxy"}`;
`proxy.rotate_bulk` → `{"rotated", "failed", "count"}`; `pool.scale` → a pool
summary without the proxy list.

Desired-state semantics: the pool converges toward `target`. Creating a proxy
increments the target; deleting one decrements it; `PATCH /v1/pool` sets it
explicitly. Proxies created via `POST /v1/proxies` are **not** scaled away by
the reconciler.

### Operational notes

- The manager health-checks proxies by **container name**, so it must run on
  the same Docker network as the warpgate containers (as the compose manager
  service does). A manager running on the host cannot resolve container names
  and will mark every proxy unhealthy.
- Each manager-created proxy gets its own named data/cache volume
  (`<name>-data`, `<name>-cache`) so `restart` keeps the WARP registration
  (`reg.json`) instead of re-registering (re-registration is rate-limited by
  Cloudflare). Volumes are removed when the proxy is deleted, scaled away, or
  evicted by the health checker.
- Query filters are strict: `include` must be `proxies|none`, `healthy` must
  be `true|false`, and `limit` must be an integer in `1..500` — anything else
  is a `422 VALIDATION_ERROR`.

### Environment Variables (Manager)

| Variable | Default | Description |
|---|---|---|
| `WARPGATE_IMAGE` | `warpgate:local` | Docker image for proxy containers |
| `WARPGATE_PREFIX` | `warpgate-` | Container name prefix |
| `WARPGATE_COUNT` | `3` | Target pool size on startup |
| `WARPGATE_NETWORK` | `warpgate-net` | Docker network to attach containers (compose overrides this with `warpgate_warpgate-net`) |
| `MANAGER_PORT` | `9090` | Management API listen port |
| `MANAGER_API_KEY` | _(none)_ | Enables Bearer auth; leave unset **or empty** to disable |
| `MANAGER_MAX_POOL` | `20` | Hard cap on pool size |
| `MANAGER_RATE_LIMIT` | `10` | Max failed-auth attempts per IP per window |
| `MANAGER_HEALTH_HOST` | `cloudflare.com` | HTTP probe CONNECT target (host) |
| `MANAGER_HEALTH_PORT` | `443` | HTTP probe CONNECT target (port) |
| `MANAGER_DEBUG` | _(none)_ | Set to `1` for debug logging |
| `MANAGER_SKIP_INIT` | _(none)_ | Set to `1` to skip pool discovery at startup |

### Integration with ai-gateway

The manager manages the proxy pool independently. ai-gateway consumes
a static list of proxy URLs from its own `config.toml`:

```toml
[warpgate]
proxies = [
  "socks5://warpgate-a1b2c3d4:1080",
  "socks5://warpgate-e5f6g7h8:1080",
  "socks5://warpgate-9i0jklmn:1080",
]
```

To get the current proxy list from the manager for config generation:

```bash
curl -H "Authorization: Bearer $MANAGER_API_KEY" \
  http://manager:9090/v1/proxies | jq -r '.[].endpoints.socks5'
```

#### Example: create a proxy (named, retry-safe) and wait for it

```bash
task=$(curl -sf -X POST -H "Authorization: Bearer $MANAGER_API_KEY" \
  -H "Idempotency-Key: create-$RANDOM" \
  -d '{"name":"warpgate-ai1"}' \
  http://manager:9090/v1/proxies)
id=$(echo "$task" | jq -r .id)

while true; do
  state=$(curl -sf -H "Authorization: Bearer $MANAGER_API_KEY" \
    http://manager:9090/v1/tasks/$id)
  echo "$state"
  [ "$(echo "$state" | jq -r .status)" = "succeeded" ] && break
  [ "$(echo "$state" | jq -r .status)" = "failed" ] && exit 1
  sleep 1
done
```

#### Running the test suite

```bash
cd manager
pip install -r requirements-dev.txt
pytest
```

## Configuration

### Container Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WARP_WAIT_RETRIES` | `30` | Number of status-poll retries before starting 3proxy |
| `WARP_WAIT_INTERVAL` | `2` | Seconds between status polls |

### Custom 3proxy Config

Mount your own config:

```yaml
services:
  warpgate:
    volumes:
      - ./custom.cfg:/etc/3proxy/3proxy.cfg
```

See [3proxy.cfg docs](https://github.com/3proxy/3proxy/wiki/3proxy.cfg) for all options.

## Files

```
warpgate/
├── Dockerfile                       # Pure proxy container (WARP + 3proxy)
├── compose.yaml                     # Single/multi-instance + manager
├── entrypoint.sh                    # Container startup
├── 3proxy.cfg                       # Default proxy config
├── manager/
│   ├── Dockerfile                   # Manager container image
│   ├── requirements.txt             # Python dependencies (runtime)
│   ├── requirements-dev.txt         # Dev deps (pytest)
│   ├── tests/                       # pytest suite (fake Docker client)
│   │   ├── conftest.py
│   │   ├── test_api.py
│   │   ├── test_schemas.py
│   │   └── test_tasks.py
│   └── warpgate_manager/
│       ├── __init__.py
│       ├── config.py                # Env configuration
│       ├── pool.py                  # Docker + pool/scale/rotate/restart logic
│       ├── tasks.py                 # Async task registry + worker pool
│       ├── schemas.py               # Serializers + request validation
│       ├── server.py                # Flask wiring + /v1 routes
│       └── openapi.yaml             # OpenAPI 3.1 spec (source of truth)
└── README.md
```

## Stack

- [3proxy](https://github.com/3proxy/3proxy) — Tiny proxy server
- [Cloudflare WARP](https://developers.cloudflare.com/warp-client/) — Encrypted tunnel
- [docker-py](https://github.com/docker/docker-py) — Docker SDK for Python
- [Flask](https://flask.palletsprojects.com/) — HTTP API framework
- Ubuntu 24.04 (noble) base image
