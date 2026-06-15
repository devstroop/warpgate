# WarpGate

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

### Multi-proxy cluster (compose.cluster.yaml)

```bash
# One-time setup: create the shared network
docker network create warpgate_network

# Build the image first
docker compose build

# Start cluster (default: 3 proxies)
docker compose -f compose.cluster.yaml up -d --scale warpgate=3

# Or use the Makefile
make cluster N=5
```

The cluster puts nginx in front as a TCP-level load balancer with `least_conn` across all warpgate instances. Scaling is dynamic — just use `--scale warpgate=N`.

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
| `WARP_CLIENT_SECRET` | — | WARP Teams enrollment token (optional) |

### Custom 3proxy Config

Mount your own config:

```yaml
services:
  warpgate:
    volumes:
      - ./custom.cfg:/etc/3proxy/3proxy.cfg
```

See [3proxy.cfg docs](https://github.com/3proxy/3proxy/wiki/3proxy.cfg) for all options.

## Architecture

```
Client → nginx (TCP least_conn) → warpgate[1..N] (WARP + 3proxy) → Internet
```

- **nginx** handles TCP-level load balancing with DNS-based upstream discovery
- **warpgate** runs Cloudflare WARP + 3proxy (SOCKS5 on :1080, HTTP on :3128)
- Scale by running `docker compose -f compose.cluster.yaml up -d --scale warpgate=N`

## Stack

- [3proxy](https://github.com/3proxy/3proxy) — Tiny proxy server
- [Cloudflare WARP](https://developers.cloudflare.com/warp-client/) — Encrypted tunnel
- Ubuntu 24.04 (noble) base image

## Files

```
warpgate/
├── Dockerfile                     # Single-stage build with 3proxy + WARP
├── compose.yaml                   # Single proxy deployment
├── compose.cluster.yaml           # Multi-proxy cluster with nginx LB
├── nginx.conf                     # TCP stream load balancer config
├── docker-entrypoint.sh           # WARP registration/connect/3proxy
├── docker-entrypoint-nginx.sh     # DNS wait + nginx start
├── 3proxy.cfg                     # Default proxy config
├── Makefile                       # Dev commands
├── .dockerignore
├── .gitignore
└── README.md
```
