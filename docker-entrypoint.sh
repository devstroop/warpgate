#!/bin/bash
set -e

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
    sleep 1
done

if [ ! -f /var/lib/cloudflare-warp/reg.json ]; then
    echo "Registering WARP..."
    warp-cli --accept-tos registration new
fi

echo "Connecting to WARP..."
warp-cli --accept-tos connect || echo "WARP connect returned $?"

if [ -n "$WARP_CLIENT_ID" ] && [ -n "$WARP_CLIENT_SECRET" ]; then
    echo "WARP+ Teams license detected, registering..."
    warp-cli --accept-tos teams-enroll-token "$WARP_CLIENT_ID" || true
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
curl -sf https://ipinfo.io/ip > "$WARP_IP_CACHE" 2>/dev/null && echo "External IP cached" || true

mkdir -p /var/log/3proxy

echo "Starting 3proxy..."
/usr/bin/3proxy /etc/3proxy/3proxy.cfg &
THREE_PROXY_PID=$!

wait
