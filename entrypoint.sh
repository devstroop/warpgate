#!/bin/bash
set -euo pipefail

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
/usr/bin/warp-svc >/dev/null 2>&1 &
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
    if output=$(warp-cli --accept-tos registration new 2>&1); then
        # Extract just the registration ID for logging
        reg_id=$(echo "$output" | grep -oP 'Registration:\s*\K\S+' || true)
        echo "WARP registered: $reg_id"
    else
        echo "ERROR: WARP registration failed: $output" >&2
    fi
fi

echo "Setting MASQUE tunnel protocol..."
warp-cli --accept-tos tunnel protocol set MASQUE

echo "Connecting to WARP..."
if ! warp-cli --accept-tos connect; then
    echo "ERROR: WARP connect failed (exit $?)"
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

echo "Capturing egress IP..."
curl -s --max-time 5 https://ipinfo.io/ip > /tmp/egress_ip 2>/dev/null || echo "unknown" > /tmp/egress_ip

mkdir -p /var/log/3proxy

echo "Starting 3proxy..."
/usr/bin/3proxy /etc/3proxy/3proxy.cfg &
THREE_PROXY_PID=$!

wait
