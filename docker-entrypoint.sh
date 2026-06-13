#!/bin/bash
set -euo pipefail

: "${WARP_IP_CACHE:=/var/cache/warp-ip.txt}"
: "${WARP_WAIT_RETRIES:=30}"
: "${WARP_WAIT_INTERVAL:=2}"

WARP_SVC_PID=
THREE_PROXY_PID=

cleanup() {
    echo "Stopping 3proxy..."
    if [ -n "$THREE_PROXY_PID" ] && kill -0 "$THREE_PROXY_PID" 2>/dev/null; then
        kill "$THREE_PROXY_PID"
        wait "$THREE_PROXY_PID" 2>/dev/null || true
    fi
    echo "Disconnecting WARP..."
    warp-cli --accept-tos disconnect 2>/dev/null || true
    echo "Stopping WARP daemon..."
    if [ -n "$WARP_SVC_PID" ] && kill -0 "$WARP_SVC_PID" 2>/dev/null; then
        kill "$WARP_SVC_PID"
        wait "$WARP_SVC_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 0' TERM INT

echo "Starting WARP daemon..."
/usr/bin/warp-svc &
WARP_SVC_PID=$!

echo "Waiting for WARP daemon to be ready..."
for ((i = 1; i <= WARP_WAIT_RETRIES; i++)); do
    if warp-cli --accept-tos status >/dev/null 2>&1; then
        echo "WARP daemon ready"
        break
    fi
    sleep "$WARP_WAIT_INTERVAL"
done

if [ ! -f /var/lib/cloudflare-warp/reg.json ]; then
    echo "Registering WARP..."
    warp-cli --accept-tos registration new
fi

echo "Connecting to WARP..."
if ! warp-cli --accept-tos connect; then
    echo "ERROR: WARP connect failed (exit $?)"
fi

if [ -n "$WARP_CLIENT_SECRET" ]; then
    echo "WARP Teams token detected, enrolling..."
    warp-cli --accept-tos teams-enroll-token "$WARP_CLIENT_SECRET" || true
fi

echo "Waiting for WARP connection..."
for ((i = 1; i <= WARP_WAIT_RETRIES; i++)); do
    status=$(warp-cli --accept-tos status 2>/dev/null || true)
    if echo "$status" | grep -q "Status update: Connected" && \
       echo "$status" | grep -q "Network: healthy"; then
        echo "WARP connected"
        break
    fi
    if echo "$status" | grep -q "Status update: Connected"; then
        echo "WARP partially connected (waiting for health)..."
    fi
    sleep "$WARP_WAIT_INTERVAL"
done

mkdir -p "$(dirname "$WARP_IP_CACHE")"
curl -sf --connect-timeout 5 --max-time 10 https://ipinfo.io/ip > "$WARP_IP_CACHE" 2>/dev/null && echo "External IP cached" || true

mkdir -p /var/log/3proxy

echo "Starting 3proxy..."
/usr/bin/3proxy /etc/3proxy/3proxy.cfg &
THREE_PROXY_PID=$!

wait
