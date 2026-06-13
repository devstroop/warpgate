# warp-proxy

SOCKS5 + HTTP/HTTPS proxy over Cloudflare WARP, powered by [3proxy](https://github.com/3proxy/3proxy).

All traffic is routed through Cloudflare WARP for privacy and security.

## Quick Start

### Single proxy (compose.yaml)

```bash
docker compose up -d
```

The proxy listens on:
- `1080` — SOCKS5 proxy
- `3128` — HTTP/HTTPS proxy

### Multi-proxy with manager API (compose.manager.yaml)

```bash
# Build the warp-proxy image first
docker compose build warp-proxy

# Start the manager (spawns proxies on demand)
docker compose -f compose.manager.yaml up -d
```

Manager API on `http://localhost:8000`. Set `MANAGER_API_KEY` in `.env` to enable auth.

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

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WARP_WAIT_RETRIES` | `30` | Number of status-poll retries before starting 3proxy |
| `WARP_WAIT_INTERVAL` | `2` | Seconds between status polls |
| `WARP_IP_CACHE` | `/var/cache/warp-ip.txt` | Where to cache the external IP from ipinfo.io |
| `WARP_CLIENT_SECRET` | — | WARP Teams enrollment token (optional) |

### Manager Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MANAGER_API_KEY` | `""` | API key for auth (empty = no auth) |
| `WARP_IMAGE` | `warp-proxy:latest` | Image tag for spawned containers |
| `SOCKS_BASE` | `1080` | First SOCKS5 port to assign |
| `HTTP_BASE` | `3128` | First HTTP proxy port to assign |
| `MAX_INSTANCES` | `10` | Max proxy containers |
| `DOCKER_TIMEOUT` | `60` | Docker CLI timeout (seconds) |
| `STOP_TIMEOUT` | `10` | Container stop timeout (seconds) |

## Manager API

See `compose.manager.yaml` and [.env.example](.env.example).

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | No | Health check |
| `GET` | `/` | `X-API-Key` | List all proxies |
| `GET` | `/{name}` | `X-API-Key` | Get one proxy |
| `POST` | `/create` | `X-API-Key` | Spin up new instance |
| `POST` | `/restart` | `X-API-Key` | Restart/start a container |
| `POST` | `/renew` | `X-API-Key` | WARP disconnect → connect |
| `DELETE` | `/delete` | `X-API-Key` | Stop, remove, clean volumes |

### Custom 3proxy Config

Mount your own config:

```bash
docker compose run --entrypoint '' warp-proxy sh
```

Or mount a volume:

```yaml
services:
  warp-proxy:
    volumes:
      - ./custom.cfg:/etc/3proxy/3proxy.cfg
```

See [3proxy.cfg docs](https://github.com/3proxy/3proxy/wiki/3proxy.cfg) for all options.

## Stack

- [3proxy](https://github.com/3proxy/3proxy) — Tiny proxy server
- [Cloudflare WARP](https://developers.cloudflare.com/warp-client/) — Encrypted tunnel
- [FastAPI](https://fastapi.tiangolo.com/) — Manager REST API
- Ubuntu 24.04 (noble) base image

## Files

```
warp-proxy/
├── Dockerfile               # Single-stage build with 3proxy + WARP
├── compose.yaml             # Single proxy deployment
├── compose.manager.yaml     # Manager-only deployment
├── docker-entrypoint.sh     # WARP registration/connect/disconnect
├── 3proxy.cfg               # Default proxy config
├── .env.example             # Environment variable reference
├── .dockerignore            # Docker build context exclusions
├── .gitignore
├── README.md
└── manager/
    ├── Dockerfile           # Python + Docker CLI
    ├── server.py            # FastAPI manager app
    └── requirements.txt     # Python dependencies
```
