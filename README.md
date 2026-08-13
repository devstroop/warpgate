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

### API Reference

#### `GET /health`

Pool liveness. Returns 200 when at least one proxy is healthy.

```json
{"status": "ok", "pool_size": 3, "healthy": 2}
```

#### `GET /status`

Full pool status with per-proxy detail.

```json
{
  "version": "1.0",
  "pool_size": 3,
  "healthy": 3,
  "degraded": 0,
  "target": 3,
  "proxies": [
    {
      "name": "warpgate-a1b2c3d4",
      "socks5": "socks5://warpgate-a1b2c3d4:1080",
      "healthy": true,
      "warp": "Connected",
      "in_flight": 0,
      "uptime_s": 120.5
    }
  ]
}
```

#### `GET /proxies`

List all proxy endpoints (compact, for config generation).

```json
[
  {"name": "warpgate-a1b2c3d4", "socks5": "socks5://warpgate-a1b2c3d4:1080", "healthy": true, "warp": "Connected"},
  {"name": "warpgate-e5f6g7h8", "socks5": "socks5://warpgate-e5f6g7h8:1080", "healthy": true, "warp": "Connected"}
]
```

#### `POST /proxies`

Create a new proxy container. Returns the endpoint details.

```json
{"name": "warpgate-abc12345", "socks5": "socks5://warpgate-abc12345:1080"}
```

#### `DELETE /proxies/<name>`

Remove a proxy container from the pool and Docker. Returns 404 if not found.

#### `POST /proxies/<name>/rotate`

Trigger a WARP reconnection on a specific proxy (disconnect + reconnect).
Returns updated health after rotation.

#### `POST /scale?count=N`

Scale the pool to `N` containers. Accepts `count` as query param or JSON body.

```json
{"status": "scaled", "target": 5, "pool_size": 5}
```

### Environment Variables (Manager)

| Variable | Default | Description |
|---|---|---|
| `WARPATE_IMAGE` | `warpgate:local` | Docker image for proxy containers |
| `WARPATE_PREFIX` | `warpgate-` | Container name prefix |
| `WARPATE_COUNT` | `3` | Target pool size on startup |
| `WARPATE_NETWORK` | `warpgate_warpgate-net` | Docker network to attach containers |
| `MANAGER_PORT` | `9090` | Management API listen port |
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
curl http://manager:9090/proxies | jq -r '.[].socks5'
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
│   ├── requirements.txt             # Python dependencies
│   └── server.py                    # Manager management server
└── README.md
```

## Stack

- [3proxy](https://github.com/3proxy/3proxy) — Tiny proxy server
- [Cloudflare WARP](https://developers.cloudflare.com/warp-client/) — Encrypted tunnel
- [docker-py](https://github.com/docker/docker-py) — Docker SDK for Python
- [Flask](https://flask.palletsprojects.com/) — HTTP API framework
- Ubuntu 24.04 (noble) base image
