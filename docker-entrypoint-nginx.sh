#!/bin/sh
set -e

# Wait for the warpgate service to be DNS-resolvable.
# Scale via: docker compose up -d --scale warpgate=N
echo "Waiting for warpgate service to be resolvable via DNS..."
while ! getent hosts warpgate >/dev/null 2>&1; do
    sleep 1
done
echo "warpgate resolved, starting nginx"

exec nginx -g 'daemon off;'